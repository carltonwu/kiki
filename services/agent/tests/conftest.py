import os
from pathlib import Path
from uuid import uuid4

import pytest
from psycopg import AsyncConnection, sql
from psycopg.conninfo import make_conninfo

MIGRATIONS = Path(__file__).resolve().parents[2] / "ingester/migrations"
# pgvector in public, set up once under a lock shared with the Rust tests
# (services/ingester/src/test_db.rs): migration 0007 would otherwise install it
# into the test schema, where it's dropped with the schema.
VECTOR = """
BEGIN;
SELECT pg_advisory_xact_lock(hashtext('kiki-tests-vector-extension'));
CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;
DO $$ BEGIN
    IF (SELECT n.nspname FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace
        WHERE e.extname = 'vector') <> 'public' THEN
        ALTER EXTENSION vector SET SCHEMA public;
    END IF;
END $$;
COMMIT;
"""


async def apply_migrations(database_url: str, *, before: str | None = None) -> None:
    """Applies the Rust migrations in order, those named before `before` only if given."""
    async with await AsyncConnection.connect(database_url, autocommit=True) as setup:
        for migration in sorted(MIGRATIONS.glob("*.sql")):
            if before is not None and migration.name >= before:
                return
            await setup.execute(migration.read_text())


@pytest.fixture
async def empty_database_url():
    """An isolated schema with no migrations applied (pgvector stays in public)."""
    async for url in schema(migrate=False):
        yield url


@pytest.fixture
async def database_url():
    """An isolated schema with every Rust migration applied (pgvector stays in public)."""
    async for url in schema(migrate=True):
        yield url


async def schema(*, migrate: bool):
    base = os.getenv("TEST_DATABASE_URL")
    if not base:
        pytest.skip("set TEST_DATABASE_URL for Postgres integration tests")
    schema = f"host_test_{uuid4().hex}"
    async with await AsyncConnection.connect(base, autocommit=True) as connection:
        await connection.execute(VECTOR)
        await connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        scoped = make_conninfo(base, options=f"-csearch_path={schema},public")
        try:
            if migrate:
                await apply_migrations(scoped)
            yield scoped
        finally:
            await connection.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
            )


class Recorded:
    """What the host recorded while export was on, in memory."""

    def __init__(self, reader, spans, recorder):
        self.reader, self.spans, self.recorder = reader, spans, recorder

    def value(self, name: str, **attributes) -> float | None:
        """A counter's value, or a histogram's count, for exactly `attributes`,
        once a flush would have released counters' held-back values."""
        baselines = self.recorder.baselines
        baselines.release(baselines.pending())
        data = self.reader.get_metrics_data()  # None before anything is recorded
        found = [
            point
            for resource in (data.resource_metrics if data else [])
            for scope in resource.scope_metrics
            for metric in scope.metrics
            if metric.name == name
            for point in metric.data.data_points
            if dict(point.attributes) == attributes
        ]
        if not found:
            return None
        [point] = found
        return point.count if hasattr(point, "count") else point.value

    def finished(self, name: str) -> list:
        return [s for s in self.spans.get_finished_spans() if s.name == name]


@pytest.fixture
def recorded(monkeypatch):
    """The host's metrics and spans for one test, recorded in memory."""
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from kiki_agent_host import telemetry

    reader, spans = InMemoryMetricReader(), InMemorySpanExporter()
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(spans))
    recorder = telemetry.Recorder(MeterProvider(metric_readers=[reader]), tracer_provider)
    monkeypatch.setattr(telemetry, "_recorder", recorder)
    monkeypatch.setattr(telemetry, "_models", set())
    return Recorded(reader, spans, recorder)


@pytest.fixture(autouse=True)
def model_catalog(monkeypatch):
    """OpenRouter's model catalog, empty (every model counts as thinking) unless a
    test fills it: tests don't fetch the real one."""
    from kiki_agent_host.openrouter import catalog

    listed: dict[str, frozenset[str]] = {}

    async def fetch():
        return dict(listed)

    monkeypatch.setattr(catalog, "fetch", fetch)
    monkeypatch.setattr(catalog, "_catalog", {})
    monkeypatch.setattr(catalog, "_fetched_at", -float("inf"))
    monkeypatch.setattr(catalog, "_attempted_at", -float("inf"))
    monkeypatch.setattr(catalog, "_refreshing", None)
    return listed
