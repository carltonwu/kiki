"""HTTP admission: no database polling, background workers, or idle telemetry."""

import asyncio
import logging
import resource
import secrets
import time
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from psycopg import OperationalError

from .. import STARTED
from ..config import Settings
from ..contracts import (
    DescribeEmojisRequest,
    DescribeEmojisResult,
    ImageJobRequest,
    JobRequest,
    JobResponse,
)
from ..executor import Executor, TransientError
from .jobs import RequestConflict, run_image_job, run_job

logger = logging.getLogger(__name__)


def create_app(settings: Settings, executor: Executor | None = None) -> FastAPI:
    app = FastAPI(title="Kiki agent host", docs_url=None, redoc_url=None, openapi_url=None)
    slots = asyncio.Semaphore(settings.max_concurrent_jobs)
    bearer = HTTPBearer(auto_error=False)

    async def authenticate(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ) -> None:
        if credentials is None or not secrets.compare_digest(
            credentials.credentials.encode(), settings.token.encode()
        ):
            raise HTTPException(status_code=401, detail="invalid host credentials")

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok", "executor_configured": executor is not None}

    async def admit(request: JobRequest | ImageJobRequest, kind: str, run) -> JSONResponse:
        """One durable job: a slot, the host deadline, and the retry contract."""
        if executor is None:
            raise HTTPException(
                status_code=501, detail="agent framework executor is not configured"
            )
        try:
            await asyncio.wait_for(slots.acquire(), timeout=0.1)
        except TimeoutError:
            raise HTTPException(status_code=503, detail="agent host is busy") from None
        started = time.monotonic()
        try:
            async with asyncio.timeout(settings.job_timeout_seconds):
                result = await run(settings.database_url, request, executor)
            logger.debug(
                "job response job_id=%s kind=%s channel_id=%s conversation_id=%s status=%s "
                "latency_ms=%d rss_mb=%d",
                request.id,
                kind,
                request.scope.channel_id,
                request.scope.conversation_id,
                result.status,
                (time.monotonic() - started) * 1000,
                max_rss_mb(),
            )
            return JSONResponse(
                status_code=202 if result.status == "running" else 200,
                content=result.model_dump(mode="json"),
            )
        except RequestConflict:
            logger.warning("job input conflict job_id=%s kind=%s", request.id, kind)
            raise HTTPException(
                status_code=409, detail="job ID already has different input"
            ) from None
        except (OperationalError, TimeoutError, TransientError) as error:
            logger.warning(
                "job interrupted job_id=%s kind=%s channel_id=%s conversation_id=%s "
                "error_type=%s latency_ms=%d; retry the same ID",
                request.id,
                kind,
                request.scope.channel_id,
                request.scope.conversation_id,
                type(error).__name__,
                (time.monotonic() - started) * 1000,
            )
            raise HTTPException(
                status_code=503, detail="job interrupted; retry the same ID"
            ) from None
        finally:
            slots.release()

    @app.post("/v1/jobs", response_model=JobResponse, dependencies=[Depends(authenticate)])
    async def execute(request: JobRequest):
        return await admit(request, request.kind, run_job)

    @app.post("/v1/images", response_model=JobResponse, dependencies=[Depends(authenticate)])
    async def draw(request: ImageJobRequest):
        """A drawing a reply queued (docs/image-generation.md); durable like jobs."""
        return await admit(request, "image", run_image_job)

    @app.post(
        "/v1/emojis/describe",
        response_model=DescribeEmojisResult,
        dependencies=[Depends(authenticate)],
    )
    async def describe_emojis(request: DescribeEmojisRequest):
        """One batch of a server's custom emoji descriptions. Not durable: the
        writes are idempotent, and Rust repeats the call until none are left."""
        if executor is None:
            raise HTTPException(
                status_code=501, detail="agent framework executor is not configured"
            )
        try:
            await asyncio.wait_for(slots.acquire(), timeout=0.1)
        except TimeoutError:
            raise HTTPException(status_code=503, detail="agent host is busy") from None
        try:
            async with asyncio.timeout(settings.job_timeout_seconds):
                return await executor.describe_emojis(request)
        except (OperationalError, TimeoutError, TransientError) as error:
            logger.warning(
                "emoji descriptions interrupted guild_id=%s error_type=%s; retry",
                request.guild_id,
                type(error).__name__,
            )
            raise HTTPException(status_code=503, detail="interrupted; retry") from None
        finally:
            slots.release()

    return app


def max_rss_mb() -> int:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024


def application() -> FastAPI:
    """Uvicorn factory. Without an OpenRouter key, jobs return 501."""
    settings = Settings.from_env()
    executor = None
    if settings.openrouter_api_key:
        from ..agent.executor import PydanticExecutor  # the SDK import dominates startup time

        executor = PydanticExecutor(
            settings.database_url,
            settings.openrouter_api_key,
            settings.openrouter_vision_model,
            settings.discord_bot_token,
            image_fallback_model=settings.openrouter_image_fallback_model,
        )
    logger.info(
        "agent host ready startup_ms=%d rss_mb=%d executor_configured=%s "
        "max_concurrent_jobs=%d job_timeout_seconds=%s vision_model=%s discord_links=%s",
        (time.monotonic() - STARTED) * 1000,
        max_rss_mb(),
        executor is not None,
        settings.max_concurrent_jobs,
        settings.job_timeout_seconds,
        settings.openrouter_vision_model or "chat model",
        "refreshed" if settings.discord_bot_token else "as stored",
    )
    return create_app(settings, executor)
