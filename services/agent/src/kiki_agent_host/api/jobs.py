"""Durable results and session locks, with connections only during a job."""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from psycopg.types.json import Jsonb

from .. import db, telemetry
from ..contracts import ImageJobRequest, ImageJobResult, JobRequest, JobResponse, JobResult
from ..deadline import run_deadline
from ..executor import Executor, TransientError

logger = logging.getLogger(__name__)

# A job's error when it ran out of time. Rust reads it (`host::TIMED_OUT`): it is
# final, so a retry of the same ID doesn't run the whole job again.
TIMED_OUT = "timed out"
# A job with less work time than this left isn't started, or retried, again.
MIN_RESUME_SECONDS = 5
# The work's deadline is this much before the job's: the work is cancelled there,
# and saving its result (or the timeout) uses the rest. Tools, model requests and
# drawing attempts cut their timeouts to it (`deadline.time_left`), so a slow
# reply still answers and a slow drawing still writes its note.
JOB_MARGIN_SECONDS = 10


class RequestConflict(Exception):
    pass


async def run_job(
    database_url: str, request: JobRequest, executor: Executor, *, timeout: float | None = None
) -> JobResponse:
    async def execute() -> JobResult:
        result = JobResult.model_validate(await executor.execute(request))
        if not result.text.strip():
            raise ValueError("executor returned an empty reply")
        return result

    return await durable(database_url, request, request.kind, execute, timeout)


async def run_image_job(
    database_url: str, request: ImageJobRequest, executor: Executor, *, timeout: float | None = None
) -> JobResponse:
    async def execute() -> ImageJobResult:
        return ImageJobResult.model_validate(await executor.draw(request))

    return await durable(database_url, request, "image", execute, timeout)


async def durable(
    database_url: str,
    request: JobRequest | ImageJobRequest,
    kind: str,
    execute: Callable[[], Awaitable[JobResult | ImageJobResult]],
    timeout: float | None = None,
) -> JobResponse:
    """Runs a job at most once per ID: a retry gets the saved result or waits.

    `timeout` counts from the job's first attempt, so an interrupted job resumed
    by a retry gets only what is left of it, inside Rust's deadline for the job
    (AGENT_HOST_TIMEOUT_MS). Running out of it is the job's final result."""
    # A session lock survives short transactions and releases on disconnect.
    async with await db.connect(database_url) as connection:
        cursor = await connection.execute(
            "SELECT pg_try_advisory_lock(hashtextextended(%s, 0))", (request.id,)
        )
        row = await cursor.fetchone()
        if not row or not row[0]:
            logger.debug("job already running job_id=%s", request.id)
            return JobResponse(id=request.id, status="running")

        # Unset optional fields (image_model) are left out, as Rust omits them, so
        # requests saved before a field existed still match on retry.
        payload = request.model_dump(mode="json", exclude_none=True)
        cursor = await connection.execute(
            "SELECT request, status, result, error, "
            "extract(epoch FROM now() - created_at)::float8 FROM agent_host_jobs WHERE id = %s",
            (request.id,),
        )
        previous = await cursor.fetchone()
        left = timeout
        if previous:
            old_request, status, result, error, age = previous
            if old_request != payload:
                raise RequestConflict(request.id)
            if status != "running":
                logger.debug("reusing saved job result job_id=%s status=%s", request.id, status)
                return JobResponse(id=request.id, status=status, result=result, error=error)
            # The previous owner disconnected before saving a result. Inference
            # may repeat; Discord delivery remains solely Rust-owned.
            if timeout is not None:
                left = timeout - max(age, 0)
            logger.info(
                "resuming interrupted job job_id=%s kind=%s left_s=%s",
                request.id,
                kind,
                "-" if left is None else int(left),
            )
        else:
            await connection.execute(
                "INSERT INTO agent_host_jobs (id, request, status) VALUES (%s, %s, 'running')",
                (request.id, Jsonb(payload)),
            )

        started = time.monotonic()
        work = None if left is None else left - JOB_MARGIN_SECONDS

        def out_of_time() -> bool:
            """Whether the job's work time is used up, rather than some step's."""
            return work is not None and work - (time.monotonic() - started) < MIN_RESUME_SECONDS

        try:
            if previous and out_of_time():
                raise TimeoutError
            # Cancels the work at its deadline; saving the result is outside it.
            async with asyncio.timeout(work):
                with run_deadline(work):
                    result = await execute()
        except TransientError:
            raise  # not persisted: a retry of the same ID runs it again
        except TimeoutError:
            if not out_of_time():
                # A step's own timeout (a model request waiting out a 429's
                # Retry-After, say) with job time left: retry like a provider error.
                logger.info(
                    "job step timed out job_id=%s kind=%s; retry the same ID", request.id, kind
                )
                raise TransientError("a step timed out") from None
            logger.warning(
                "job timed out job_id=%s kind=%s channel_id=%s conversation_id=%s latency_ms=%d",
                request.id,
                kind,
                request.scope.channel_id,
                request.scope.conversation_id,
                (time.monotonic() - started) * 1000,
            )
            await connection.execute(
                "UPDATE agent_host_jobs SET status = 'failed', error = %s, updated_at = now() "
                "WHERE id = %s",
                (TIMED_OUT, request.id),
            )
            return JobResponse(id=request.id, status="failed", error=TIMED_OUT)
        except Exception as error:  # noqa: BLE001 - isolate framework failures without leaking payloads
            # Provider exceptions can contain credentials or private prompts.
            logger.error(
                "executor failed job_id=%s kind=%s channel_id=%s conversation_id=%s error_type=%s",
                request.id,
                kind,
                request.scope.channel_id,
                request.scope.conversation_id,
                type(error).__name__,
            )
            error = "executor failed"
            await connection.execute(
                "UPDATE agent_host_jobs SET status = 'failed', error = %s, updated_at = now() "
                "WHERE id = %s",
                (error, request.id),
            )
            return JobResponse(id=request.id, status="failed", error=error)
        finally:
            # Each attempt that ran the job: a retry answered from the saved
            # result, or turned away as running, didn't.
            telemetry.job_ran(kind, time.monotonic() - started)

        await connection.execute(
            "UPDATE agent_host_jobs SET status = 'completed', result = %s, error = NULL, "
            "updated_at = now() WHERE id = %s",
            (Jsonb(result.model_dump(mode="json")), request.id),
        )
        return JobResponse(id=request.id, status="completed", result=result)
