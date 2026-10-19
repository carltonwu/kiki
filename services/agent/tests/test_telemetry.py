import os
import subprocess
import sys

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
            "RAILWAY_REPLICA_ID": "replica-1",
        }
    ) == {
        "service.name": "agent-staging",
        "service.namespace": "kiki",
        "deployment.environment.name": "production",
        "service.version": "abc123",
        "service.instance.id": "replica-1",
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


def test_export_starts_no_timer_that_would_keep_the_host_awake():
    """Railway sleeps the host only after 10 idle minutes without outbound traffic."""
    script = """
import threading
from kiki_agent_host import telemetry
from kiki_agent_host.api.app import create_app
from kiki_agent_host.config import Settings

assert telemetry.configure()
app = create_app(Settings("postgresql://unreachable.invalid/kiki", "token"))
assert any(m.cls is telemetry.FlushAfterJobs for m in app.user_middleware)
print(sorted(t.name for t in threading.enumerate()))
telemetry.shutdown()
"""
    env = {
        **os.environ,
        "OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:9",
        "OTEL_SDK_DISABLED": "false",
    }
    ran = subprocess.run(
        [sys.executable, "-c", script], env=env, capture_output=True, text=True, check=True
    )
    # The span and log batchers send only when they hold something; metrics
    # have no thread of their own and leave only on flush().
    assert ran.stdout.strip() == (
        "['MainThread', 'OtelBatchLogRecordProcessor', 'OtelBatchSpanRecordProcessor']"
    )
