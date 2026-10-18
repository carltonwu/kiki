"""Durable results and session locks, with connections only during a job."""

import logging
from collections.abc import Awaitable, Callable

from psycopg.types.json import Jsonb

from .. import db
from ..contracts import ImageJobRequest, ImageJobResult, JobRequest, JobResponse, JobResult
from ..executor import Executor, TransientError

logger = logging.getLogger(__name__)


class RequestConflict(Exception):
    pass


async def run_job(database_url: str, request: JobRequest, executor: Executor) -> JobResponse:
    async def execute() -> JobResult:
        result = JobResult.model_validate(await executor.execute(request))
        if not result.text.strip():
            raise ValueError("executor returned an empty reply")
        return result

    return await durable(database_url, request, request.kind, execute)


async def run_image_job(
    database_url: str, request: ImageJobRequest, executor: Executor
) -> JobResponse:
    async def execute() -> ImageJobResult:
        return ImageJobResult.model_validate(await executor.draw(request))

    return await durable(database_url, request, "image", execute)


async def durable(
    database_url: str,
    request: JobRequest | ImageJobRequest,
    kind: str,
    execute: Callable[[], Awaitable[JobResult | ImageJobResult]],
) -> JobResponse:
    """Runs a job at most once per ID: a retry gets the saved result or waits."""
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
            "SELECT request, status, result, error FROM agent_host_jobs WHERE id = %s",
            (request.id,),
        )
        previous = await cursor.fetchone()
        if previous:
            old_request, status, result, error = previous
            if old_request != payload:
                raise RequestConflict(request.id)
            if status != "running":
                logger.debug("reusing saved job result job_id=%s status=%s", request.id, status)
                return JobResponse(id=request.id, status=status, result=result, error=error)
            # The previous owner disconnected before saving a result. Inference
            # may repeat; Discord delivery remains solely Rust-owned.
            logger.info("resuming interrupted job job_id=%s kind=%s", request.id, kind)
        else:
            await connection.execute(
                "INSERT INTO agent_host_jobs (id, request, status) VALUES (%s, %s, 'running')",
                (request.id, Jsonb(payload)),
            )

        try:
            result = await execute()
        except TransientError:
            raise  # not persisted: a retry of the same ID runs it again
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

        await connection.execute(
            "UPDATE agent_host_jobs SET status = 'completed', result = %s, error = NULL, "
            "updated_at = now() WHERE id = %s",
            (Jsonb(result.model_dump(mode="json")), request.id),
        )
        return JobResponse(id=request.id, status="completed", result=result)
