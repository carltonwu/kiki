"""OpenTelemetry traces, metrics and logs over OTLP/HTTP (docs/observability.md).

Export is off unless OTEL_EXPORTER_OTLP_ENDPOINT is set, so tests, local runs and
the simulator send nothing. The exporters read the other standard OTEL_* variables
(headers, timeouts) themselves.

Railway sleeps this service once it has sent nothing for 10 minutes, so nothing
exports on a timer: metrics leave only on flush(), which the API runs after each
job, and the span and log batches send only when they hold something.

So each wake is a new process whose counters start again, and Prometheus's
increase() ignores a series' first sample: a counter's attribute set first
exports a zero, and its real value once that has landed (`Baselines`,
`MetricFlush`).

The host's own metrics and spans are recorded through the functions at the end
(`llm_call`, `tool_call`, ...): with export off each is a None check, and the SDK
is never imported.
"""

import asyncio
import contextlib
import logging
import math
import os
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field

from opentelemetry import metrics, trace
from opentelemetry.trace import SpanKind, StatusCode
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
# Histogram buckets, fewer than the shared sets (docs/observability.md,
# "Cardinality rules"): tools and vision take seconds, runs and images tens of
# seconds, and nothing outlasts the 120 s job deadline. A run makes at most 10
# model requests.
DURATION_BUCKETS = (0.5, 2.5, 10, 30, 120)
COUNT_BUCKETS = (1, 2, 5, 10)
# Distinct `model` label values per process. Admins configure any model, and
# aliases resolve to new versioned IDs over time; later ones become "other".
MAX_MODELS = 16
# Between a new series' zero and its first real value: Mimir keeps samples by
# the millisecond, and two at one would be one.
POINT_GAP_SECONDS = 0.01

_providers: tuple | None = None
_recorder: "Recorder | None" = None
_models: set[str] = set()
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
    global _providers, _handler, _closing, _recorder
    if _providers is not None or not enabled():
        return _providers is not None
    # Imported here: a process with export off never loads the SDK.
    from opentelemetry import _logs, propagate
    from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.httpx import (
        HTTPX2ClientInstrumentor,
        HTTPXClientInstrumentor,
    )
    from opentelemetry.instrumentation.logging.handler import LoggingHandler
    from opentelemetry.instrumentation.psycopg import PsycopgInstrumentor
    from opentelemetry.sdk._logs import LoggerProvider
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk.metrics.view import (
        DropAggregation,
        ExplicitBucketHistogramAggregation,
        View,
    )
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
    exporter = Acknowledged(OTLPMetricExporter(timeout=timeout))
    reader = PeriodicExportingMetricReader(exporter, export_interval_millis=math.inf)
    # Series spent on nothing we read: request and response size histograms, and
    # database client metrics (made only if the database semconv is opted into).
    views = [
        View(instrument_name=name, aggregation=DropAggregation())
        for name in ("http.server.*.body.size", "db.client.*")
    ]
    # FastAPI's request duration in our duration buckets: semconv's end at 10 s,
    # and a job request takes tens of seconds, so every p95 read "10 s".
    views.append(
        View(
            instrument_name="http.server.request.duration",
            aggregation=ExplicitBucketHistogramAggregation(DURATION_BUCKETS),
        )
    )
    meter_provider = MeterProvider(
        resource=resource, metric_readers=[reader], views=views, shutdown_on_exit=False
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
    # Query spans named by the operation, without the SQL text or parameters
    # (InRequestOnly), and errors' messages.
    PsycopgInstrumentor().instrument(tracer_provider=InRequestOnly.Provider(tracer_provider))
    _closing = False
    _recorder = Recorder(meter_provider, tracer_provider)
    metric_flush = MetricFlush(meter_provider, exporter, _recorder.baselines)
    _providers = (tracer_provider, metric_flush, logger_provider)
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
    global _providers, _closing, _recorder
    _closing = True
    if _providers is None:
        return
    providers, _providers, _recorder = _providers, None, None
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


class Baselines:
    """Zero baselines for the host's counters. An attribute set's first point in
    a process would hold its first job's value, and increase() and rate() ignore
    a series' first sample (Grafana Cloud doesn't ingest created timestamps), so
    the first job after every wake would go uncounted.

    A counter's new attribute set (a key) is added 0 at once and its values held
    back until `MetricFlush` has sent the zero. Histograms can't be baselined (no
    SDK measurement makes a point with a count of 0): their first observation
    per attribute set per process goes uncounted by increase().

    Recording is on the event loop, releasing on a flush thread: the lock covers
    both."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # Keys whose zero has gone out in a flush: their values go straight in.
        self._exported: set = set()
        # Keys given their zero and the total held back since.
        self._pending: dict = {}

    def add(self, counter, amount: float, attributes: Mapping[str, str]) -> None:
        key = (counter.name, frozenset(attributes.items()))
        with self._lock:
            if key in self._exported:
                counter.add(amount, attributes)
            elif key in self._pending:
                self._pending[key][2] += amount
            else:
                counter.add(0, attributes)
                self._pending[key] = [counter, attributes, amount]

    def pending(self) -> frozenset:
        """Keys whose zeros a flush started now sends."""
        with self._lock:
            return frozenset(self._pending)

    def release(self, keys: frozenset) -> None:
        """Adds the held-back values of `keys`, whose zeros an export has sent."""
        with self._lock:
            for key in keys:
                if (held := self._pending.pop(key, None)) is not None:
                    counter, attributes, amount = held
                    counter.add(amount, attributes)
            self._exported.update(keys)


class Acknowledged:
    """A metric exporter that keeps whether its last export landed: the reader
    discards the result, and a zero must land before its value is released."""

    def __init__(self, exporter) -> None:
        self.exporter = exporter
        self.succeeded = False

    def export(self, metrics_data, timeout_millis: float = 10_000, **kwargs):
        from opentelemetry.sdk.metrics.export import MetricExportResult

        self.succeeded = False
        result = self.exporter.export(metrics_data, timeout_millis=timeout_millis, **kwargs)
        self.succeeded = result is MetricExportResult.SUCCESS
        return result

    def __getattr__(self, name: str):
        return getattr(self.exporter, name)


class MetricFlush:
    """The meter provider, flushed through the counters' baselines. Every metric
    flush, a job's or shutdown()'s, comes here, one at a time.

    A flush sends what was recorded, new series' zeros included. Once that
    export has landed, it waits POINT_GAP_SECONDS, releases those series' held
    back values and sends them, so only a flush with new series exports twice.
    Nothing else collects meanwhile, so a value's point is always the gap or
    more after its zero's. If the export fails, the values stay held back and
    the next flush sends the zeros again. The lock is held across exports: the
    reader runs one export at a time anyway."""

    def __init__(self, provider, exporter: Acknowledged, baselines: "Baselines") -> None:
        self.provider = provider
        self.exporter = exporter
        self.baselines = baselines
        self._lock = threading.Lock()

    def force_flush(self, timeout_millis: float) -> None:
        with self._lock:
            # Taken before collecting: these keys' zeros are in this export.
            zeroed = self.baselines.pending()
            if self._export(timeout_millis) and zeroed:
                time.sleep(POINT_GAP_SECONDS)
                self.baselines.release(zeroed)
                self._export(timeout_millis)

    def _export(self, timeout_millis: float) -> bool:
        self.exporter.succeeded = False
        self.provider.force_flush(timeout_millis)
        return self.exporter.succeeded

    def shutdown(self) -> None:
        self.provider.shutdown()


class Counter:
    """A counter recorded through `Baselines`."""

    def __init__(self, counter, baselines: Baselines) -> None:
        self.counter = counter
        self.baselines = baselines

    def add(self, amount: float, attributes: Mapping[str, str] | None = None) -> None:
        self.baselines.add(self.counter, amount, attributes or {})


class Recorder:
    """The host's instruments (docs/observability.md, "Metric catalog") and its
    tracer. Made only once export is on."""

    def __init__(self, meter_provider, tracer_provider) -> None:
        meter = meter_provider.get_meter(SCOPE)
        self.tracer = tracer_provider.get_tracer(SCOPE)

        def histogram(name: str, unit: str, description: str, buckets=DURATION_BUCKETS):
            return meter.create_histogram(
                name,
                unit=unit,
                description=description,
                explicit_bucket_boundaries_advisory=buckets,
            )

        self.baselines = Baselines()

        def counter(name: str, unit: str, description: str) -> Counter:
            created = meter.create_counter(name, unit=unit, description=description)
            return Counter(created, self.baselines)

        self.jobs = counter("kiki.agent.jobs", unit="{job}", description="Job requests answered")
        self.job_duration = histogram("kiki.agent.job.duration", "s", "Time running a job")
        self.jobs_rejected = counter(
            "kiki.agent.jobs.rejected", unit="{job}", description="Job requests turned away"
        )
        self.model_requests = histogram(
            "kiki.agent.job.model_requests",
            "{request}",
            "Model requests per reply or topic run",
            COUNT_BUCKETS,
        )
        self.llm_calls = counter("kiki.llm.calls", unit="{call}", description="Model calls")
        self.llm_cost = counter("kiki.llm.cost_usd", unit="{USD}", description="Billed cost")
        self.llm_tokens = counter("kiki.llm.tokens", unit="{token}", description="Tokens billed")
        self.llm_duration = histogram("kiki.llm.duration", "s", "Model call time")
        self.image_attempts = counter(
            "kiki.images.attempts", unit="{attempt}", description="Image model attempts"
        )
        self.image_fallback = counter(
            "kiki.images.fallback", unit="{attempt}", description="Fallback model attempts"
        )
        self.image_duration = histogram("kiki.images.duration", "s", "Image model attempt time")
        self.tool_calls = counter("kiki.tools.calls", unit="{call}", description="Tool calls")
        self.tool_duration = histogram("kiki.tools.duration", "s", "Tool call time")
        self.web_calls = counter(
            "kiki.web.calls", unit="{call}", description="OpenRouter web searches and fetches"
        )

    @contextlib.contextmanager
    def span(self, name: str, attributes: dict) -> Iterator[trace.Span]:
        with quiet_span(self.tracer, name, attributes=attributes) as span:
            yield span


@contextlib.contextmanager
def quiet_span(tracer, name: str, **options) -> Iterator[trace.Span]:
    """A span that keeps a failure's type, never its message: provider errors can
    quote prompts or credentials, Postgres's the values a query was given."""
    with tracer.start_as_current_span(
        name, record_exception=False, set_status_on_exception=False, **options
    ) as span:
        try:
            yield span
        except BaseException as error:
            span.set_attribute("error.type", type(error).__qualname__)
            span.set_status(StatusCode.ERROR)
            raise


class InRequestOnly(trace.Tracer):
    """psycopg's tracer: quiet spans named by the operation (`SELECT`), without
    the SQL text, and only inside a recorded one (a request's), so
    `kiki-agent-host index` doesn't start a trace per query."""

    def __init__(self, tracer: trace.Tracer) -> None:
        self.tracer = tracer

    def start_span(self, name, context=None, *args, **kwargs) -> trace.Span:
        if not trace.get_current_span(context).is_recording():
            return trace.INVALID_SPAN
        return self.tracer.start_span(name, context, *args, **kwargs)

    @contextlib.contextmanager
    def start_as_current_span(
        self, name, context=None, kind=SpanKind.INTERNAL, attributes=None, **_options
    ) -> Iterator[trace.Span]:
        if not trace.get_current_span(context).is_recording():
            yield trace.INVALID_SPAN
            return
        attributes = {k: v for k, v in (attributes or {}).items() if k not in SQL_TEXT}
        with quiet_span(
            self.tracer, name, context=context, kind=kind, attributes=attributes
        ) as span:
            yield WithoutSql(span)

    class Provider(trace.TracerProvider):
        def __init__(self, provider: trace.TracerProvider) -> None:
            self.provider = provider

        def get_tracer(self, *args, **kwargs) -> trace.Tracer:
            return InRequestOnly(self.provider.get_tracer(*args, **kwargs))


# The SQL text: psycopg's instrumentation renders composed SQL (`sql.Literal`)
# with its values, so no query's text is kept.
SQL_TEXT = frozenset({"db.statement", "db.query.text"})


class WithoutSql:
    """A query span that drops the SQL text the instrumentation sets."""

    def __init__(self, span: trace.Span) -> None:
        self.span = span

    def set_attribute(self, key: str, value) -> None:
        if key not in SQL_TEXT:
            self.span.set_attribute(key, value)

    def set_attributes(self, attributes) -> None:
        self.span.set_attributes({k: v for k, v in attributes.items() if k not in SQL_TEXT})

    def __getattr__(self, name: str):
        return getattr(self.span, name)


@dataclass
class Call:
    """One recorded call. The caller sets `outcome` where it differs from what
    the recorder infers, and `bill`s a model call."""

    outcome: str = "ok"
    cost: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    span: trace.Span = field(default=trace.INVALID_SPAN, repr=False)

    def bill(self, cost: float | None, input_tokens: int | None = 0, output_tokens=0) -> None:
        """Adds one billed response: a run bills each as it arrives."""
        self.cost += cost or 0.0
        self.input_tokens += input_tokens or 0
        self.output_tokens += output_tokens or 0


def model_label(model: str) -> str:
    """`model` as a metric label: the first MAX_MODELS distinct IDs keep their
    names, later ones are "other". Spans keep the full ID."""
    if model in _models:
        return model
    # Not atomic, and needn't be: every caller records on the event loop thread
    # (the API's, or `index`'s asyncio.run). Recording from a worker thread would
    # need a lock here.
    if len(_models) < MAX_MODELS:
        _models.add(model)
        return model
    return "other"


# A call given up on (indexing a reply stopped waiting for, a job past its
# deadline) has no result: its span says so, and no metric counts it.
CANCELLED = "cancelled"


def call_result(error: BaseException) -> str:
    if isinstance(error, asyncio.CancelledError):
        return CANCELLED
    # openai's and httpx's timeouts aren't TimeoutErrors.
    timeout = isinstance(error, TimeoutError) or "Timeout" in type(error).__name__
    return "timeout" if timeout else "error"


@contextlib.contextmanager
def llm_call(
    purpose: str, model: str, *, span: str = "", call: Call | None = None
) -> Iterator[Call]:
    """One model call as kiki.llm.* (`purpose` and the model actually called) and a
    span with its cost and tokens, billed so far even if it fails. An exception
    makes it an error or a timeout; cancelled, it is only a span. `call` is one
    already being billed (a run's, by its model)."""
    call = call or Call()
    if (recorder := _recorder) is None:
        yield call
        return
    labels = {"purpose": purpose, "model": model_label(model)}
    started = time.monotonic()
    with recorder.span(
        span or f"llm {purpose}", {"kiki.purpose": purpose, "gen_ai.request.model": model}
    ) as call.span:
        try:
            yield call
        except BaseException as error:
            call.outcome = call_result(error)
            raise
        finally:
            if call.outcome != CANCELLED:
                recorder.llm_calls.add(1, {**labels, "result": call.outcome})
                recorder.llm_duration.record(time.monotonic() - started, labels)
            if call.cost:
                recorder.llm_cost.add(call.cost, labels)
            for kind, tokens in (("input", call.input_tokens), ("output", call.output_tokens)):
                if tokens:
                    recorder.llm_tokens.add(tokens, {**labels, "type": kind})
            call.span.set_attributes(
                {
                    "kiki.llm.result": call.outcome,
                    "kiki.cost_usd": call.cost,
                    "gen_ai.usage.input_tokens": call.input_tokens,
                    "gen_ai.usage.output_tokens": call.output_tokens,
                }
            )


@contextlib.contextmanager
def image_attempt(
    model: str, failed_as: Callable[[BaseException], str], *, fallback: bool = False
) -> Iterator[Call]:
    """One image model attempt: an `llm_call` (purpose image) and kiki.images.*.
    `failed_as` names a failure's result (safety_blocked, refused or failed);
    `fallback` marks the fallback model's attempt after a safety block."""
    result = "drawn"
    started = time.monotonic()
    with llm_call("image", model, span="image attempt") as call:
        try:
            yield call
        except BaseException as error:
            result = CANCELLED if isinstance(error, asyncio.CancelledError) else failed_as(error)
            raise
        finally:
            if (recorder := _recorder) is not None and result != CANCELLED:
                label = model_label(model)
                recorder.image_attempts.add(1, {"model": label, "result": result})
                recorder.image_duration.record(time.monotonic() - started, {"model": label})
                if fallback:
                    recorder.image_fallback.add(
                        1, {"result": "drawn" if result == "drawn" else "failed"}
                    )
                call.span.set_attribute("kiki.image.result", result)


@contextlib.contextmanager
def tool_call(tool: str) -> Iterator[Call]:
    """One tool call as kiki.tools.* and a span. The caller sets the outcome; an
    exception makes it an error (cancelled: only a span). One turned away
    (budget_exhausted, out_of_time) never ran, so it has no duration."""
    call = Call()
    if (recorder := _recorder) is None:
        yield call
        return
    started = time.monotonic()
    with recorder.span(f"execute_tool {tool}", {"gen_ai.tool.name": tool}) as call.span:
        try:
            yield call
        except BaseException as error:
            call.outcome = CANCELLED if isinstance(error, asyncio.CancelledError) else "error"
            raise
        finally:
            if call.outcome != CANCELLED:
                recorder.tool_calls.add(1, {"tool": tool, "outcome": call.outcome})
            if call.outcome not in (CANCELLED, "budget_exhausted", "out_of_time"):
                recorder.tool_duration.record(time.monotonic() - started, {"tool": tool})
            call.span.set_attribute("kiki.tool.outcome", call.outcome)


def job_answered(kind: str, outcome: str) -> None:
    if _recorder is not None:
        _recorder.jobs.add(1, {"kind": kind, "outcome": outcome})


def job_ran(kind: str, seconds: float) -> None:
    if _recorder is not None:
        _recorder.job_duration.record(seconds, {"kind": kind})


def job_rejected(reason: str) -> None:
    if _recorder is not None:
        _recorder.jobs_rejected.add(1, {"reason": reason})


def model_requests(requests: int) -> None:
    if _recorder is not None:
        _recorder.model_requests.record(requests)


def web_calls(searches: int, fetches: int) -> None:
    if _recorder is not None:
        for kind, calls in (("search", searches), ("fetch", fetches)):
            if calls:
                _recorder.web_calls.add(calls, {"kind": kind})
