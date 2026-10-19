"""Message history tools: search the server, read around a message, read a
channel in order, and count."""

import asyncio
import contextlib
import logging
from datetime import datetime
from typing import Annotated

from psycopg import OperationalError
from pydantic import BeforeValidator
from pydantic_ai import RunContext
from pydantic_ai.toolsets import FunctionToolset

from ... import db
from ...chat.counting import GroupBy, count
from ...chat.history import channel_history as read_history
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


async def channel_history(
    ctx: RunContext[Deps],
    channel: str | None = None,
    after: str | None = None,
    before: str | None = None,
    limit: int = 100,
    cursor: str | None = None,
) -> dict:
    """Read a channel's messages in time order, threads included: everything said
    between two points, not ranked matches.

    Use it to catch someone up ("what did I miss", "what happened last night",
    "summarize #general today") or to follow a stretch of conversation from start to
    finish. Use search_messages to find something specific, and get_message_context
    for just the few messages around one hit.

    With `after`, it reads forward from that point; without it, it returns the newest
    messages (before `before`, if given). Long stretches come in pages: when
    `cursor` is in the result, pass it back with the same other arguments for the
    next page. `total_in_range` says how many messages the whole stretch has.

    Args:
        channel: The channel to read, like #general. Leave it out for this channel.
        after: Only messages after this point: an ISO 8601 time (UTC unless it has an
            offset, like 2026-10-18T02:00-04:00) or a message ID.
        before: Only messages before this point: a time or a message ID, as `after`.
        limit: Maximum messages in this page, up to 100.
        cursor: The `cursor` from the previous page.
    """

    async def work():
        return await read_history(
            ctx.deps.connection,
            ctx.deps.scope,
            channel=channel,
            after=after,
            before=before,
            limit=limit,
            cursor=cursor,
            roster=ctx.deps.roster,
        )

    return await guarded(ctx, "channel_history", work)


async def count_messages(
    ctx: RunContext[Deps],
    query: str | None = None,
    pattern: str | None = None,
    person: str | None = None,
    channel: str | None = None,
    before: OptionalTime = None,
    after: OptionalTime = None,
    group_by: GroupBy | None = None,
    limit: int = 10,
) -> dict:
    """Count messages in this server exactly: totals and leaderboards.

    Use this, not search_messages, for "who says X the most", "how often does Y
    come up", "how many times", "who talks most in #channel" or trends over time:
    search only returns a few examples, so counting its hits is wrong. Returns the
    total and, with `group_by`, the top groups by count, each with an example
    message link to cite. Counts messages (not repeats within one), excluding bots
    and deleted messages.

    Args:
        query: Full-text words a message must all contain, matched by stem ("slay"
            also finds "slayed"). Supports "exact phrase", `or` and -word. Leave
            both query and pattern out to count every message.
        pattern: A case-insensitive regex the text must match, for exact
            spellings, short or common words, emoji or slang (`\\bslay\\b`).
            With a query, it narrows the query's matches, which is faster.
        person: Only messages by this person: a ref like u2 (best), an @username,
            or a name they've gone by.
        channel: Only messages in this channel, like #general.
        before: Only messages sent before this time (ISO 8601, UTC).
        after: Only messages sent at or after this time (ISO 8601, UTC).
        group_by: "person" or "channel" for a ranking, "month" or "year" (UTC)
            for counts over time. Leave it out for just the total.
        limit: How many top groups to return, up to 50.
    """

    async def work():
        # Its own connection: the count's statement timeout lives in a transaction,
        # which mustn't interleave with indexing on the run's shared one.
        async with await db.connect(ctx.deps.memory.database_url) as connection:
            return await count(
                connection,
                ctx.deps.scope,
                query=query,
                pattern=pattern,
                person=person,
                channel=channel,
                roster=ctx.deps.roster,
                before=before,
                after=after,
                group_by=group_by,
                limit=limit,
            )

    return await guarded(ctx, "count_messages", work)


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


toolset = FunctionToolset([search_messages, get_message_context, channel_history, count_messages])
