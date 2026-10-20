"""Which chat models think, from OpenRouter's model catalog (`/api/v1/models`):
the ones that take its `reasoning` parameter."""

import logging
import time

import httpx

from ..config import openrouter_base

logger = logging.getLogger(__name__)

CACHE_SECONDS = 3600
RETRY_SECONDS = 60  # after a failed fetch
TIMEOUT_SECONDS = 3

_catalog: dict[str, frozenset[str]] = {}
_fetched_at = -float("inf")


async def fetch() -> dict[str, frozenset[str]]:
    """Model ID → its supported parameters (tests replace this)."""
    async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as http:
        response = await http.get(f"{openrouter_base()}/v1/models")
        response.raise_for_status()
        return {
            m["id"]: frozenset(m.get("supported_parameters") or []) for m in response.json()["data"]
        }


async def reasons(model: str) -> bool:
    """Whether `model` thinks. A model the catalog doesn't list, or a catalog
    that can't be read, counts as thinking: losing a reply to thinking is worse
    than a longer one."""
    global _catalog, _fetched_at
    age = time.monotonic() - _fetched_at
    if age > CACHE_SECONDS or (not _catalog and age > RETRY_SECONDS):
        # Set first: during an outage, replies don't each wait on the catalog.
        _fetched_at = time.monotonic()
        try:
            _catalog = await fetch()
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as error:
            logger.warning("model catalog unavailable error_type=%s", type(error).__name__)
    parameters = _catalog.get(model)
    return parameters is None or "reasoning" in parameters
