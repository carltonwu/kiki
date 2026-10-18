"""HTTP admission: no database polling, background workers, or idle telemetry."""

import asyncio
import logging
import os
import resource
import secrets
import time
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from psycopg import OperationalError

from . import STARTED
from .contracts import DescribeEmojisRequest, DescribeEmojisResult, JobRequest, JobResponse
from .executor import Executor, TransientError
from .jobs import RequestConflict, run_job

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Settings:
    database_url: str
    token: str
    max_concurrent_jobs: int = 2
    job_timeout_seconds: float = 120
    openrouter_api_key: str = ""
    openrouter_vision_model: str = ""

    def __post_init__(self) -> None:
        if not self.database_url.strip() or not self.token.strip():
            raise ValueError("DATABASE_URL and AGENT_HOST_TOKEN must be nonempty")
        if self.max_concurrent_jobs < 1 or self.job_timeout_seconds <= 0:
            raise ValueError("job concurrency and timeout must be positive")

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            database_url=os.environ["DATABASE_URL"],
            token=os.environ["AGENT_HOST_TOKEN"],
            max_concurrent_jobs=int(os.getenv("AGENT_HOST_MAX_CONCURRENT_JOBS", "2")),
            job_timeout_seconds=float(os.getenv("AGENT_HOST_JOB_TIMEOUT_SECS", "120")),
            openrouter_api_key=os.getenv("OPENROUTER_CHAT_API_KEY", ""),
            openrouter_vision_model=os.getenv("OPENROUTER_VISION_MODEL", "").strip(),
        )


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

    @app.post("/v1/jobs", response_model=JobResponse, dependencies=[Depends(authenticate)])
    async def execute(request: JobRequest):
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
                result = await run_job(settings.database_url, request, executor)
            logger.debug(
                "job response job_id=%s kind=%s channel_id=%s conversation_id=%s status=%s "
                "latency_ms=%d rss_mb=%d",
                request.id,
                request.kind,
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
            logger.warning("job input conflict job_id=%s kind=%s", request.id, request.kind)
            raise HTTPException(
                status_code=409, detail="job ID already has different input"
            ) from None
        except (OperationalError, TimeoutError, TransientError) as error:
            logger.warning(
                "job interrupted job_id=%s kind=%s channel_id=%s conversation_id=%s "
                "error_type=%s latency_ms=%d; retry the same ID",
                request.id,
                request.kind,
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
        from .agent import PydanticExecutor  # the SDK import dominates startup time

        executor = PydanticExecutor(
            settings.database_url,
            settings.openrouter_api_key,
            settings.openrouter_vision_model,
        )
    logger.info(
        "agent host ready startup_ms=%d rss_mb=%d executor_configured=%s "
        "max_concurrent_jobs=%d job_timeout_seconds=%s vision_model=%s",
        (time.monotonic() - STARTED) * 1000,
        max_rss_mb(),
        executor is not None,
        settings.max_concurrent_jobs,
        settings.job_timeout_seconds,
        settings.openrouter_vision_model or "chat model",
    )
    return create_app(settings, executor)
