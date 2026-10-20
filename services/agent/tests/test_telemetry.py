import logging
import os
import subprocess
import sys

import pytest

from kiki_agent_host import telemetry
from kiki_agent_host.api.app import create_app
from kiki_agent_host.config import Settings


def test_export_needs_an_endpoint_and_no_kill_switch():
    endpoint = {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4318"}
    assert not telemetry.enabled({})
    assert not telemetry.enabled({"OTEL_EXPORTER_OTLP_ENDPOINT": " "})
    assert telemetry.enabled(endpoint)
    assert telemetry.enabled({**endpoint, "OTEL_SDK_DISABLED": "false"})
    assert not telemetry.enabled({**endpoint, "OTEL_SDK_DISABLED": "TRUE"})


def test_resource_describes_the_railway_deployment():
    assert telemetry.resource_attributes({}) == {
        "service.name": "agent",
        "service.namespace": "kiki",
        "deployment.environment.name": "local",
        "service.instance.id": "agent",
    }
    assert telemetry.resource_attributes(
        {
            "OTEL_SERVICE_NAME": "agent-staging",
            "RAILWAY_ENVIRONMENT_NAME": "production",
            "RAILWAY_GIT_COMMIT_SHA": "abc123",
            "RAILWAY_REPLICA_ID": "replica-1",  # not the instance: one runs at a time
        }
    ) == {
        "service.name": "agent-staging",
        "service.namespace": "kiki",
        "deployment.environment.name": "production",
        "service.version": "abc123",
        "service.instance.id": "agent-staging",
    }


async def test_without_an_endpoint_everything_is_a_safe_no_op(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    assert not telemetry.configure()
    telemetry.meter().create_counter("kiki.test").add(1, {"kind": "reply"})
    await telemetry.flush()
    telemetry.flush_soon()
    telemetry.shutdown()
    app = create_app(Settings("postgresql://unreachable.invalid/kiki", "token"))
    assert not any(m.cls is telemetry.FlushAfterJobs for m in app.user_middleware)


def test_http_client_and_exporter_logs_are_never_exported():
    def record(name):
        return logging.LogRecord(name, logging.DEBUG, __file__, 1, "msg", None, None)

    for name in ("httpx", "httpx2", "httpcore", "httpcore2", "openai._base_client"):
        assert not telemetry.exportable(record(name))
    for name in ("opentelemetry.sdk._shared_internal", "urllib3.connectionpool"):
        assert not telemetry.exportable(record(name))
    assert telemetry.exportable(record("kiki_agent_host.api.app"))
    assert telemetry.exportable(record("httpx_like"))


# Runs in a subprocess (providers are process-wide) with a local OTLP receiver
# that records every request; RECEIVED holds (path, headers), and EXPORTING is
# set once the first has arrived.
RECEIVER = """
import asyncio, os, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

RECEIVED = []
EXPORTING = threading.Event()

class Receiver(BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.reply()

    def do_GET(self):
        self.reply()

    def reply(self):
        RECEIVED.append((self.path, dict(self.headers)))
        EXPORTING.set()
        time.sleep(float(os.environ.get("RECEIVER_DELAY", "0")))
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass

server = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
server.daemon_threads = True
threading.Thread(target=server.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{server.server_port}"
os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = BASE

import logging
from kiki_agent_host import telemetry
from kiki_agent_host.logs import configure_logging

configure_logging()
assert telemetry.configure()
"""


def run(script: str, **env: str) -> str:
    env = {**os.environ, "OTEL_SDK_DISABLED": "false", **env}
    ran = subprocess.run(
        [sys.executable, "-c", RECEIVER + script],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert ran.returncode == 0, ran.stderr
    return ran.stdout


def test_after_a_flush_nothing_is_sent_while_idle():
    """Railway sleeps the host only after 10 idle minutes without outbound traffic."""
    run(
        """
from opentelemetry import trace

names = sorted(t.name for t in threading.enumerate())
assert "OtelPeriodicExportingMetricReader" not in names, names

with trace.get_tracer("test").start_as_current_span("job"):
    logging.getLogger("kiki_agent_host.test").warning("a job")
    telemetry.meter().create_counter("kiki.test").add(1)
asyncio.run(telemetry.flush())
time.sleep(1)  # the batch processors wake every 0.1 s here
paths = sorted(path for path, _ in RECEIVED)
assert paths == ["/v1/logs", "/v1/metrics", "/v1/traces"], paths
time.sleep(1.5)
assert len(RECEIVED) == 3, RECEIVED
""",
        OTEL_BSP_SCHEDULE_DELAY="100",
        OTEL_BLRP_SCHEDULE_DELAY="100",
    )


@pytest.mark.parametrize("semconv", ["", "http/dup"])
def test_trace_context_comes_in_but_never_goes_out(semconv):
    """Client spans are still recorded, with no credentials, query or fragment in
    any URL attribute, even when legacy semantic conventions are asked for."""
    run(
        """
import httpx, httpx2
from opentelemetry import trace
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from kiki_agent_host.api.app import create_app
from kiki_agent_host.config import Settings

spans = InMemorySpanExporter()
trace.get_tracer_provider().add_span_processor(SimpleSpanProcessor(spans))
app = create_app(Settings("postgresql://unreachable.invalid/kiki", "token"))
parent = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
url = BASE.replace("http://", "http://user:secret@") + "/probe?token=secret#frag"

async def main():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://host") as http:
        await http.post("/v1/jobs", json={}, headers={"traceparent": parent})
    with trace.get_tracer("test").start_as_current_span("job"):
        async with httpx.AsyncClient() as http:
            await http.get(url)
        async with httpx2.AsyncClient() as http:
            await http.get(url)

asyncio.run(main())
server_span = next(s for s in spans.get_finished_spans() if s.name == "POST /v1/jobs")
assert format(server_span.context.trace_id, "032x") == "0af7651916cd43dd8448eb211c80319c"
probes = [headers for path, headers in RECEIVED if path.startswith("/probe")]
assert len(probes) == 2, RECEIVED
assert not any("traceparent" in {k.lower() for k in h} for h in probes), probes
clients = [s for s in spans.get_finished_spans() if s.name == "GET"]
assert [s.attributes["url.full"] for s in clients] == [BASE + "/probe"] * 2, clients
for span in spans.get_finished_spans():
    assert "http.url" not in span.attributes, span.attributes
    assert not any("secret" in str(v) for v in span.attributes.values()), span.attributes
""",
        OTEL_SEMCONV_STABILITY_OPT_IN=semconv,
    )


def test_shutdown_keeps_to_its_deadline_when_the_backend_stalls():
    run(
        """
from opentelemetry import trace

with trace.get_tracer("test").start_as_current_span("job"):
    logging.getLogger("kiki_agent_host.test").warning("a job")
    telemetry.meter().create_counter("kiki.test").add(1)
started = time.monotonic()
telemetry.shutdown()
elapsed = time.monotonic() - started
assert elapsed < telemetry.SHUTDOWN_SECONDS + 0.5, elapsed
assert telemetry.flush_soon() is None
""",
        RECEIVER_DELAY="30",
    )


def test_process_exit_keeps_to_the_shutdown_deadline():
    """A flush in flight, batches queued and a slow backend: shutdown() keeps to
    its deadline, and neither the flush thread nor the SDK's exit handlers hold
    the process long after (unbounded, they'd drain 20 batches at 1.6 s each)."""
    script = """
from opentelemetry import trace

async def main():
    tracer = trace.get_tracer("test")
    for _ in range(200):
        tracer.start_span("queued").end()
    telemetry.flush_soon()
    # The flush is now waiting on the receiver.
    assert await asyncio.to_thread(EXPORTING.wait, 10)

    def stop():
        started = time.monotonic()
        telemetry.shutdown()
        return time.monotonic() - started

    print("shutdown took", await asyncio.to_thread(stop), flush=True)

asyncio.run(main())
"""
    env = {
        **os.environ,
        "OTEL_SDK_DISABLED": "false",
        "OTEL_BSP_MAX_EXPORT_BATCH_SIZE": "10",
        "RECEIVER_DELAY": "1.6",
    }
    process = subprocess.Popen(
        [sys.executable, "-c", RECEIVER + script], env=env, stdout=subprocess.PIPE, text=True
    )
    try:
        out, _ = process.communicate(timeout=telemetry.SHUTDOWN_SECONDS + 15)
    finally:
        process.kill()
    assert process.returncode == 0
    took = float(
        next(line for line in out.splitlines() if line.startswith("shutdown took")).split()[-1]
    )
    assert took < telemetry.SHUTDOWN_SECONDS + 0.5, took


def test_queries_are_traced_inside_a_request_without_sql_or_values():
    """Spans named by the operation: composed SQL renders its values into the
    text, and Postgres quotes them in its errors, so a failed query keeps only
    its type."""
    if not os.getenv("TEST_DATABASE_URL"):
        pytest.skip("set TEST_DATABASE_URL for Postgres integration tests")
    run(
        """
from opentelemetry import trace
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from psycopg import sql
from kiki_agent_host import db

spans = InMemorySpanExporter()
trace.get_tracer_provider().add_span_processor(SimpleSpanProcessor(spans))

async def main():
    async with await db.connect(os.environ["TEST_DATABASE_URL"]) as connection:
        await connection.execute("SELECT %s::text", ("outside-secret",))
        with trace.get_tracer("test").start_as_current_span("job"):
            await connection.execute("SELECT %s::text", ("inside-secret",))
            # Rendered with its value by the instrumentation.
            await connection.execute(sql.SQL("SELECT {}::text").format(sql.Literal("secret")))
            try:
                await connection.execute("SELECT %s::int", ("not-a-number-secret",))
            except Exception as error:
                assert "secret" in str(error)  # what the span must not keep

asyncio.run(main())
queries = [s for s in spans.get_finished_spans() if s.name == "SELECT"]
assert len(queries) == 3, spans.get_finished_spans()
for query in queries:
    assert query.attributes["db.system"] == "postgresql", query.attributes
    assert query.attributes["db.name"] == "kiki", query.attributes
    assert not telemetry.SQL_TEXT & set(query.attributes), query.attributes
assert queries[2].attributes["error.type"] == "InvalidTextRepresentation", queries[2].attributes
for span in spans.get_finished_spans():
    assert not span.events, span.events
    assert "secret" not in str(span.status.description)
    assert not any("secret" in str(v) for v in span.attributes.values()), span.attributes
"""
    )
