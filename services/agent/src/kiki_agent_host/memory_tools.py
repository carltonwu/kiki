"""Memory tools for the chat agent (docs/subject-memory-hld.md), kept apart from
the search tools in `agent`. They read `scope`, `roster` and `job_id` from the
run's deps, and keep their own connection and budget in `deps.memory`."""

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Literal

from psycopg import AsyncConnection
from pydantic_ai import Agent, RunContext

from . import memory

logger = logging.getLogger(__name__)

MAX_CALLS = 6  # separate from the search budget: filing a memory shouldn't cost a search
TIMEOUT_SECONDS = 10


@dataclass
class Session:
    """One run's memory state. Its own connection: memory transactions must not
    interleave with background indexing on the run's shared one. The lock covers
    parallel tool calls."""

    database_url: str
    calls: int = 0
    connection: AsyncConnection | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def connect(self) -> AsyncConnection:
        if self.connection is None:
            self.connection = await AsyncConnection.connect(
                self.database_url, autocommit=True, connect_timeout=10
            )
        return self.connection

    async def close(self) -> None:
        if self.connection is not None:
            connection, self.connection = self.connection, None
            with contextlib.suppress(Exception):
                await connection.close()


async def run(ctx: RunContext[Any], name: str, operation) -> dict:
    deps, session = ctx.deps, ctx.deps.memory
    session.calls += 1
    if session.calls > MAX_CALLS:
        logger.debug("memory budget exhausted job_id=%s tool=%s", deps.job_id, name)
        return {"error": "Memory budget used up. Answer with what you have."}
    started = time.monotonic()
    outcome = "ok"
    try:
        async with session.lock, asyncio.timeout(TIMEOUT_SECONDS):
            try:
                result = await operation(await session.connect())
            except asyncio.CancelledError:
                # Timed out mid-query: the connection's state is unknown, so the
                # next memory call opens a fresh one.
                await session.close()
                raise
    except TimeoutError:
        outcome, result = "timeout", {"error": "Timed out."}
    except ValueError as e:
        outcome, result = "invalid_arguments", {"error": str(e)}
    logger.info(
        "tool completed job_id=%s tool=%s channel_id=%s conversation_id=%s "
        "latency_ms=%d hits=%d outcome=%s",
        deps.job_id,
        name,
        deps.scope.channel_id,
        deps.scope.conversation_id,
        (time.monotonic() - started) * 1000,
        len(result.get("entries", [])),
        "error" if outcome == "ok" and "error" in result else outcome,
    )
    return result


async def recall(ctx: RunContext[Any], subject: str, doc: str | None = None) -> dict:
    """Read what you remember about a person or thing: their base profile, or one of their docs.

    The result also lists all of the subject's docs (title, what belongs there, size), so
    you can read a more specific one next.

    Args:
        subject: A person's ref like u2 (best), an @username or a name they've gone by; or
            the name of a thing (a pet, a place, a game, an inside joke).
        doc: Which doc to read. Leave it out for the base profile.
    """
    return await run(
        ctx,
        "recall",
        lambda db: memory.recall(db, ctx.deps.scope, ctx.deps.roster, subject, doc),
    )


async def search_memory(ctx: RunContext[Any], query: str, subject: str | None = None) -> dict:
    """Search your memory docs about everyone and everything in this server, by keywords.

    Args:
        query: What to look for.
        subject: Only this person or thing's docs.
    """
    return await run(
        ctx,
        "search_memory",
        lambda db: memory.search(db, ctx.deps.scope, ctx.deps.roster, query, subject),
    )


async def remember(
    ctx: RunContext[Any],
    subject: str,
    text: str,
    kind: Literal["fact", "opinion"],
    doc: str | None = None,
    replaces: str | None = None,
    sources: list[str] | None = None,
) -> dict:
    """Save one thing you learned about a person or thing, so you know it in later conversations.

    One claim per call, written so it makes sense on its own ("works night shifts at the
    hospital", "thinks the Eras tour was overrated"). Leave out `doc` to have it filed in
    the subject's best-matching doc. The result names where it went and similar entries
    already saved.

    Changing a saved entry (`replaces`) needs evidence in proportion to how settled it is:
    opinions change with one message; facts that have stood for months need several.
    Without `sources`, the evidence is the subject's latest message in this conversation.

    Args:
        subject: A person's ref like u2 (best), an @username or a name; or a thing's name.
        text: The claim, at most 60 words, in your own words.
        kind: "fact" for things that are true of them (job, pets, history, relationships);
            "opinion" for views, tastes and feelings, theirs or the group's about them.
        doc: A doc of the subject to put it in, from recall's list.
        replaces: The ID (like e12) of an entry this corrects or updates. Same text
            confirms that entry instead.
        sources: Message IDs from search results that show it.
    """
    return await run(
        ctx,
        "remember",
        lambda db: memory.remember(
            db,
            ctx.deps.scope,
            ctx.deps.roster,
            subject,
            text,
            kind,
            doc=doc,
            replaces=replaces,
            sources=sources,
            job_id=ctx.deps.job_id,
        ),
    )


async def forget(ctx: RunContext[Any], entry: str, sources: list[str] | None = None) -> dict:
    """Retire a saved entry that turned out wrong or outdated, or that someone asked you to forget.

    Needs the same evidence as changing it (see remember), unless the person it's about
    asked you to forget it themselves in this conversation.

    Args:
        entry: The entry ID, like e12.
        sources: Message IDs from search results showing it's wrong or outdated.
    """
    return await run(
        ctx,
        "forget",
        lambda db: memory.forget(db, ctx.deps.scope, ctx.deps.roster, entry, sources),
    )


async def split_doc(
    ctx: RunContext[Any],
    subject: str,
    new_doc: str,
    title: str,
    summary: str,
    move_entries: list[str],
    from_doc: str = "profile",
) -> dict:
    """Make a new doc for a subject, moving entries into it from a full or crowded one.

    Each doc holds at most 1024 words; remember tells you when one is full and how to split.

    Args:
        subject: The same subject as in remember.
        new_doc: Short lowercase slug, like "music" or "work-history". An existing doc of
            the subject also works, if it has room.
        title: Short title, like "Alex's music taste".
        summary: One line saying what belongs in this doc; you'll find the doc by it later.
        move_entries: Entry IDs (like e12) to move. Empty creates an empty doc.
        from_doc: The doc to move them from.
    """
    return await run(
        ctx,
        "split_doc",
        lambda db: memory.split_doc(
            db,
            ctx.deps.scope,
            ctx.deps.roster,
            subject,
            new_doc,
            title,
            summary,
            move_entries,
            from_doc,
        ),
    )


TOOLS = [recall, search_memory, remember, forget, split_doc]


def register(agent: Agent) -> None:
    for tool in TOOLS:
        agent.tool(tool)
