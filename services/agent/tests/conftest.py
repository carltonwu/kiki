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


@pytest.fixture
async def database_url():
    """An isolated schema with every Rust migration applied (pgvector stays in public)."""
    base = os.getenv("TEST_DATABASE_URL")
    if not base:
        pytest.skip("set TEST_DATABASE_URL for Postgres integration tests")
    schema = f"host_test_{uuid4().hex}"
    async with await AsyncConnection.connect(base, autocommit=True) as connection:
        await connection.execute(VECTOR)
        await connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        scoped = make_conninfo(base, options=f"-csearch_path={schema},public")
        try:
            async with await AsyncConnection.connect(scoped, autocommit=True) as setup:
                for migration in sorted(MIGRATIONS.glob("*.sql")):
                    await setup.execute(migration.read_text())
            yield scoped
        finally:
            await connection.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
            )


class Recorded:
    """What the host recorded while export was on, in memory."""

    def __init__(self, reader, spans):
        self.reader, self.spans = reader, spans

    def value(self, name: str, **attributes) -> float | None:
        """A counter's value, or a histogram's count, for exactly `attributes`."""
        found = [
            point
            for resource in self.reader.get_metrics_data().resource_metrics
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
    return Recorded(reader, spans)
