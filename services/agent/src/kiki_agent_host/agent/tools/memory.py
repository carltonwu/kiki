"""Memory tools (docs/subject-memory-hld.md). They use their own connection
(`deps.memory`) and their own budget, apart from the lookups."""

import asyncio
import logging
from typing import Literal

from pydantic_ai import RunContext
from pydantic_ai.toolsets import FunctionToolset

from ...chat import memory
from ..deps import Deps
from ..guard import Budget, guarded

logger = logging.getLogger(__name__)


# Separate from the lookups budget: filing a memory shouldn't cost a search.
MEMORY = Budget("Memory", 6)
TIMEOUT_SECONDS = 10


async def run(ctx: RunContext[Deps], name: str, operation) -> dict:
    session = ctx.deps.memory

    async def work() -> dict:
        try:
            return await operation(await session.connect())
        except asyncio.CancelledError:
            # Timed out mid-query: the connection's state is unknown, so the
            # next memory call opens a fresh one.
            await session.close()
            raise

    return await guarded(ctx, name, work, budget=MEMORY, timeout=TIMEOUT_SECONDS, lock=session.lock)


async def recall(ctx: RunContext[Deps], subject: str, doc: str | None = None) -> dict:
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


async def search_memory(ctx: RunContext[Deps], query: str, subject: str | None = None) -> dict:
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
    ctx: RunContext[Deps],
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


async def forget(ctx: RunContext[Deps], entry: str, sources: list[str] | None = None) -> dict:
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
    ctx: RunContext[Deps],
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


toolset = FunctionToolset([recall, search_memory, remember, forget, split_doc])
