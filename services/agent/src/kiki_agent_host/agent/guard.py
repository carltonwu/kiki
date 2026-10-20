"""Every tool call runs through `guarded`: a deadline, a per-reply budget, one log
line, and its telemetry (kiki.tools.*, a span). Errors the model can act on come
back as `{"error": ...}` results.

A reply also has a run deadline (`deadline.run_deadline`), inside the host's job timeout:
a tool's own deadline is cut to what the run has left after keeping
`ANSWER_RESERVE_SECONDS` for the final answer, and a tool called with less than
that left doesn't run. So a slow run still answers with what it has instead of
hitting the job timeout and saying nothing. Pydantic AI's own per-tool `timeout`
isn't used: it turns a timeout into a retry prompt, which spends the tool's
retries, and running out of those fails the whole reply."""

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from pydantic_ai import RunContext

from .. import telemetry
from ..deadline import run_deadline, time_left
from ..media.vision import ImageRefused
from .deps import Deps

logger = logging.getLogger(__name__)

TOOL_TIMEOUT_SECONDS = 10
# Kept for the last turn when a tool would run into the run deadline: up to two
# model requests, one that thinks until its limit (about 31 s at most in prod) and
# then the same without thinking (`executor.UNTHINKING_SECONDS`, which the first
# leaves it).
ANSWER_RESERVE_SECONDS = 45
# Below this a tool isn't worth starting.
MIN_TOOL_SECONDS = 1.0
OUT_OF_TIME = "Out of time for lookups. Answer now with what you have."


def out_of_lookup_time() -> bool:
    """Whether a tool called now would be turned away for the run deadline."""
    left = time_left()
    return left is not None and left - ANSWER_RESERVE_SECONDS < MIN_TOOL_SECONDS


def time_for_another_turn() -> bool:
    """Whether the run can still afford a whole last turn (`ANSWER_RESERVE_SECONDS`),
    as an output validator's retry is."""
    left = time_left()
    return left is None or left >= ANSWER_RESERVE_SECONDS


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
    deadline starts; the run deadline bounds the wait for it too. Inside, the
    tool's own deadline is the job's (`deadline.time_left`), so its steps (a
    vision call and its retry, say) can share it out.

    A call turned away for the run deadline is `out_of_time`, and doesn't count
    against the tool budget: it never ran."""
    deps = ctx.deps
    left = time_left()
    cap = None if left is None else left - ANSWER_RESERVE_SECONDS
    with telemetry.tool_call(name) as call:
        if cap is not None and cap < MIN_TOOL_SECONDS:
            logger.info("out of time for a tool job_id=%s tool=%s", deps.job_id, name)
            call.outcome = "out_of_time"
            return {"error": OUT_OF_TIME}
        if budget:
            deps.spent[budget.name] += 1
            if deps.spent[budget.name] > budget.calls:
                logger.debug(
                    "%s budget exhausted job_id=%s tool=%s",
                    budget.name.lower(),
                    deps.job_id,
                    name,
                )
                call.outcome = "budget_exhausted"
                return {"error": f"{budget.name} budget used up. Answer with what you have."}
        started = time.monotonic()
        try:
            async with asyncio.timeout(cap), lock or contextlib.nullcontext():
                own = timeout if cap is None else min(timeout, cap - (time.monotonic() - started))
                async with asyncio.timeout(own):
                    with run_deadline(own):
                        result = await work()
        except TimeoutError:
            call.outcome = "timeout"
            result = {"error": "Timed out."}
        except ImageRefused as e:
            call.outcome = "refused"
            result = {"error": str(e)}
        except ValueError as e:
            call.outcome = "invalid_arguments"
            result = {"error": str(e)}
        if call.outcome == "ok" and "error" in result:
            call.outcome = "error"
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
        call.outcome,
    )
    return result
