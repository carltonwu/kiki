"""OpenTelemetry traces, metrics and logs over OTLP/HTTP (docs/observability.md).

Export is off unless OTEL_EXPORTER_OTLP_ENDPOINT is set, so tests, local runs and
the simulator send nothing. The exporters read the other standard OTEL_* variables
(headers, timeouts) themselves.

Railway sleeps this service once it has sent nothing for 10 minutes, so nothing
exports on a timer: metrics leave only on flush(), which the API runs after each
job, and the span and log batches send only when they hold something.
"""

import asyncio
import logging
import math
import os
from collections.abc import Mapping

from opentelemetry import metrics

logger = logging.getLogger(__name__)

SCOPE = "kiki-agent-host"
FLUSH_TIMEOUT_MILLIS = 5_000
# The exporter's own logs (and its HTTP client's) would each cause another export.
EXPORTER_LOGGERS = ("opentelemetry", "urllib3", "requests")
JOB_PATHS = "/v1/"

_providers: tuple | None = None
_flushes: set[asyncio.Task] = set()


def enabled(env: Mapping[str, str] = os.environ) -> bool:
    endpoint = env.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    return bool(endpoint) and env.get("OTEL_SDK_DISABLED", "").strip().lower() != "true"


def resource_attributes(env: Mapping[str, str] = os.environ) -> dict[str, str]:
    def get(name: str) -> str:
        return env.get(name, "").strip()

    name = get("OTEL_SERVICE_NAME") or "agent"
    attributes = {
        "service.name": name,
        "service.namespace": "kiki",
        "deployment.environment.name": get("RAILWAY_ENVIRONMENT_NAME") or "local",
        # Never the SDK's random per-process ID: it becomes the `instance` label,
        # and every wake from sleep would start a new set of series.
        "service.instance.id": get("RAILWAY_REPLICA_ID") or name,
    }
    if get("RAILWAY_GIT_COMMIT_SHA"):
        attributes["service.version"] = get("RAILWAY_GIT_COMMIT_SHA")
    return attributes


def configure() -> bool:
    """Starts export when the environment enables it. Call once logging is set up
    and before the app and its HTTP clients are created."""
    global _providers
    if _providers is not None or not enabled():
        return _providers is not None
    # Imported here: a process with export off never loads the SDK.
    from opentelemetry import _logs, trace
    from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.httpx import (
        HTTPX2ClientInstrumentor,
        HTTPXClientInstrumentor,
    )
    from opentelemetry.instrumentation.logging.handler import LoggingHandler
    from opentelemetry.sdk._logs import LoggerProvider
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk.metrics.view import DropAggregation, View
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    # Stable HTTP semantic conventions: http.server.request.duration in seconds.
    os.environ.setdefault("OTEL_SEMCONV_STABILITY_OPT_IN", "http")
    resource = Resource.create(resource_attributes())
    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    # An infinite interval starts no export thread; flush() collects instead.
    # Cumulative temporality (the exporter's default) suits Grafana's Mimir.
    reader = PeriodicExportingMetricReader(OTLPMetricExporter(), export_interval_millis=math.inf)
    # Request and response size histograms: series spent on nothing we read.
    drop_sizes = View(instrument_name="http.server.*.body.size", aggregation=DropAggregation())
    meter_provider = MeterProvider(resource=resource, metric_readers=[reader], views=[drop_sizes])
    logger_provider = LoggerProvider(resource=resource)
    logger_provider.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter()))
    trace.set_tracer_provider(tracer_provider)
    metrics.set_meter_provider(meter_provider)
    _logs.set_logger_provider(logger_provider)

    handler = LoggingHandler(logger_provider=logger_provider)
    handler.addFilter(not_exporter)
    logging.getLogger().addHandler(handler)
    # Client spans carry the W3C traceparent on outbound calls (httpx2 is the
    # OpenAI SDK's, so OpenRouter's). No client metrics: read_link's arbitrary
    # hosts would become metric labels. GenAI instrumentation stays off: prompts
    # and replies never go into telemetry.
    for instrumentor in (HTTPXClientInstrumentor(), HTTPX2ClientInstrumentor()):
        instrumentor.instrument(
            meter_provider=metrics.NoOpMeterProvider(),
            request_hook=without_query,
            async_request_hook=async_without_query,
        )
    _providers = (tracer_provider, meter_provider, logger_provider)
    logger.info("OpenTelemetry export on service=%s", resource_attributes()["service.name"])
    return True


def instrument_app(app) -> None:
    """Traces requests (continuing the caller's trace) and flushes after each job."""
    if _providers is None:
        return
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    app.add_middleware(FlushAfterJobs)
    FastAPIInstrumentor.instrument_app(
        app, excluded_urls="/health", exclude_spans=["receive", "send"]
    )


def meter() -> metrics.Meter:
    """The meter for the host's instruments. A no-op when export is off."""
    return metrics.get_meter(SCOPE)


async def flush() -> None:
    """Sends everything recorded so far, off the event loop. Never raises."""
    if _providers is not None:
        await asyncio.to_thread(_force_flush)


def flush_soon() -> None:
    """flush() once the current request has finished, its own span included."""
    if _providers is not None:
        task = asyncio.get_running_loop().create_task(flush())
        _flushes.add(task)
        task.add_done_callback(_flushes.discard)


def shutdown() -> None:
    """Flushes and stops the exporters. For process exit."""
    global _providers
    if _providers is None:
        return
    _force_flush()  # metrics have no export thread to send the last collection
    providers, _providers = _providers, None
    for provider in providers:
        try:
            provider.shutdown()
        except Exception:  # exiting anyway
            logger.warning("telemetry shutdown failed", exc_info=True)


def _force_flush() -> None:
    for provider in _providers or ():
        try:
            provider.force_flush(FLUSH_TIMEOUT_MILLIS)
        except Exception:  # telemetry never fails a job
            logger.warning("telemetry flush failed", exc_info=True)


def not_exporter(record: logging.LogRecord) -> bool:
    return not any(
        record.name == name or record.name.startswith(name + ".") for name in EXPORTER_LOGGERS
    )


def without_query(span, request) -> None:
    """Drops the query from a client span's URL: Discord's signed attachment
    links (and fetched pages' links) carry access tokens there."""
    if span.is_recording():
        span.set_attribute("url.full", str(request.url.copy_with(query=None)))


async def async_without_query(span, request) -> None:
    without_query(span, request)


class FlushAfterJobs:
    """ASGI middleware: schedules a flush once a job request has been answered."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        try:
            await self.app(scope, receive, send)
        finally:
            if scope["type"] == "http" and scope["path"].startswith(JOB_PATHS):
                flush_soon()
