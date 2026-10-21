"""Unexpected failures, saved for the ingester to file as GitHub issues
(`fault_events`, docs/observability.md "Fault issues").

Code anywhere in a job notes a fault (`note`); the job saves what was noted
when it ends (`api/jobs.py`), on the connection it already holds, and a job
that fails adds its own failure (`classify`). The issues are public, so a fault
holds structured fields only: an exception's class name, never its message
(provider exceptions can contain credentials or private prompts), and nothing
from the conversation.

No SDK imports, like `deadline`: the API imports this at startup. Exceptions are
told apart by class name."""

import contextlib
import hashlib
import logging
import os
from collections.abc import Iterator
from contextvars import ContextVar
from dataclasses import dataclass

from opentelemetry import trace

logger = logging.getLogger(__name__)

# A job's catch-all failure: a bug, or an exception nothing else classifies.
EXECUTOR_FAILED = "executor_failed"
# The model's output broke the run: invalid tool calls past their retries, no output.
MODEL_MISBEHAVED = "model_misbehaved"
# The provider turned the request down (a 4xx other than 429).
PROVIDER_REJECTED = "provider_rejected"
# A tool raised something it doesn't handle (`agent/guard.py`).
TOOL_CRASHED = "tool_crashed"
# A drawing failed for something other than time or a refusal.
DRAWING_FAILED = "drawing_failed"
# A tool fell back quietly (lexical search, an unreadable link): filed only past
# a rate (`faults/reporter.rs`).
TOOL_DEGRADED = "tool_degraded"

# One job's noted faults beyond this are dropped: a loop that fails on every
# item counts once per item at most this often.
MAX_PER_JOB = 20

INSERT = """
INSERT INTO fault_events (fingerprint, service, kind, job_kind, error_type, stage, http_status,
                          model, tool, finish_reason, version, environment, trace_id, run_id)
VALUES (%s, 'agent', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""


@dataclass(frozen=True)
class Fault:
    kind: str
    error_type: str | None = None
    stage: str | None = None
    http_status: int | None = None
    model: str | None = None
    tool: str | None = None
    finish_reason: str | None = None
    trace_id: str | None = None

    def fingerprint(self) -> str:
        return fingerprint(
            "agent", self.kind, self.error_type, self.stage, self.http_status, self.tool
        )


def fingerprint(
    service: str,
    kind: str,
    error_type: str | None,
    stage: str | None,
    http_status: int | None,
    tool: str | None,
) -> str:
    """What makes two faults the same issue; the model isn't part of it, so one
    bug seen with three models is one issue. Rust's `faults::fingerprint` must
    match (both test the same value)."""
    identity = "|".join(
        "" if part is None else str(part)
        for part in (service, kind, error_type, stage, http_status, tool)
    )
    return hashlib.sha256(identity.encode()).hexdigest()[:16]


_noted: ContextVar[list[Fault] | None] = ContextVar("kiki_faults", default=None)
_stage: ContextVar[str | None] = ContextVar("kiki_fault_stage", default=None)


@contextlib.contextmanager
def collecting() -> Iterator[list[Fault]]:
    """Collects what the code inside notes, from tasks it starts too (they copy
    the context, and with it this list)."""
    faults: list[Fault] = []
    token = _noted.set(faults)
    try:
        yield faults
    finally:
        _noted.reset(token)


@contextlib.contextmanager
def stage(name: str) -> Iterator[None]:
    """Names where the code inside is, for faults noted in it and for an
    exception leaving it (`classify` reads that one off the exception: the
    context variable is reset by the time the job catches it)."""
    token = _stage.set(name)
    try:
        yield
    except Exception as error:
        annotate(error, stage=name)
        raise
    finally:
        _stage.reset(token)


def annotate(error: BaseException, **fields) -> None:
    """Fault fields for `classify` to find on `error`, as `stage` and
    `finish_reason`. The first value set wins: it is the innermost."""
    found = getattr(error, "kiki_fault", None)
    if found is None:
        found = {}
        with contextlib.suppress(AttributeError):
            error.kiki_fault = found
    for key, value in fields.items():
        if value is not None:
            found.setdefault(key, value)


def note(
    kind: str,
    *,
    error_type: str | None = None,
    stage: str | None = None,
    http_status: int | None = None,
    model: str | None = None,
    tool: str | None = None,
    finish_reason: str | None = None,
) -> None:
    """Notes a fault for the current job to save. Outside a job (a background
    refresh) it's only logged."""
    faults = _noted.get()
    if faults is None:
        logger.debug("fault outside a job kind=%s tool=%s", kind, tool or "-")
        return
    if len(faults) >= MAX_PER_JOB:
        return
    faults.append(
        Fault(
            kind,
            error_type=error_type,
            stage=stage or _stage.get(),
            http_status=http_status,
            model=model,
            tool=tool,
            finish_reason=finish_reason,
            trace_id=trace_id(),
        )
    )


def degraded(
    tool: str,
    stage: str,
    *,
    error: BaseException | None = None,
    http_status: int | None = None,
) -> None:
    """Notes a quiet fallback: an upstream that failed with `error` or answered
    `http_status`. Running out of time and rate limits are expected, not noted;
    nor are 4xx answers, which are about the request (a deleted post)."""
    if http_status == 429 or error is not None and timed_out(error):
        return
    if http_status is not None and http_status < 500:
        return
    note(
        TOOL_DEGRADED,
        error_type=type(error).__name__ if error is not None else None,
        stage=stage,
        http_status=http_status,
        tool=tool,
    )


def classify(error: BaseException) -> Fault:
    """The fault for a job's unexpected failure, from the exception and those it
    was raised from (an exception group's first member too)."""
    chain = list(causes(error))
    fields: dict = {}
    for link in chain:
        for key, value in (getattr(link, "kiki_fault", None) or {}).items():
            fields.setdefault(key, value)
    stage, finish_reason = fields.get("stage"), fields.get("finish_reason")
    for link in chain:
        names = class_names(link)
        status = getattr(link, "status_code", None)
        if "ModelHTTPError" in names and isinstance(status, int) and 400 <= status < 500:
            return Fault(
                PROVIDER_REJECTED,
                error_type=type(link).__name__,
                stage=stage,
                http_status=status,
                model=getattr(link, "model_name", None),
                trace_id=trace_id(),
            )
    for link in chain:
        if class_names(link) & {"UnexpectedModelBehavior", "FallbackExceptionGroup"}:
            return Fault(
                MODEL_MISBEHAVED,
                error_type=type(link).__name__,
                stage=stage,
                finish_reason=finish_reason,
                trace_id=trace_id(),
            )
    return Fault(EXECUTOR_FAILED, error_type=type(error).__name__, stage=stage, trace_id=trace_id())


def causes(error: BaseException, depth: int = 6) -> Iterator[BaseException]:
    seen: set[int] = set()
    pending = [error]
    while pending and len(seen) < depth:
        link = pending.pop(0)
        if id(link) in seen:
            continue
        seen.add(id(link))
        yield link
        if isinstance(link, BaseExceptionGroup) and link.exceptions:
            pending.append(link.exceptions[0])
        pending.extend(e for e in (link.__cause__, link.__context__) if e is not None)


def class_names(error: BaseException) -> set[str]:
    return {cls.__name__ for cls in type(error).__mro__}


def timed_out(error: BaseException) -> bool:
    """asyncio's and httpx's timeouts."""
    return isinstance(error, TimeoutError) or "TimeoutException" in class_names(error)


def trace_id() -> str | None:
    context = trace.get_current_span().get_span_context()
    return format(context.trace_id, "032x") if context.is_valid else None


async def save(connection, faults: list[Fault], *, job_kind: str, run_id: str, model: str) -> None:
    """Inserts a job's faults. Never raises: reporting can't fail a job."""
    if not faults:
        return
    version = os.getenv("RAILWAY_GIT_COMMIT_SHA") or None
    environment = os.getenv("RAILWAY_ENVIRONMENT_NAME") or "local"
    rows = [
        (
            f.fingerprint(),
            f.kind,
            job_kind,
            f.error_type,
            f.stage,
            f.http_status,
            f.model or model or None,
            f.tool,
            f.finish_reason,
            version,
            environment,
            f.trace_id,
            run_id,
        )
        for f in faults
    ]
    try:
        async with connection.cursor() as cursor:
            await cursor.executemany(INSERT, rows)
    except Exception as error:  # noqa: BLE001 - a job's result matters more than its report
        logger.warning(
            "couldn't record faults job_id=%s faults=%d error_type=%s",
            run_id,
            len(rows),
            type(error).__name__,
        )
        return
    for f in faults:
        logger.info(
            "fault recorded job_id=%s kind=%s error_type=%s stage=%s tool=%s fingerprint=%s",
            run_id,
            f.kind,
            f.error_type or "-",
            f.stage or "-",
            f.tool or "-",
            f.fingerprint(),
        )
