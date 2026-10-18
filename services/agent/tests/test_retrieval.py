import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from psycopg import AsyncConnection
from pydantic_ai.models.test import TestModel

from kiki_agent_host import memory_tools, retrieval
from kiki_agent_host.agent import PydanticExecutor
from kiki_agent_host.contracts import JobRequest

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
    return retrieval.Embedder(SimpleNamespace(embeddings=FakeEmbeddings(fail)))


SCOPE = retrieval.Scope(GUILD, CHANNEL, CONVERSATION)


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
    result = await retrieval.search(db, embedder(), SCOPE, query, **kwargs)
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
    assert await retrieval.index_pending(db, embedder(), guild_id=GUILD) == (4, None)

    # Lexical OR query: stopwords and unmatched words don't block a hit.
    assert (await ids(db, "what did alex say about the release"))[0] == 10
    # Every channel of the guild, and only this guild.
    assert sorted(await ids(db, "release shipped")) == [10, 12]
    assert await ids(db, "release shipped", channel="#lore") == [12]
    assert await ids(db, "release shipped", channel=f"<#{CHANNEL}>") == [10]
    result = await retrieval.search(db, embedder(), SCOPE, "release", channel="#random")
    assert "error" in result
    [hit] = (await retrieval.search(db, embedder(), SCOPE, "release", channel="lore"))["messages"]
    assert hit["channel"] == "#lore"
    assert hit["url"] == f"https://discord.com/channels/{GUILD}/{OTHER_CHANNEL}/12"
    # Semantic only: no shared words with message 10.
    assert (await ids(db, "deploy"))[0] == 10
    assert (await ids(db, "food"))[0] == 11
    result = await retrieval.search(db, embedder(), SCOPE, "release", person="sam")
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
    roster = retrieval.Roster.of(
        [
            SimpleNamespace(ref="u1", user_id="106", label="Alex"),
            SimpleNamespace(ref="u2", user_id="101", label="Chris"),
        ]
    )

    async def search(person):
        return await retrieval.search(
            db, embedder(), SCOPE, "lighting", person=person, roster=roster
        )

    async def authors(person):
        return sorted({m["author"] for m in (await search(person))["messages"]})

    # Similar names are different people.
    assert await authors("Chrissy") == ["Chrissy (@chrissy)"]
    assert await authors("nik") == ["nik"]
    assert await authors("Niko") == ["niko"]
    # A shared name finds everyone with it, and says so.
    both = await search("chris")
    assert {m["author"] for m in both["messages"]} == {"u2 Chris", "Chris (@chris.l)"}
    assert "2 different people" in both["note"]
    # Refs, mentions and any past username all mean the same account, shown by
    # its current name; mentions in results read as names.
    for person in ["u1", "@alexk_old", "ALEX_99", "<@106>"]:
        assert await authors(person) == ["u1 Alex"], person
    hits = (await search("u1"))["messages"]
    assert any(m["text"].endswith("@u2 Chris") for m in hits)
    assert "error" in await search("Ethan")
    with pytest.raises(ValueError):
        await search("u9")


async def test_recency_breaks_near_ties_but_not_explicit_dates(db):
    await add(db, 20, "release release release plan", days_ago=400)
    await add(db, 21, "release plan", days_ago=1)
    await retrieval.index_pending(db, embedder(), guild_id=GUILD)
    assert await ids(db, "release plan") == [21, 20]
    before = NOW - timedelta(days=30)
    assert await ids(db, "release plan", before=before) == [20]


async def test_edits_and_deletes_invalidate_embeddings(db):
    await add(db, 30, "pizza")
    await add(db, 31, "lunch")
    await retrieval.index_pending(db, embedder(), guild_id=GUILD)
    await db.execute("UPDATE messages SET content = 'release' WHERE id = 30")
    await db.execute("UPDATE messages SET deleted_at = now() WHERE id = 31")
    cursor = await db.execute("SELECT count(*) FROM message_embeddings")
    assert (await cursor.fetchone())[0] == 0
    # A vector computed for old text is not saved.
    [stale] = await embedder()(["pizza"])
    await db.execute(retrieval.SAVE, ([30], ["md5-of-old-text"], [stale]))
    cursor = await db.execute("SELECT count(*) FROM message_embeddings")
    assert (await cursor.fetchone())[0] == 0
    assert await retrieval.index_pending(db, embedder(), guild_id=GUILD) == (1, None)


async def test_embedding_outage_degrades_to_lexical(db):
    await add(db, 40, "release day")
    result = await retrieval.search(db, embedder(fail=True), SCOPE, "release")
    assert result["semantic"] == "off"
    assert [m["id"] for m in result["messages"]] == ["40"]
    with pytest.raises(ValueError):
        await retrieval.search(db, embedder(), SCOPE, "  ")


async def test_message_context_is_bounded_and_guild_scoped(db):
    for i in range(50, 60):
        await add(db, i, f"message {i}")
    await add(db, 70, "elsewhere", channel=OTHER_CHANNEL)
    await add(db, 71, "elsewhere, later", channel=OTHER_CHANNEL)
    await add(db, 72, "another server", guild=OTHER_GUILD, channel=50)
    result = await retrieval.message_context(db, SCOPE, 55, before=2, after=1)
    assert [m["id"] for m in result["messages"]] == ["53", "54", "55", "56"]
    assert result["messages"][0]["url"] == f"https://discord.com/channels/{GUILD}/{CHANNEL}/53"
    # Another channel of the guild: its own neighbors, not this channel's.
    result = await retrieval.message_context(db, SCOPE, 70)
    assert [m["id"] for m in result["messages"]] == ["70", "71"]
    assert "error" in await retrieval.message_context(db, SCOPE, 72)


async def test_ignored_authors_are_never_read(db):
    await add(db, 60, "release plan")
    await add(
        db, 61, "release plan from the noisy bot", author="noisy", author_id=1358234666267578368
    )
    await add(db, 62, "release plan again")
    assert await retrieval.index_pending(db, embedder(), guild_id=GUILD) == (2, None)
    assert sorted(await ids(db, "release plan")) == [60, 62]
    result = await retrieval.message_context(db, SCOPE, 60)
    assert [m["id"] for m in result["messages"]] == ["60", "62"]


async def test_executor_runs_tools_and_reports_usage(database_url, db):
    await add(db, 80, "we shipped the release")

    class TestExecutor(PydanticExecutor):
        web_tools = ()  # TestModel can't run OpenRouter's server tools (see test_web.py)

        def model(self, name):
            return TestModel()

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
    assert set(json.loads(result.text)) - {t.__name__ for t in memory_tools.TOOLS} == {
        "search_messages",
        "get_message_context",
        "read_image",
        "search_emojis",
        "find_emoji",
    }
    # Message indexing plus the two searches' query embeddings.
    assert result.usage.cost == pytest.approx(0.003)
    cursor = await db.execute("SELECT count(*) FROM message_embeddings")
    assert (await cursor.fetchone())[0] == 1
