"""OpenTelemetry traces, metrics and logs over OTLP/HTTP (docs/observability.md).

Export is off unless OTEL_EXPORTER_OTLP_ENDPOINT is set, so tests, local runs and
the simulator send nothing. The exporters read the other standard OTEL_* variables
(headers, timeouts) themselves.

Railway sleeps this service once it has sent nothing for 10 minutes, so nothing
exports on a timer: metrics leave only on flush(), which the API runs after each
job, and the span and log batches send only when they hold something.
"""

import asyncio
import contextlib
import logging
import math
import os
import threading
import time
from collections.abc import Mapping

from opentelemetry import metrics
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

logger = logging.getLogger(__name__)

SCOPE = "kiki-agent-host"
# Per export request; the batch processors don't honour flush timeouts themselves.
EXPORT_TIMEOUT_SECONDS = 3
FLUSH_TIMEOUT_MILLIS = 3_000
# All of shutdown: Railway's 30 s drain may be mostly spent by then.
SHUTDOWN_SECONDS = 4
# Never exported as logs: the exporter's own (each would cause another export),
# and HTTP clients', whose request lines hold signed URLs and, at DEBUG, bodies.
EXCLUDED_LOGGERS = (
    "opentelemetry",
    "urllib3",
    "requests",
    "httpx",
    "httpx2",
    "httpcore",
    "httpcore2",
    "h11",
    "openai",
)
JOB_PATHS = "/v1/"

_providers: tuple | None = None
_handler: logging.Handler | None = None
_closing = False
_flushing: asyncio.Task | None = None
_flush_again = False


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
        # The `instance` label. One instance runs, so the name: never the SDK's
        # random per-process ID, which would start new series on every wake.
        "service.instance.id": name,
    }
    if get("RAILWAY_GIT_COMMIT_SHA"):
        attributes["service.version"] = get("RAILWAY_GIT_COMMIT_SHA")
    return attributes


def configure() -> bool:
    """Starts export when the environment enables it. Call once logging is set up
    and before the app and its HTTP clients are created."""
    global _providers, _handler, _closing
    if _providers is not None or not enabled():
        return _providers is not None
    # Imported here: a process with export off never loads the SDK.
    from opentelemetry import _logs, propagate, trace
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

    # Stable HTTP semantic conventions only (http.server.request.duration in
    # seconds), forced: "http/dup" would add the legacy http.url, query and all.
    os.environ["OTEL_SEMCONV_STABILITY_OPT_IN"] = "http"
    resource = Resource.create(resource_attributes())
    # No atexit handlers: they flush without a deadline. shutdown() has one.
    tracer_provider = TracerProvider(resource=resource, shutdown_on_exit=False)
    timeout = EXPORT_TIMEOUT_SECONDS
    tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(timeout=timeout)))
    # An infinite interval starts no export thread; flush() collects instead.
    # Cumulative temporality (the exporter's default) suits Grafana's Mimir.
    reader = PeriodicExportingMetricReader(
        OTLPMetricExporter(timeout=timeout), export_interval_millis=math.inf
    )
    # Request and response size histograms: series spent on nothing we read.
    drop_sizes = View(instrument_name="http.server.*.body.size", aggregation=DropAggregation())
    meter_provider = MeterProvider(
        resource=resource, metric_readers=[reader], views=[drop_sizes], shutdown_on_exit=False
    )
    logger_provider = LoggerProvider(resource=resource, shutdown_on_exit=False)
    logger_provider.add_log_record_processor(
        BatchLogRecordProcessor(OTLPLogExporter(timeout=timeout))
    )
    trace.set_tracer_provider(tracer_provider)
    metrics.set_meter_provider(meter_provider)
    _logs.set_logger_provider(logger_provider)
    propagate.set_global_textmap(ExtractOnly())

    _handler = LoggingHandler(logger_provider=logger_provider)
    _handler.addFilter(exportable)
    logging.getLogger().addHandler(_handler)
    # Client spans for outbound calls (httpx2 is the OpenAI SDK's, so OpenRouter's),
    # without sending trace context to those third parties (ExtractOnly). No
    # client metrics: read_link's arbitrary hosts would become metric labels.
    # GenAI instrumentation stays off: prompts and replies never go into telemetry.
    for instrumentor in (HTTPXClientInstrumentor(), HTTPX2ClientInstrumentor()):
        instrumentor.instrument(
            meter_provider=metrics.NoOpMeterProvider(),
            request_hook=redact_url,
            async_request_hook=async_redact_url,
        )
    _closing = False
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
    if task := flush_soon():
        await asyncio.shield(task)


def flush_soon() -> asyncio.Task | None:
    """Starts a flush, or, while one runs, has it go once more when it ends: one
    flush thread at a time however many jobs finish. None once shutting down."""
    global _flushing, _flush_again
    if _providers is None or _closing:
        return None
    if _flushing is not None and not _flushing.done():
        _flush_again = True
    else:
        _flushing = asyncio.get_running_loop().create_task(_flush_until_caught_up())
    return _flushing


async def _flush_until_caught_up() -> None:
    global _flush_again
    while True:
        _flush_again = False
        await _on_daemon_thread(_force_flush)
        if not _flush_again or _closing:
            return


async def _on_daemon_thread(work) -> None:
    """Like asyncio.to_thread, but exit doesn't wait for it: batch force_flush
    ignores its timeout, and only shutdown() may hold the process up."""
    loop = asyncio.get_running_loop()
    done = loop.create_future()

    def finish() -> None:
        if not done.done():
            done.set_result(None)

    def run() -> None:
        try:
            work()
        finally:
            with contextlib.suppress(RuntimeError):  # the loop has closed
                loop.call_soon_threadsafe(finish)

    threading.Thread(target=run, name="OtelFlush", daemon=True).start()
    await done


def shutdown() -> None:
    """Flushes and stops the exporters within SHUTDOWN_SECONDS in all. For process
    exit. Each provider stops on its own daemon thread, so a stalled one neither
    starves the others nor keeps the process alive past the deadline."""
    global _providers, _closing
    _closing = True
    if _providers is None:
        return
    providers, _providers = _providers, None
    if _handler is not None:
        logging.getLogger().removeHandler(_handler)
    deadline = time.monotonic() + SHUTDOWN_SECONDS
    threads = [
        threading.Thread(target=_stop, args=(provider,), name="OtelShutdown", daemon=True)
        for provider in providers
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(max(0, deadline - time.monotonic()))
    if any(thread.is_alive() for thread in threads):
        logger.warning("telemetry shutdown timed out after %ds", SHUTDOWN_SECONDS)


def _stop(provider) -> None:
    try:
        # Metrics have no export thread: only a flush sends the last collection.
        provider.force_flush(FLUSH_TIMEOUT_MILLIS)
        provider.shutdown()
    except Exception:  # exiting anyway
        logger.warning("telemetry shutdown failed", exc_info=True)


def _force_flush() -> None:
    for provider in _providers or ():
        try:
            provider.force_flush(FLUSH_TIMEOUT_MILLIS)
        except Exception:  # telemetry never fails a job
            logger.warning("telemetry flush failed", exc_info=True)


def exportable(record: logging.LogRecord) -> bool:
    return not any(
        record.name == name or record.name.startswith(name + ".") for name in EXCLUDED_LOGGERS
    )


def redact_url(span, request) -> None:
    """A client span's URL without credentials, query or fragment: Discord's signed
    attachment links (and fetched pages' links) carry access tokens there."""
    if span.is_recording():
        url = str(request.url.copy_with(username=None, password=None, query=None, fragment=None))
        span.set_attribute("url.full", url)
        if "http.url" in (getattr(span, "attributes", None) or {}):  # legacy semconv
            span.set_attribute("http.url", url)


async def async_redact_url(span, request) -> None:
    redact_url(span, request)


class ExtractOnly(TraceContextTextMapPropagator):
    """W3C trace context in from the ingester, never out: every outbound call
    goes to a third party."""

    def inject(self, carrier, context=None, setter=None) -> None:
        pass


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
