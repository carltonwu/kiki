"""Message history tools: search the server and read around a message."""

import asyncio
import contextlib
import logging
from datetime import datetime
from typing import Annotated

from psycopg import OperationalError
from pydantic import BeforeValidator
from pydantic_ai import RunContext
from pydantic_ai.toolsets import FunctionToolset

from ...chat.indexing import index_pending
from ...chat.search import Semantic, message_context, search
from ..deps import Deps
from ..guard import guarded

logger = logging.getLogger(__name__)


# Some models fill every optional argument, sending "" for a time they mean to leave
# out. Rejecting it costs a tool retry, and a second slip fails the whole reply.
OptionalTime = Annotated[
    datetime | None, BeforeValidator(lambda v: None if isinstance(v, str) and not v.strip() else v)
]


async def search_messages(
    ctx: RunContext[Deps],
    query: str,
    person: str | None = None,
    channel: str | None = None,
    before: OptionalTime = None,
    after: OptionalTime = None,
    limit: int = 10,
) -> dict:
    """Search the message history of every channel in this server (by meaning and by keywords).

    Recent matches rank slightly higher unless `before`/`after` is given. Older
    messages already shown to you in this conversation are not returned.

    Args:
        query: What to look for, in natural language or keywords.
        person: Only messages written by this person: a ref like u2 (best), an
            @username, or a name they've gone by. Names match exactly, so "Chris"
            won't find "Chrissy"; if several people share the name, results say so.
        channel: Only messages in this channel, like #general. Leave it out to
            search the whole server.
        before: Only messages sent before this time (ISO 8601, UTC).
        after: Only messages sent at or after this time (ISO 8601, UTC).
        limit: Maximum results, up to 10.
    """

    async def work():
        deps = ctx.deps
        if deps.indexing is None:
            deps.indexing = asyncio.create_task(index_guild(deps))
        # Shielded: a search timing out must not cancel indexing for the next one.
        semantic = await asyncio.shield(deps.indexing)
        return await search(
            deps.connection,
            deps.embed,
            deps.scope,
            query,
            person=person,
            channel=channel,
            roster=deps.roster,
            before=before,
            after=after,
            limit=limit,
            semantic=semantic,
        )

    return await guarded(ctx, "search_messages", work)


async def get_message_context(
    ctx: RunContext[Deps], message_id: str, before: int = 5, after: int = 5
) -> dict:
    """Fetch the messages around one message in its channel, plus what it replied to.

    Args:
        message_id: The `id` of a message from search results.
        before: Messages before it, up to 10.
        after: Messages after it, up to 10.
    """

    async def work():
        if not message_id.isdigit():
            raise ValueError("message_id must be a message ID from search results")
        return await message_context(
            ctx.deps.connection, ctx.deps.scope, int(message_id), before, after, ctx.deps.roster
        )

    return await guarded(ctx, "get_message_context", work)


async def index_guild(deps: Deps) -> Semantic:
    try:
        found, more = await index_pending(deps.connection, deps.embed, guild_id=deps.scope.guild_id)
    except OperationalError:
        raise
    except Exception as error:  # noqa: BLE001 - preserve search without logging provider payloads
        logger.warning(
            "indexing failed job_id=%s guild_id=%s error_type=%s; using partial index",
            deps.job_id,
            deps.scope.guild_id,
            type(error).__name__,
        )
        return "partial"
    logger.debug(
        "indexed guild job_id=%s guild_id=%s messages=%d more_pending=%s",
        deps.job_id,
        deps.scope.guild_id,
        found,
        more is not None,
    )
    return "partial" if more else "full"


async def finish(task: asyncio.Task) -> None:
    """Cancels indexing the run didn't wait for: the reply shouldn't wait either.
    Saved batches stay saved; the rest is indexed by a later search."""
    if not task.done():
        task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task


toolset = FunctionToolset([search_messages, get_message_context])
