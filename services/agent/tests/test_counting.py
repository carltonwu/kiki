import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from psycopg import AsyncConnection
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from kiki_agent_host.agent.executor import PydanticExecutor
from kiki_agent_host.chat import counting
from kiki_agent_host.chat.counting import count
from kiki_agent_host.chat.scope import Roster, Scope
from kiki_agent_host.contracts import JobRequest

GUILD, OTHER_GUILD, GENERAL, LORE = 1, 7, 2, 9
SCOPE = Scope(GUILD, GENERAL, 1)
ROSTER = Roster.of([SimpleNamespace(ref="u1", user_id="100", label="Alex")])


class FakeEmbeddings:
    async def create(self, model, input):
        data = [SimpleNamespace(index=i, embedding=[0.1] * 1024) for i in range(len(input))]
        return SimpleNamespace(data=data, usage=SimpleNamespace(cost=0))


@pytest.fixture
async def db(database_url):
    async with await AsyncConnection.connect(database_url, autocommit=True) as connection:
        await connection.execute(
            "INSERT INTO channels (id, guild_id, name, type)"
            " VALUES (%s, %s, 'general', 0), (%s, %s, 'lore', 0)",
            (GENERAL, GUILD, LORE, GUILD),
        )
        yield connection


async def add(db, id, content, when, *, author="alex", author_id=100, **kwargs):
    raw = {"author": {"id": str(author_id), "username": author}, "member": {}, "mentions": []}
    await db.execute(
        "INSERT INTO messages (id, guild_id, channel_id, author_id, author_name, author_bot,"
        " content, created_at, raw) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            id,
            kwargs.get("guild", GUILD),
            kwargs.get("channel", GENERAL),
            author_id,
            author,
            kwargs.get("bot", False),
            content,
            datetime(*when, tzinfo=UTC),
            json.dumps(raw),
        ),
    )


@pytest.fixture
async def history(db):
    await add(db, 10, "slay", (2024, 3, 1))
    await add(db, 11, "she slayed that", (2025, 1, 5))
    await add(db, 12, "slaying as usual", (2025, 2, 9), channel=LORE)
    await add(db, 13, "SLAY queen", (2025, 2, 10), author="sam", author_id=101)
    await add(db, 14, "slay slay slay", (2025, 6, 1), author="sam", author_id=101, channel=LORE)
    await add(db, 15, "the slaw was ok", (2025, 6, 2), author="sam", author_id=101)
    await add(db, 16, "pizza", (2025, 6, 3), author="chris", author_id=102)
    # Never counted: a bot, a deleted message and another server.
    await add(db, 17, "slay says the bot", (2025, 6, 4), author="pilz", author_id=103, bot=True)
    await add(db, 18, "slay, then deleted", (2025, 6, 5))
    await add(db, 19, "slay elsewhere", (2025, 6, 6), guild=OTHER_GUILD, channel=50)
    await db.execute("UPDATE messages SET deleted_at = now() WHERE id = 18")
    return db


async def test_who_says_it_most_is_an_exact_ranking_with_links(history):
    result = await count(history, SCOPE, query="slay", roster=ROSTER, group_by="person")
    assert result["total"] == 5
    assert result["groups_total"] == 2
    [alex, sam] = result["groups"]
    assert (alex["person"], alex["count"]) == ("u1 Alex", 3)
    assert (sam["person"], sam["count"]) == ("sam", 2)
    # The newest match of each person, linked so Kiki can cite it.
    assert alex["example"]["url"] == f"https://discord.com/channels/{GUILD}/{LORE}/12"
    assert sam["example"]["text"] == "slay slay slay"


async def test_examples_avoid_the_conversation_but_it_still_counts(history):
    await history.execute(
        "INSERT INTO conversations (id, guild_id, channel_id, started_by) VALUES (1, %s, %s, 100)",
        (GUILD, GENERAL),
    )
    await add(history, 20, "kiki who says slay the most?", (2025, 7, 1))
    await history.execute(
        "INSERT INTO conversation_messages (message_id, conversation_id, via)"
        " VALUES (20, 1, 'mention'), (14, 1, 'decide')"
    )
    result = await count(history, SCOPE, query="slay", roster=ROSTER, group_by="person")
    [alex, sam] = result["groups"]
    assert alex["count"] == 4 and alex["example"]["text"] == "slaying as usual"
    # Sam's newest match (14) is in the conversation, so the example is the one before.
    assert sam["count"] == 2 and sam["example"]["text"] == "SLAY queen"


async def test_counts_over_time_and_by_channel(history):
    years = await count(history, SCOPE, query="slay", group_by="year")
    assert [(g["year"], g["count"]) for g in years["groups"]] == [("2025", 4), ("2024", 1)]
    months = await count(history, SCOPE, query="slay", group_by="month", limit=2)
    assert [(g["month"], g["count"]) for g in months["groups"]] == [("2025-02", 2), ("2024-03", 1)]
    assert months["groups_total"] == 4
    channels = await count(history, SCOPE, query="slay", group_by="channel")
    assert {g["channel"]: g["count"] for g in channels["groups"]} == {"#general": 3, "#lore": 2}


async def test_filters_narrow_the_count(history):
    async def total(**kwargs):
        return (await count(history, SCOPE, roster=ROSTER, **kwargs))["total"]

    assert await total() == 7  # every message, no query
    assert await total(query="slay", person="u1") == 3
    assert await total(query="slay", person="@sam", channel="#lore") == 1
    after, before = datetime(2025, 1, 1, tzinfo=UTC), datetime(2025, 3, 1, tzinfo=UTC)
    assert await total(query="slay", after=after, before=before) == 3
    # The pattern is exact text, case-insensitive: "slayed" and "slaying" don't
    # match a whole word, and \b (which models write) means a word boundary.
    assert await total(pattern=r"\bslay\b") == 3
    assert await total(query="slay", pattern="queen") == 1
    assert await total(query='"slay queen"') == 1
    assert "error" in await count(history, SCOPE, query="slay", person="Ethan")
    assert "error" in await count(history, SCOPE, query="slay", channel="#random")
    single = await count(history, SCOPE, query="pizza")
    assert single["total"] == 1 and single["example"]["text"] == "pizza"


async def test_bad_patterns_and_stopword_queries_are_argument_errors(history):
    with pytest.raises(ValueError, match="pattern"):
        await count(history, SCOPE, query="the")
    with pytest.raises(ValueError, match="regular expression"):
        await count(history, SCOPE, pattern="(unclosed")
    # The connection is still usable after the failed statement.
    assert (await count(history, SCOPE, pattern="slaw"))["total"] == 1


async def test_a_runaway_count_is_cut_off(history, monkeypatch):
    monkeypatch.setattr(counting, "STATEMENT_TIMEOUT", "1ms")
    monkeypatch.setitem(
        counting.FILTERS, "pattern", "m.content ~* %(pattern)s AND pg_sleep(0.05) IS NOT NULL"
    )
    result = await count(history, SCOPE, pattern="s")
    assert "too long" in result["error"]
    cursor = await history.execute("SHOW statement_timeout")
    assert (await cursor.fetchone())[0] == "0"  # SET LOCAL ended with the transaction


async def test_the_tool_counts_on_its_own_connection(database_url, history):
    def respond(messages, info: AgentInfo) -> ModelResponse:
        if len(messages) == 1:
            args = {"query": "slay", "group_by": "person", "after": ""}
            return ModelResponse(parts=[ToolCallPart("count_messages", args)])
        [part] = [p for p in messages[-1].parts if isinstance(p, ToolReturnPart)]
        return ModelResponse(parts=[TextPart(json.dumps(part.content))])

    class TestExecutor(PydanticExecutor):
        web_tools = ()  # FunctionModel can't run OpenRouter's server tools (see test_web.py)

        def model(self, name):
            return FunctionModel(respond)

    executor = TestExecutor(database_url, "unused-key")
    executor.embeddings = SimpleNamespace(embeddings=FakeEmbeddings())
    request = JobRequest.model_validate(
        {
            "id": "reply:count",
            "kind": "reply",
            "scope": {"guild_id": str(GUILD), "channel_id": str(GENERAL), "conversation_id": "1"},
            "model": "test/model",
            "messages": [{"role": "user", "content": "alex: who says slay the most?"}],
            "max_output_tokens": 100,
            "people": [{"ref": "u1", "user_id": "100", "label": "Alex", "names": ["alex"]}],
        }
    )
    result = json.loads((await executor.execute(request)).text)
    assert result["total"] == 5
    assert [(g["person"], g["count"]) for g in result["groups"]] == [("u1 Alex", 3), ("sam", 2)]
