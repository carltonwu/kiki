"""Pydantic AI executor: the SDK owns the loop, these tools search the guild and its memory."""

import asyncio
import contextlib
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from psycopg import AsyncConnection, OperationalError
from pydantic_ai import Agent, RunContext
from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    TextPart,
    UserPromptPart,
)
from pydantic_ai.models import Model
from pydantic_ai.models.openrouter import OpenRouterModel
from pydantic_ai.providers.openrouter import OpenRouterProvider
from pydantic_ai.usage import UsageLimits

from . import memory, retrieval
from .contracts import JobRequest, JobResult, Message, Usage
from .executor import TransientError

logger = logging.getLogger(__name__)
os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")

MAX_TOOL_CALLS = 4
MAX_MEMORY_CALLS = 6  # separate budget: filing a memory shouldn't cost a search
MAX_MODEL_REQUESTS = 10
TOOL_TIMEOUT_SECONDS = 10
EMBEDDING_TIMEOUT_SECONDS = 4  # two calls (index, query) fit in the tool deadline


@dataclass
class Deps:
    """Trusted per-run context: tool arguments can only narrow it."""

    connection: AsyncConnection
    scope: retrieval.Scope
    embed: retrieval.Embedder
    roster: retrieval.Roster
    tool_calls: int = 0
    indexing: asyncio.Task[retrieval.Semantic] | None = None
    job_id: str = ""
    database_url: str = ""
    memory_calls: int = 0
    # Memory has its own connection: its transactions must not interleave with
    # background indexing on the shared one. The lock covers parallel tool calls.
    memory_connection: AsyncConnection | None = None
    memory_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def memory_db(self) -> AsyncConnection:
        if self.memory_connection is None:
            self.memory_connection = await AsyncConnection.connect(
                self.database_url, autocommit=True, connect_timeout=10
            )
        return self.memory_connection


async def guarded(ctx: RunContext[Deps], name: str, work, *, kind: str = "search") -> dict:
    if kind == "memory":
        ctx.deps.memory_calls += 1
        exhausted = ctx.deps.memory_calls > MAX_MEMORY_CALLS
    else:
        ctx.deps.tool_calls += 1
        exhausted = ctx.deps.tool_calls > MAX_TOOL_CALLS
    if exhausted:
        logger.debug("tool budget exhausted job_id=%s tool=%s", ctx.deps.job_id, name)
        return {"error": f"{kind.capitalize()} budget used up. Answer with what you have."}
    started = time.monotonic()
    outcome = "ok"
    try:
        async with asyncio.timeout(TOOL_TIMEOUT_SECONDS):
            result = await work()
    except TimeoutError:
        outcome = "timeout"
        result = {"error": "Timed out."}
    except ValueError as e:
        outcome = "invalid_arguments"
        result = {"error": str(e)}
    logger.info(
        "tool completed job_id=%s tool=%s channel_id=%s conversation_id=%s "
        "latency_ms=%d hits=%d semantic=%s outcome=%s",
        ctx.deps.job_id,
        name,
        ctx.deps.scope.channel_id,
        ctx.deps.scope.conversation_id,
        (time.monotonic() - started) * 1000,
        len(result.get("messages", result.get("entries", []))),
        result.get("semantic", "-"),
        "error" if outcome == "ok" and "error" in result else outcome,
    )
    return result


async def search_messages(
    ctx: RunContext[Deps],
    query: str,
    person: str | None = None,
    channel: str | None = None,
    before: datetime | None = None,
    after: datetime | None = None,
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
        return await retrieval.search(
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
        return await retrieval.message_context(
            ctx.deps.connection, ctx.deps.scope, int(message_id), before, after, ctx.deps.roster
        )

    return await guarded(ctx, "get_message_context", work)


async def on_memory(ctx: RunContext[Deps], name: str, operation) -> dict:
    async def work():
        async with ctx.deps.memory_lock:
            connection = await ctx.deps.memory_db()
            try:
                return await operation(connection)
            except asyncio.CancelledError:
                # Timed out mid-query: the connection's state is unknown, so the
                # next memory call opens a fresh one.
                ctx.deps.memory_connection = None
                with contextlib.suppress(Exception):
                    await connection.close()
                raise

    return await guarded(ctx, name, work, kind="memory")


async def recall(ctx: RunContext[Deps], subject: str, doc: str | None = None) -> dict:
    """Read what you remember about a person or thing: their base profile, or one of their docs.

    The result also lists all of the subject's docs (title, what belongs there, size), so
    you can read a more specific one next.

    Args:
        subject: A person's ref like u2 (best), an @username or a name they've gone by; or
            the name of a thing (a pet, a place, a game, an inside joke).
        doc: Which doc to read. Leave it out for the base profile.
    """
    return await on_memory(
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
    return await on_memory(
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
    return await on_memory(
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
    return await on_memory(
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
    return await on_memory(
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


async def index_guild(deps: Deps) -> retrieval.Semantic:
    try:
        found, more = await retrieval.index_pending(
            deps.connection, deps.embed, guild_id=deps.scope.guild_id
        )
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


chat = Agent(
    deps_type=Deps,
    tools=[
        search_messages,
        get_message_context,
        recall,
        search_memory,
        remember,
        forget,
        split_doc,
    ],
)
plain = Agent()


def history(messages: list[Message]) -> list[ModelMessage]:
    def convert(m: Message) -> ModelMessage:
        if m.role == "assistant":
            return ModelResponse(parts=[TextPart(m.content)])
        part = SystemPromptPart(m.content) if m.role == "system" else UserPromptPart(m.content)
        return ModelRequest(parts=[part])

    return [convert(m) for m in messages]


class PydanticExecutor:
    def __init__(self, database_url: str, api_key: str):
        self.database_url = database_url
        if os.getenv("OPENROUTER_BASE_URL"):
            self.provider = OpenRouterProvider(openai_client=retrieval.openrouter_client(api_key))
        else:
            self.provider = OpenRouterProvider(api_key=api_key)
        # Fail fast inside a tool's deadline: a slow embedding degrades to "partial"
        # indexing and lexical search instead of losing the whole call.
        self.embeddings = retrieval.openrouter_client(
            api_key, timeout=EMBEDDING_TIMEOUT_SECONDS, max_retries=0
        )

    def model(self, name: str) -> Model:
        return OpenRouterModel(name, provider=self.provider)

    async def execute(self, request: JobRequest) -> JobResult:
        started = time.monotonic()
        if request.messages[-1].role != "user":
            raise ValueError("the last message must be from the user")
        prompt, earlier = request.messages[-1].content, history(request.messages[:-1])
        options = {
            "model": self.model(request.model),
            "message_history": earlier,
            "model_settings": {
                "max_tokens": request.max_output_tokens,
                "openrouter_usage": {"include": True},
            },
            "usage_limits": UsageLimits(request_limit=MAX_MODEL_REQUESTS),
        }
        embed = retrieval.Embedder(self.embeddings)
        try:
            if request.kind == "topic":
                result = await plain.run(prompt, **options)
            else:
                async with await AsyncConnection.connect(
                    self.database_url, autocommit=True, connect_timeout=10
                ) as connection:
                    scope = retrieval.Scope(
                        int(request.scope.guild_id),
                        int(request.scope.channel_id),
                        int(request.scope.conversation_id),
                    )
                    deps = Deps(
                        connection,
                        scope,
                        embed,
                        retrieval.Roster.of(request.people),
                        job_id=request.id,
                        database_url=self.database_url,
                    )
                    # Embedding is cheap: start while the model reads the prompt,
                    # rather than when it first searches.
                    deps.indexing = asyncio.create_task(index_guild(deps))
                    try:
                        result = await chat.run(prompt, deps=deps, **options)
                    finally:
                        await finish(deps.indexing)
                        if deps.memory_connection is not None:
                            await deps.memory_connection.close()
        except ModelHTTPError as e:
            if e.status_code == 429 or e.status_code >= 500:
                raise TransientError(f"provider returned {e.status_code}") from e
            raise
        except (ModelAPIError, OperationalError) as e:
            raise TransientError(type(e).__name__) from e

        usage = result.usage
        # OpenRouter's billed cost per response; Pydantic's price table lags new models.
        billed = [
            m.provider_details["cost"]
            for m in result.new_messages()
            if isinstance(m, ModelResponse) and "cost" in (m.provider_details or {})
        ]
        cost = sum(billed) if billed else (float(usage.cost) if usage.cost is not None else None)
        if embed.cost:
            cost = (cost or 0) + embed.cost
        logger.info(
            "execution completed job_id=%s kind=%s channel_id=%s conversation_id=%s model=%s "
            "requests=%d tool_calls=%d prompt_tokens=%d completion_tokens=%d "
            "cost_usd=%s latency_ms=%d",
            request.id,
            request.kind,
            request.scope.channel_id,
            request.scope.conversation_id,
            request.model,
            usage.requests,
            usage.tool_calls,
            usage.input_tokens,
            usage.output_tokens,
            cost,
            (time.monotonic() - started) * 1000,
        )
        return JobResult(
            text=result.output,
            model=request.model,
            steps=usage.requests,
            usage=Usage(
                prompt_tokens=usage.input_tokens, completion_tokens=usage.output_tokens, cost=cost
            ),
        )
