"""Which chat models think, from OpenRouter's model catalog (`/api/v1/models`):
the ones that take its `reasoning` parameter.

The catalog is fetched at startup and refreshed in the background (`refresh`);
a reply only reads the copy it has (`reasons`), so it never waits on OpenRouter.
A model it doesn't know, before the first fetch or after a failed one, counts as
thinking: losing a reply to thinking is worse than a longer one."""

import asyncio
import logging
import time

import httpx

from ..config import openrouter_base

logger = logging.getLogger(__name__)

CACHE_SECONDS = 3600
RETRY_SECONDS = 60  # after a failed fetch, or for a model the copy doesn't list
TIMEOUT_SECONDS = 10

_catalog: dict[str, frozenset[str]] = {}
_fetched_at = -float("inf")  # last successful fetch
_attempted_at = -float("inf")  # last fetch started, successful or not
_refreshing: asyncio.Task | None = None


async def fetch() -> dict[str, frozenset[str]]:
    """Model ID → its supported parameters (tests replace this)."""
    async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as http:
        response = await http.get(f"{openrouter_base()}/v1/models")
        response.raise_for_status()
        return {
            m["id"]: frozenset(m.get("supported_parameters") or []) for m in response.json()["data"]
        }


async def refresh() -> None:
    """Fetches the catalog; a failure keeps the copy it has, and is retried after
    `RETRY_SECONDS` (not a whole `CACHE_SECONDS`)."""
    global _catalog, _fetched_at, _attempted_at
    _attempted_at = time.monotonic()
    try:
        _catalog = await fetch()
        _fetched_at = time.monotonic()
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as error:
        logger.warning("model catalog unavailable error_type=%s", type(error).__name__)


def due(model: str, now: float) -> bool:
    """Whether to fetch again: the copy is old, or doesn't list `model` (a new
    one), and no fetch started in the last `RETRY_SECONDS`."""
    if now - _attempted_at < RETRY_SECONDS:
        return False
    return now - _fetched_at > CACHE_SECONDS or model not in _catalog


def reasons(model: str) -> bool:
    """Whether `model` thinks, from the copy at hand; starts a background refresh
    when one is due (call it on the event loop)."""
    global _refreshing
    if due(model, time.monotonic()) and (_refreshing is None or _refreshing.done()):
        _refreshing = asyncio.get_running_loop().create_task(refresh())
    parameters = _catalog.get(model)
    return parameters is None or "reasoning" in parameters
