import asyncio
import json
from collections import Counter
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from functools import partial
from types import SimpleNamespace

import pytest
from psycopg import AsyncConnection, OperationalError
from pydantic import TypeAdapter, ValidationError
from pydantic_ai.models.test import TestModel

from kiki_agent_host import db as db_module
from kiki_agent_host.agent.deps import MemorySession
from kiki_agent_host.agent.executor import PydanticExecutor
from kiki_agent_host.agent.guard import guarded
from kiki_agent_host.agent.tools import memory as memory_tools
from kiki_agent_host.agent.tools import messages as message_tools
from kiki_agent_host.agent.tools.messages import OptionalTime
from kiki_agent_host.chat import indexing
from kiki_agent_host.chat import search as search_module
from kiki_agent_host.chat.indexing import ENQUEUE_ALL, SAVE, SEARCHABLE, index_pending
from kiki_agent_host.chat.scope import Roster, Scope
from kiki_agent_host.chat.search import message_context, search
from kiki_agent_host.contracts import JobRequest
from kiki_agent_host.openrouter.embeddings import Embedder

GUILD, OTHER_GUILD, CHANNEL, OTHER_CHANNEL, CONVERSATION = 1, 7, 2, 9, 1
TOPICS = [{"deploy", "release", "ship", "shipped"}, {"pizza", "lunch", "food"}]
NOW = datetime.now(UTC)


def vector(text: str) -> list[float]:
    """A topic vector: paraphrases share a dimension, so they're semantically close."""
    words = set(text.lower().replace("?", "").split())
    values = [float(len(words & topic)) for topic in TOPICS] + [0.01]
    return values + [0.0] * (2048 - len(values))  # longer than DIMENSIONS, like Qwen


class FakeEmbeddings:
    def __init__(self, fail=False):
        self.fail, self.calls = fail, 0

    async def create(self, model, input):
        self.calls += 1
        if self.fail:
            raise RuntimeError("embedding provider down")
        data = [SimpleNamespace(index=i, embedding=vector(t)) for i, t in enumerate(input)]
        return SimpleNamespace(data=data, usage=SimpleNamespace(cost=0.001))


def embedder(fail=False):
    return Embedder(SimpleNamespace(embeddings=FakeEmbeddings(fail)))


SCOPE = Scope(GUILD, CHANNEL, CONVERSATION)


@pytest.fixture
async def db(database_url):
    async with await AsyncConnection.connect(database_url, autocommit=True) as connection:
        await connection.execute(
            "INSERT INTO channels (id, guild_id, name, type)"
            " VALUES (%s, %s, 'general', 0), (%s, %s, 'lore', 0)",
            (CHANNEL, GUILD, OTHER_CHANNEL, GUILD),
        )
        await connection.execute(
            "INSERT INTO conversations (id, guild_id, channel_id, started_by) VALUES (%s, %s, %s, 5)",
            (CONVERSATION, GUILD, CHANNEL),
        )
        yield connection


async def add(
    db,
    id,
    content,
    *,
    days_ago=0,
    guild=GUILD,
    channel=CHANNEL,
    author="alex",
    author_id=100,
    display=None,
    nick=None,
    bot=False,
):
    raw = {
        "author": {"id": str(author_id), "username": author, "global_name": display, "bot": bot},
        "member": {"nick": nick},
        "mentions": [],
    }
    await db.execute(
        "INSERT INTO messages (id, guild_id, channel_id, author_id, author_name, author_bot,"
        " content, created_at, raw) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            id,
            guild,
            channel,
            author_id,
            author,
            bot,
            content,
            NOW - timedelta(days=days_ago),
            json.dumps(raw),
        ),
    )


async def ids(db, query, **kwargs):
    result = await search(db, embedder(), SCOPE, query, **kwargs)
    return [int(m["id"]) for m in result["messages"]]


async def test_hybrid_search_respects_scope_and_matches_paraphrases(db):
    await add(db, 10, "we shipped the release on friday", days_ago=40)
    await add(db, 11, "pizza for lunch?", days_ago=1)
    await add(db, 12, "the release is shipped", channel=OTHER_CHANNEL, days_ago=60)
    await add(db, 16, "release shipped in another server", guild=OTHER_GUILD, channel=50)
    await add(db, 13, "release shipped, says the bot", bot=True)
    await add(db, 14, "release shipped, then deleted")
    await add(db, 15, "release notes in this conversation")
    await db.execute("UPDATE messages SET deleted_at = now() WHERE id = 14")
    await db.execute(
        "INSERT INTO conversation_messages (message_id, conversation_id, via) VALUES (15, %s, 'reply')",
        (CONVERSATION,),
    )
    # Indexed, but never returned: the conversation is already in the prompt.
    assert await index_pending(db, embedder(), guild_id=GUILD) == (4, None)

    # Lexical OR query: stopwords and unmatched words don't block a hit.
    assert (await ids(db, "what did alex say about the release"))[0] == 10
    # Every channel of the guild, and only this guild.
    assert sorted(await ids(db, "release shipped")) == [10, 12]
    assert await ids(db, "release shipped", channel="#lore") == [12]
    assert await ids(db, "release shipped", channel=f"<#{CHANNEL}>") == [10]
    result = await search(db, embedder(), SCOPE, "release", channel="#random")
    assert "error" in result
    [hit] = (await search(db, embedder(), SCOPE, "release", channel="lore"))["messages"]
    assert hit["channel"] == "#lore"
    assert hit["url"] == f"https://discord.com/channels/{GUILD}/{OTHER_CHANNEL}/12"
    # Semantic only: no shared words with message 10.
    assert (await ids(db, "deploy"))[0] == 10
    assert (await ids(db, "food"))[0] == 11
    result = await search(db, embedder(), SCOPE, "release", person="sam")
    assert "error" in result


async def test_people_are_found_by_id_whatever_they_were_called(db):
    # Two Chrises, a Chrissy, a Nik and a Niko, and Alex, who changed usernames.
    await add(db, 100, "chris w on lighting", author="chris_w", author_id=101, nick="Chris")
    await add(db, 101, "chrissy on lighting", author="chrissy", author_id=102, display="Chrissy")
    await add(db, 102, "nik on lighting", author="nik", author_id=103)
    await add(db, 103, "niko on lighting", author="niko", author_id=104)
    await add(db, 104, "chris l on lighting", author="chris.l", author_id=105, display="Chris")
    await add(
        db, 105, "alex before the rename, lighting", author="alexk_old", author_id=106, days_ago=9
    )
    await add(
        db,
        106,
        "alex after the rename, lighting <@101>",
        author="alex_99",
        author_id=106,
        display="Alex",
    )
    roster = Roster.of(
        [
            SimpleNamespace(ref="u1", user_id="106", label="Alex"),
            SimpleNamespace(ref="u2", user_id="101", label="Chris"),
        ]
    )

    async def lookup(person):
        return await search(db, embedder(), SCOPE, "lighting", person=person, roster=roster)

    async def authors(person):
        return sorted({m["author"] for m in (await lookup(person))["messages"]})

    # Similar names are different people.
    assert await authors("Chrissy") == ["Chrissy (@chrissy)"]
    assert await authors("nik") == ["nik"]
    assert await authors("Niko") == ["niko"]
    # A shared name finds everyone with it, and says so.
    both = await lookup("chris")
    assert {m["author"] for m in both["messages"]} == {"u2 Chris", "Chris (@chris.l)"}
    assert "2 different people" in both["note"]
    # Refs, mentions and any past username all mean the same account, shown by
    # its current name; mentions in results read as names.
    for person in ["u1", "@alexk_old", "ALEX_99", "<@106>"]:
        assert await authors(person) == ["u1 Alex"], person
    hits = (await lookup("u1"))["messages"]
    assert any(m["text"].endswith("@u2 Chris") for m in hits)
    assert "error" in await lookup("Ethan")
    with pytest.raises(ValueError):
        await lookup("u9")


async def test_recency_breaks_near_ties_but_not_explicit_dates(db):
    await add(db, 20, "release release release plan", days_ago=400)
    await add(db, 21, "release plan", days_ago=1)
    await index_pending(db, embedder(), guild_id=GUILD)
    assert await ids(db, "release plan") == [21, 20]
    before = NOW - timedelta(days=30)
    assert await ids(db, "release plan", before=before) == [20]


async def test_semantic_search_can_use_the_vector_index(db):
    # The index is on an expression the query must repeat exactly; without it,
    # every search reads every embedding. Sorting off, so the tiny table
    # doesn't make the planner prefer one anyway: the index is then the only
    # cheap way to get neighbours in order.
    await add(db, 30, "we shipped the release")
    await index_pending(db, embedder(), guild_id=GUILD)
    [query] = await embedder()(["deploy"], query=True)
    await db.execute(search_module.INDEX_SETTINGS)
    await db.execute("SET enable_sort = off")
    params = {
        "guild": GUILD,
        "channels": None,
        "conversation": CONVERSATION,
        "authors": None,
        "own": None,
        "before": None,
        "after": None,
        "query": "deploy",
        "vector": query,
        "candidates": search_module.CANDIDATES,
        "shortlist": search_module.SHORTLIST,
        "k": search_module.RRF_K,
        "min_similarity": search_module.MIN_SIMILARITY,
        "half_life": search_module.HALF_LIFE_DAYS,
        "recency": search_module.RECENCY_WEIGHT,
        "limit": 10,
    }
    plan = await (await db.execute("EXPLAIN " + search_module.SEARCH, params)).fetchall()
    assert "message_embeddings_hnsw_idx" in "\n".join(row[0] for row in plan)


async def test_edits_and_deletes_invalidate_embeddings(db):
    await add(db, 30, "pizza")
    await add(db, 31, "lunch")
    await index_pending(db, embedder(), guild_id=GUILD)
    await db.execute("UPDATE messages SET content = 'release' WHERE id = 30")
    await db.execute("UPDATE messages SET deleted_at = now() WHERE id = 31")
    cursor = await db.execute("SELECT count(*) FROM message_embeddings")
    assert (await cursor.fetchone())[0] == 0
    # A vector computed for old text is not saved.
    [stale] = await embedder()(["pizza"])
    await db.execute(SAVE, ([30], ["md5-of-old-text"], [stale]))
    cursor = await db.execute("SELECT count(*) FROM message_embeddings")
    assert (await cursor.fetchone())[0] == 0
    assert await index_pending(db, embedder(), guild_id=GUILD) == (1, None)


async def test_embedding_outage_degrades_to_lexical(db):
    await add(db, 40, "release day")
    result = await search(db, embedder(fail=True), SCOPE, "release")
    assert result["semantic"] == "off"
    assert [m["id"] for m in result["messages"]] == ["40"]
    with pytest.raises(ValueError):
        await search(db, embedder(), SCOPE, "  ")


async def test_message_context_is_bounded_and_guild_scoped(db):
    for i in range(50, 60):
        await add(db, i, f"message {i}")
    await add(db, 70, "elsewhere", channel=OTHER_CHANNEL)
    await add(db, 71, "elsewhere, later", channel=OTHER_CHANNEL)
    await add(db, 72, "another server", guild=OTHER_GUILD, channel=50)
    result = await message_context(db, SCOPE, 55, before=2, after=1)
    assert [m["id"] for m in result["messages"]] == ["53", "54", "55", "56"]
    assert result["messages"][0]["url"] == f"https://discord.com/channels/{GUILD}/{CHANNEL}/53"
    # Another channel of the guild: its own neighbors, not this channel's.
    result = await message_context(db, SCOPE, 70)
    assert [m["id"] for m in result["messages"]] == ["70", "71"]
    assert "error" in await message_context(db, SCOPE, 72)


async def test_ignored_authors_are_never_read(db):
    await add(db, 60, "release plan")
    await add(
        db, 61, "release plan from the noisy bot", author="noisy", author_id=1358234666267578368
    )
    await add(db, 62, "release plan again")
    assert await index_pending(db, embedder(), guild_id=GUILD) == (2, None)
    assert sorted(await ids(db, "release plan")) == [60, 62]
    result = await message_context(db, SCOPE, 60)
    assert [m["id"] for m in result["messages"]] == ["60", "62"]


async def test_a_link_fixers_repost_is_its_authors_message(db):
    # Seen in prod: the link fixer (an ignored author) deleted a tweet link and
    # reposted it, and the repost, the only copy, was invisible to Kiki.
    relay = (
        "<@100>: release plan\nhttps://fxtwitter.com/a/status/1 \n\n-# The original message"
        " sent by <@100> was deleted so the bot could fix the broken embed links for easier"
        " viewing."
    )
    fixer = {"author": "PilzBrots", "author_id": 1358234666267578368, "bot": True}
    await add(db, 60, relay, **fixer)
    await add(db, 61, "release plan from the chatbot", **fixer)
    await add(db, 62, "hi")
    assert await index_pending(db, embedder(), guild_id=GUILD) == (2, None)
    assert await ids(db, "release plan", person="alex") == [60]
    message, hi = (await message_context(db, SCOPE, 60))["messages"]
    assert hi["id"] == "62"
    assert (message["author"], message["text"]) == (
        "alex",
        "release plan\nhttps://fxtwitter.com/a/status/1",
    )


class ToolsOnlyModel(TestModel):
    """Calls every tool but `load_capability`: skill loading is covered in test_skills,
    and a placeholder skill id only errors."""

    async def request(self, messages, model_settings, parameters):
        tools = [t for t in parameters.function_tools if t.name != "load_capability"]
        parameters = replace(parameters, function_tools=tools)
        return await super().request(messages, model_settings, parameters)


async def test_executor_runs_tools_and_reports_usage(database_url, db):
    await add(db, 80, "we shipped the release")
    # The emoji tools are offered once the server has emoji synced.
    await db.execute(
        "INSERT INTO guild_emojis (id, guild_id, name, raw) VALUES (11, 1, 'huh', '{}')"
    )

    class TestExecutor(PydanticExecutor):
        web_tools = ()  # TestModel can't run OpenRouter's server tools (see test_web.py)

        def model(self, name):
            return ToolsOnlyModel()

    executor = TestExecutor(database_url, "unused-key")
    executor.embeddings = SimpleNamespace(embeddings=FakeEmbeddings())
    request = JobRequest.model_validate(
        {
            "id": "reply:1",
            "kind": "reply",
            "scope": {"guild_id": "1", "channel_id": "2", "conversation_id": "1"},
            "model": "test/model",
            "messages": [
                {"role": "system", "content": "You are Kiki."},
                {"role": "user", "content": "alex: when did we ship?"},
            ],
            "max_output_tokens": 100,
            "people": [{"ref": "u1", "user_id": "100", "label": "alex", "names": ["alex"]}],
        }
    )
    result = await executor.execute(request)
    # TestModel calls each tool once (with placeholder arguments), then answers
    # with their results.
    assert result.steps == 2
    # Memory tools are covered in test_memory; placeholder arguments only get errors.
    assert set(json.loads(result.text)) - set(memory_tools.toolset.tools) == {
        "search_messages",
        "find_quotes",
        "get_message_context",
        "channel_history",
        "count_messages",
        "read_image",
        "read_link",
        "search_emojis",
        "find_emoji",
    }
    # Message indexing plus search_messages' query embedding: search_emojis comes
    # after the 4-call lookup budget.
    assert result.usage.cost == pytest.approx(0.002)
    cursor = await db.execute("SELECT count(*) FROM message_embeddings")
    assert (await cursor.fetchone())[0] == 1


def test_blank_search_times_mean_no_bound():
    # Models that fill every optional argument send "" for an unused bound.
    times = TypeAdapter(OptionalTime)
    assert times.validate_python("") is None
    assert times.validate_python("  ") is None
    assert times.validate_python(None) is None
    assert times.validate_python("2026-10-18") == datetime(2026, 10, 18)
    with pytest.raises(ValidationError):
        times.validate_python("yesterday")


async def queued(db) -> list[int]:
    cursor = await db.execute("SELECT message_id FROM message_embedding_queue ORDER BY 1")
    return [row[0] for row in await cursor.fetchall()]


async def test_triggers_queue_exactly_the_searchable_messages(db):
    # Seen in prod: with nothing pending, finding pending messages walked all
    # 236k messages (5 to 7 s) at the start of every reply.
    await add(db, 80, "release plan")
    await add(db, 81, "   ")
    await add(db, 82, "a bot", bot=True)
    await add(db, 83, "the noisy bot", author_id=1358234666267578368)
    relay = (
        "<@100>: plan\nhttps://fxtwitter.com/a/status/1\n\n-# The original message sent by"
        " <@100> was deleted so the bot could fix the broken embed links for easier viewing."
    )
    await add(db, 84, relay, author_id=1358234666267578368, bot=True)
    await add(db, 85, "lunch")
    assert await queued(db) == [80, 84, 85]

    assert await index_pending(db, embedder(), guild_id=GUILD) == (3, None)
    assert await queued(db) == []
    # An edit drops the vector and queues the message again; an edit to
    # nothing or a delete takes it out.
    await db.execute("UPDATE messages SET content = 'pizza' WHERE id = 80")
    await db.execute("UPDATE messages SET content = 'friday' WHERE id = 81")
    assert await queued(db) == [80, 81]
    await db.execute("UPDATE messages SET content = '' WHERE id = 81")
    await db.execute("UPDATE messages SET deleted_at = now() WHERE id = 85")
    assert await queued(db) == [80]
    assert await index_pending(db, embedder(), guild_id=GUILD) == (1, None)
    assert await queued(db) == []


async def test_pending_reads_the_queue_and_drops_what_nothing_will_embed(db):
    await add(db, 90, "release plan")
    await add(db, 91, "pizza")
    await add(db, 92, "lunch")
    # Not queued (as if lost): replies don't look for it.
    await db.execute("DELETE FROM message_embedding_queue WHERE message_id = 90")
    # Embedded without leaving the queue (a host from before the queue, mid-deploy).
    [embedding] = await embedder()(["pizza"])
    await db.execute(
        "INSERT INTO message_embeddings (message_id, embedding) VALUES (91, %s::halfvec)",
        (embedding,),
    )
    # No longer searchable: its author has been ignored since.
    await db.execute("UPDATE messages SET author_id = 1358234666267578368 WHERE id = 92")
    assert await index_pending(db, embedder(), guild_id=GUILD) == (0, None)
    assert await queued(db) == []

    # `kiki-agent-host index` queues whatever is missing before draining.
    await db.execute("UPDATE messages SET author_id = 100 WHERE id = 92")
    cursor = await db.execute(ENQUEUE_ALL)
    assert cursor.rowcount == 2
    assert await index_pending(db, embedder(), guild_id=GUILD) == (2, None)
    cursor = await db.execute("SELECT count(*) FROM message_embeddings")
    assert (await cursor.fetchone())[0] == 3


async def test_a_search_does_not_wait_long_for_indexing(db, database_url, monkeypatch):
    # Seen in prod: saving vectors took 11 s, and the search waiting on it hit
    # the tool's 10 s deadline with nothing (reply:391).
    await add(db, 95, "release plan")
    await index_pending(db, embedder(), guild_id=GUILD)
    monkeypatch.setattr(message_tools, "INDEX_WAIT_SECONDS", 0.05)
    stuck = asyncio.Event()

    async def indexing():
        await stuck.wait()
        return "full"

    deps = SimpleNamespace(
        connection=db,
        embed=embedder(),
        scope=SCOPE,
        roster=Roster(),
        spent=Counter(),
        job_id="reply:test",
        indexing=asyncio.create_task(indexing()),
        memory=MemorySession(database_url),
    )
    result = await message_tools.search_messages(SimpleNamespace(deps=deps), "release plan")
    assert result["semantic"] == "partial"
    assert [m["id"] for m in result["messages"]] == ["95"]
    # Still running, for the next search.
    assert not deps.indexing.done()
    stuck.set()
    assert await deps.indexing == "full"


# Raises what a statement timeout raises when `semantic` is true: a timed-out
# query without depending on timing.
CANCEL_IF = """
CREATE FUNCTION cancel_if(semantic boolean) RETURNS boolean LANGUAGE plpgsql VOLATILE AS $$
BEGIN
    IF semantic THEN
        RAISE EXCEPTION 'canceling statement due to statement timeout'
            USING ERRCODE = 'query_canceled';
    END IF;
    RETURN true;
END
$$
"""


async def test_a_timed_out_hybrid_search_falls_back_to_lexical(db, monkeypatch):
    await add(db, 96, "release plan")
    await index_pending(db, embedder(), guild_id=GUILD)
    await db.execute(CANCEL_IF)
    timed_out = f"SELECT * FROM ({search_module.SEARCH}) s WHERE cancel_if(%(vector)s IS NOT NULL)"
    monkeypatch.setattr(search_module, "SEARCH", timed_out)
    result = await search(db, embedder(), SCOPE, "release plan")
    assert result["semantic"] == "off"
    assert [m["id"] for m in result["messages"]] == ["96"]
    # SET LOCAL: the connection keeps no timeout.
    cursor = await db.execute("SELECT current_setting('statement_timeout')")
    assert (await cursor.fetchone())[0] == "0"


async def test_a_search_that_times_out_both_ways_is_an_error_for_kiki(db, monkeypatch):
    await add(db, 97, "release plan")
    await db.execute(CANCEL_IF)
    monkeypatch.setattr(search_module, "SEARCH", "SELECT cancel_if(true) FROM (SELECT %(limit)s) x")
    result = await search(db, embedder(), SCOPE, "release plan")
    assert result == {"error": "The search took too long. Try again, or narrow it down."}


async def test_a_search_fits_its_deadline_when_every_step_is_slow(db, database_url, monkeypatch):
    """Seen in review: a 2 s index wait, a 1.5 s connect, a 3.8 s embedding and
    2.9 s of SQL, each under its own cap, added up to the tool's bare timeout."""
    await add(db, 98, "release plan")
    await index_pending(db, embedder(), guild_id=GUILD)
    # Caps that add up to more than the deadline, so the deadline binds; the
    # lexical query keeps 0.3 s, plenty on a loaded machine.
    monkeypatch.setattr(message_tools, "SEARCH_SECONDS", 1.5)
    monkeypatch.setattr(message_tools, "INDEX_WAIT_SECONDS", 0.5)
    monkeypatch.setattr(message_tools, "CONNECT_SECONDS", 0.3)
    monkeypatch.setattr(search_module, "EMBED_SECONDS", 0.8)
    monkeypatch.setattr(search_module, "STATEMENT_SECONDS", 0.3)
    monkeypatch.setattr(search_module, "LEXICAL_SECONDS", 0.4)
    monkeypatch.setattr(search_module, "CITE_SECONDS", 0.2)
    connect = db_module.connect

    async def slow_connect(url, **kwargs):
        await asyncio.sleep(0.15)
        return await connect(url, **kwargs)

    monkeypatch.setattr(db_module, "connect", slow_connect)

    class SlowEmbeddings(FakeEmbeddings):
        async def create(self, model, input):
            await asyncio.sleep(10)

    deps = SimpleNamespace(
        connection=db,
        embed=Embedder(SimpleNamespace(embeddings=SlowEmbeddings())),
        scope=SCOPE,
        roster=Roster(),
        spent=Counter(),
        job_id="reply:test",
        indexing=asyncio.create_task(asyncio.sleep(10, "full")),
        memory=MemorySession(database_url),
    )
    started = asyncio.get_running_loop().time()
    result = await message_tools.search_messages(SimpleNamespace(deps=deps), "release plan")
    # Its 1.5 s deadline, with room for a loaded machine; before, the tool's 10 s.
    assert asyncio.get_running_loop().time() - started < 2.5
    # Lexical results, not {"error": "Timed out."}.
    assert result["semantic"] == "off"
    assert [m["id"] for m in result["messages"]] == ["98"]
    deps.indexing.cancel()


async def test_slow_citations_still_return_the_results(db, database_url, monkeypatch):
    """Seen in review (times here are a fifth): a 2 s index wait, a 1.5 s
    connect and a 3.3 s embedding left the citations starting at 6.8 s, and a
    3.5 s citation query ran past the tool's 10 s, throwing the results away."""
    await add(db, 99, "release plan <@100>")
    await index_pending(db, embedder(), guild_id=GUILD)
    monkeypatch.setattr(message_tools, "SEARCH_SECONDS", 9 / 5)
    monkeypatch.setattr(message_tools, "INDEX_WAIT_SECONDS", 2 / 5)
    monkeypatch.setattr(message_tools, "CONNECT_SECONDS", 2 / 5)
    monkeypatch.setattr(search_module, "EMBED_SECONDS", 4 / 5)
    monkeypatch.setattr(search_module, "STATEMENT_SECONDS", 3 / 5)
    monkeypatch.setattr(search_module, "LEXICAL_SECONDS", 1 / 5)
    monkeypatch.setattr(search_module, "CITE_SECONDS", 1 / 5)
    # The outer guard, scaled the same way.
    monkeypatch.setattr(message_tools, "guarded", partial(guarded, timeout=10 / 5))
    connect = db_module.connect

    async def slow_connect(url, **kwargs):
        await asyncio.sleep(1.5 / 5)
        return await connect(url, **kwargs)

    monkeypatch.setattr(db_module, "connect", slow_connect)

    class SlowEmbeddings(FakeEmbeddings):
        async def create(self, model, input):
            await asyncio.sleep(3.3 / 5)
            return await super().create(model, input)

    async def slow_channel_names(connection, scope, ids):
        await connection.execute("SELECT pg_sleep(3.5 / 5)")
        return {}

    monkeypatch.setattr(search_module, "channel_names", slow_channel_names)
    deps = SimpleNamespace(
        connection=db,
        embed=Embedder(SimpleNamespace(embeddings=SlowEmbeddings())),
        scope=SCOPE,
        roster=Roster({"u1": 100}, {100: "u1 Alex"}),
        spent=Counter(),
        job_id="reply:test",
        indexing=asyncio.create_task(asyncio.sleep(10, "full")),
        memory=MemorySession(database_url),
    )
    result = await message_tools.search_messages(SimpleNamespace(deps=deps), "release plan")
    deps.indexing.cancel()
    # The results, labeled with what's at hand, not {"error": "Timed out."}.
    assert result["enriched"] is False
    assert "couldn't be looked up in time" in result["note"]
    [hit] = result["messages"]
    assert hit["id"] == "99"
    assert hit["author"] == "u1 Alex"  # from the prompt's roster
    assert hit["channel"] == f"#{CHANNEL}"
    assert hit["text"] == "release plan @u1 Alex"
    # The connection is fine afterwards, and keeps no timeout.
    cursor = await db.execute("SELECT current_setting('statement_timeout')")
    assert (await cursor.fetchone())[0] == "0"


async def test_slow_name_resolution_is_bounded(db, monkeypatch):
    await add(db, 100, "release plan")

    async def slow_resolve(connection, scope, roster, person):
        await connection.execute("SELECT pg_sleep(1)")
        return [100]

    monkeypatch.setattr(search_module, "resolve_person", slow_resolve)
    monkeypatch.setattr(search_module, "RESOLVE_SECONDS", 0.1)
    result = await search(
        db, embedder(), SCOPE, "release plan", person="alex", deadline=search_module.Deadline(5)
    )
    assert result == {"error": "The search took too long. Try again, or narrow it down."}


async def test_database_errors_in_indexing_leave_search_partial(monkeypatch):
    async def connect(database_url):
        raise OperationalError("too many clients")

    monkeypatch.setattr(db_module, "connect", connect)
    deps = SimpleNamespace(
        memory=MemorySession("unused"), embed=embedder(), scope=SCOPE, job_id="reply:test"
    )
    assert await message_tools.index_guild(deps) == "partial"


FOOTER = (
    "-# The original message sent by <@100> was deleted so the bot could fix the broken"
    " embed links for easier viewing."
)
VARIETY = [
    # (id, content, author_id, bot): what the triggers and the host must agree on.
    (100, "release plan", 100, False),
    (101, "", 100, False),
    (102, " \n ", 100, False),
    (103, "a bot's message", 300, True),
    (104, "the ignored chatbot", 1358234666267578368, True),
    (105, "an ignored person", 1358234666267578368, False),
    (
        106,
        "<@100>: plan\n\n" + FOOTER,
        1358234666267578368,
        True,
    ),
    (
        107,
        "<@100>:\n\n" + FOOTER,
        1358234666267578368,
        True,
    ),
    (108, "lunch", 100, False),
]


async def test_the_triggers_and_the_host_agree_on_what_is_searchable(db):
    """The rule is written twice: in migration 0029's trigger function and in
    chat/indexing.py SEARCHABLE (from chat/scope.py)."""
    for id, content, author_id, bot in VARIETY:
        await add(db, id, content, author_id=author_id, bot=bot)
    await db.execute("UPDATE messages SET deleted_at = now() WHERE id = 108")
    cursor = await db.execute(f"SELECT m.id FROM messages m WHERE {SEARCHABLE} ORDER BY 1")
    searchable = [row[0] for row in await cursor.fetchall()]
    # (btrim strips spaces only, so whitespace with a newline counts as text,
    # 102, and so does a relay of nothing but the footer, 107: the rule as it was.)
    assert {100, 106} <= set(searchable) and not {101, 103, 104, 105, 108} & set(searchable)
    assert await queued(db) == searchable
    # And the migration's one-off backfill, the same rule a third time.
    await db.execute("DELETE FROM message_embedding_queue")
    await db.execute(backfill())
    assert await queued(db) == searchable


def backfill() -> str:
    """Migration 0029's closing INSERT, its backfill."""
    from conftest import MIGRATIONS

    [path] = MIGRATIONS.glob("*_message_embedding_queue.sql")
    text = path.read_text()
    return text[text.rindex("INSERT INTO message_embedding_queue") :]


async def test_the_migration_queues_what_is_already_pending(empty_database_url):
    from conftest import MIGRATIONS, apply_migrations

    [queue] = MIGRATIONS.glob("*_message_embedding_queue.sql")
    await apply_migrations(empty_database_url, before=queue.name)
    async with await AsyncConnection.connect(empty_database_url, autocommit=True) as db:
        await db.execute(
            "INSERT INTO channels (id, guild_id, name, type) VALUES (%s, %s, 'general', 0)",
            (CHANNEL, GUILD),
        )
        for id, content, author_id, bot in VARIETY:
            await add(db, id, content, author_id=author_id, bot=bot)
        await add(db, 109, "already embedded")
        [embedding] = await embedder()(["already embedded"])
        await db.execute(
            "INSERT INTO message_embeddings (message_id, embedding) VALUES (109, %s::halfvec)",
            (embedding,),
        )
        cursor = await db.execute(
            f"SELECT m.id FROM messages m WHERE {SEARCHABLE} AND m.id <> 109 ORDER BY 1"
        )
        pending = [row[0] for row in await cursor.fetchall()]
        assert 108 in pending
        await db.execute(queue.read_text())
        assert await queued(db) == pending
        # The triggers it adds work on the data it found.
        await db.execute("UPDATE messages SET content = 'friday' WHERE id = 109")
        assert await queued(db) == [*pending, 109]


async def test_a_concurrent_edit_keeps_its_message_queued(db, database_url):
    await add(db, 110, "pizza")
    [embedding] = await embedder()(["pizza"])
    cursor = await db.execute("SELECT md5(content) FROM messages WHERE id = 110")
    [old_hash] = await cursor.fetchone()
    async with await AsyncConnection.connect(database_url) as editor:
        # The edit holds the row; SAVE's FOR SHARE waits for it, then sees new text.
        await editor.execute("UPDATE messages SET content = 'lunch' WHERE id = 110")
        save = asyncio.create_task(db.execute(SAVE, ([110], [old_hash], [embedding])))
        await asyncio.sleep(0.2)
        assert not save.done()
        await editor.commit()
        await save
    cursor = await db.execute("SELECT count(*) FROM message_embeddings")
    assert (await cursor.fetchone())[0] == 0
    assert await queued(db) == [110]
    assert await index_pending(db, embedder(), guild_id=GUILD) == (1, None)


async def test_an_edit_during_a_full_requeue_is_not_lost(db, database_url):
    # Seen in review: the trigger's insert hit `kiki-agent-host index`'s
    # uncommitted queue row, failing the edit and so the ingester's write.
    await add(db, 120, "pizza")
    await db.execute("DELETE FROM message_embedding_queue")
    async with await AsyncConnection.connect(database_url) as index:
        await index.execute(ENQUEUE_ALL)  # uncommitted
        async with await AsyncConnection.connect(database_url, autocommit=True) as ingester:
            edit = asyncio.create_task(
                ingester.execute("UPDATE messages SET content = 'lunch' WHERE id = 120")
            )
            await asyncio.sleep(0.2)
            await index.commit()
            await edit
    cursor = await db.execute("SELECT content FROM messages WHERE id = 120")
    assert (await cursor.fetchone())[0] == "lunch"
    assert await queued(db) == [120]


async def test_stale_rows_are_dropped_from_the_newest_of_the_guild_only(db, monkeypatch):
    monkeypatch.setattr(indexing, "STALE_WINDOW", 2)
    for id in (130, 131, 132):
        await add(db, id, "pizza")
    await add(db, 133, "lunch", guild=OTHER_GUILD, channel=50)
    # All four become stale: their author is ignored since.
    await db.execute("UPDATE messages SET author_id = 1358234666267578368")
    assert await index_pending(db, embedder(), guild_id=GUILD) == (0, None)
    # The guild's two newest went; the older one waits, the other guild's stays.
    assert await queued(db) == [130, 133]


HISTORY = 20_000


async def plan(db, query: str, params: dict) -> str:
    cursor = await db.execute("EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF) " + query, params)
    return "\n".join(row[0] for row in await cursor.fetchall())


async def test_the_queue_drives_the_pending_queries_whatever_the_statistics(db):
    """Seen in review: as a plain join, Postgres could walk every message and
    every embedding before reading the (drained) queue."""
    # The vector index isn't under test, and building it for 20k rows is slow.
    await db.execute("DROP INDEX message_embeddings_hnsw_idx")
    await db.execute(
        "INSERT INTO messages (id, guild_id, channel_id, author_id, author_name, content,"
        " created_at, raw)"
        " SELECT i, %s, %s, 100, 'alex', 'message ' || i, now(), '{}'"
        " FROM generate_series(1, %s) i",
        (GUILD, CHANNEL, HISTORY),
    )
    await db.execute(
        "INSERT INTO message_embeddings (message_id, embedding)"
        " SELECT i, array_fill(0.01::real, ARRAY[1024])::halfvec FROM generate_series(1, %s) i",
        (HISTORY,),
    )
    # Statistics from when everything was queued, then a drained queue.
    await db.execute("ANALYZE messages, message_embeddings, message_embedding_queue")
    await db.execute("DELETE FROM message_embedding_queue")
    await add(db, HISTORY + 1, "release plan")

    for query, params in [
        (indexing.PENDING, {"guild": GUILD, "below": None, "limit": indexing.INDEX_LIMIT}),
        (indexing.DROP_STALE, {"guild": GUILD, "window": indexing.STALE_WINDOW}),
    ]:
        shown = await plan(db, query, params)
        assert "Scan Backward using message_embedding_queue_pkey" in shown, shown
        assert "Seq Scan on messages" not in shown, shown
        assert "Seq Scan on message_embeddings" not in shown, shown
        assert "Index Scan Backward using messages_pkey" not in shown, shown
        assert "Hash" not in shown and "Merge" not in shown, shown
        # One queued row: one message looked up, once.
        assert "on messages m (actual rows=1.00 loops=1)" in shown, shown
    assert await index_pending(db, embedder(), guild_id=GUILD) == (1, None)


async def test_the_cli_sweeps_stale_rows_past_the_reply_window(db, monkeypatch):
    monkeypatch.setattr(indexing, "STALE_WINDOW", 1)
    for id in (140, 141, 142):
        await add(db, id, "pizza")
    await db.execute("UPDATE messages SET author_id = 1358234666267578368")
    await index_pending(db, embedder(), guild_id=GUILD)
    assert await queued(db) == [140, 141]
    assert await indexing.sweep(db) == 2
    assert await queued(db) == []
