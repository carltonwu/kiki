"""Every tool call runs through `guarded`: a deadline, a per-reply budget, and one
log line. Errors the model can act on come back as `{"error": ...}` results."""

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from pydantic_ai import RunContext

from ..media.vision import ImageRefused
from .deps import Deps

logger = logging.getLogger(__name__)

TOOL_TIMEOUT_SECONDS = 10


@dataclass(frozen=True)
class Budget:
    """How many calls a group of tools gets per reply. Groups don't share calls,
    so filing a memory doesn't cost a search."""

    name: str
    calls: int


LOOKUPS = Budget("Tool", 4)


async def guarded(
    ctx: RunContext[Deps],
    name: str,
    work: Callable[[], Awaitable[dict]],
    *,
    budget: Budget | None = LOOKUPS,
    timeout: float = TOOL_TIMEOUT_SECONDS,
    lock: asyncio.Lock | None = None,
) -> dict:
    """Runs a tool with a deadline. Tools without a budget (generate_image) have
    their own cap, so searching can't block a drawing. `lock` is taken before the
    deadline starts."""
    deps = ctx.deps
    if budget:
        deps.spent[budget.name] += 1
        if deps.spent[budget.name] > budget.calls:
            logger.debug(
                "%s budget exhausted job_id=%s tool=%s", budget.name.lower(), deps.job_id, name
            )
            return {"error": f"{budget.name} budget used up. Answer with what you have."}
    started = time.monotonic()
    outcome = "ok"
    try:
        async with lock or contextlib.nullcontext(), asyncio.timeout(timeout):
            result = await work()
    except TimeoutError:
        outcome = "timeout"
        result = {"error": "Timed out."}
    except ImageRefused as e:
        outcome = "refused"
        result = {"error": str(e)}
    except ValueError as e:
        outcome = "invalid_arguments"
        result = {"error": str(e)}
    hits = next(
        (result[k] for k in ("messages", "images", "emojis", "entries", "groups") if k in result),
        [],
    )
    logger.info(
        "tool completed job_id=%s tool=%s channel_id=%s conversation_id=%s "
        "latency_ms=%d hits=%d semantic=%s outcome=%s",
        deps.job_id,
        name,
        deps.scope.channel_id,
        deps.scope.conversation_id,
        (time.monotonic() - started) * 1000,
        len(hits),
        result.get("semantic", "-"),
        "error" if outcome == "ok" and "error" in result else outcome,
    )
    return result
