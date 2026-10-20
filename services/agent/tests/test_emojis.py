import json
import re
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from psycopg import AsyncConnection
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelResponse, TextPart, ThinkingPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from kiki_agent_host.agent import executor as agent_executor
from kiki_agent_host.agent.executor import PydanticExecutor
from kiki_agent_host.api.app import create_app
from kiki_agent_host.chat import emojis
from kiki_agent_host.config import Settings
from kiki_agent_host.contracts import DescribeEmojisRequest, DescribeEmojisResult, JobRequest
from kiki_agent_host.media import fetch
from kiki_agent_host.openrouter.embeddings import Embedder

GUILD, OTHER_GUILD = 1, 7
MOODS = [{"confused", "huh", "what"}, {"laughing", "crying", "funny", "dead"}]
PNG = b"\x89PNG\r\n\x1a\nfake"


def vector(text: str) -> list[float]:
    words = set(re.findall(r"[a-z]+", text.lower()))
    values = [float(len(words & mood)) for mood in MOODS] + [0.01]
    return values + [0.0] * (1024 - len(values))


class FakeEmbeddings:
    def __init__(self, fail=False):
        self.fail, self.texts = fail, []

    async def create(self, model, input):
        if self.fail:
            raise RuntimeError("embedding provider down")
        self.texts += input
        data = [SimpleNamespace(index=i, embedding=vector(t)) for i, t in enumerate(input)]
        return SimpleNamespace(data=data, usage=SimpleNamespace(cost=0.001))


def embedder(fail=False):
    return Embedder(SimpleNamespace(embeddings=FakeEmbeddings(fail)))


def cdn(missing=(), html=()):
    """Discord's CDN: a PNG for every emoji except `missing` (404) and `html`."""
    return httpx.AsyncClient(transport=cdn_transport(missing, html))


def cdn_transport(missing=(), html=()):
    def handle(request: httpx.Request) -> httpx.Response:
        emoji_id = int(request.url.path.rsplit("/", 1)[1].removesuffix(".png"))
        if emoji_id in missing:
            return httpx.Response(404)
        if emoji_id in html:
            return httpx.Response(200, content=b"<html>", headers={"content-type": "image/png"})
        return httpx.Response(200, content=PNG)

    return httpx.MockTransport(handle)


@pytest.fixture
async def db(database_url):
    async with await AsyncConnection.connect(database_url, autocommit=True) as connection:
        yield connection


async def add(db, id, name, description=None, *, guild=GUILD, removed=False, available=True):
    await db.execute(
        "INSERT INTO guild_emojis (id, guild_id, name, available, raw, description, removed_at)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (
            id,
            guild,
            name,
            available,
            json.dumps({"id": str(id), "name": name}),
            description,
            datetime.now(UTC) if removed else None,
        ),
    )
    # As the Gateway or a resync records it: this server's list is known.
    await db.execute(
        "INSERT INTO guild_emoji_syncs (guild_id, snapshot_at) VALUES (%s, now())"
        " ON CONFLICT DO NOTHING",
        (guild,),
    )


async def database_now(db) -> datetime:
    cursor = await db.execute("SELECT now()")
    return (await cursor.fetchone())[0]


async def test_describes_each_pending_emoji_once_per_round(db):
    await add(db, 11, "huh")
    await add(db, 12, "dead")
    await add(db, 13, "gone")  # its image 404s
    await add(db, 14, "old", "already described")
    await add(db, 15, "removed", removed=True)
    await add(db, 21, "theirs", guild=OTHER_GUILD)
    asked = []

    async def describe(name, animated, image):
        asked.append(name)
        assert image.data == PNG and image.media_type == "image/png"
        return {"huh": "a confused face, what?", "dead": "laughing so hard, crying"}[name], 0.01

    started = await database_now(db)
    embed = embedder()
    result = await emojis.describe_pending(
        db,
        describe,
        embed,
        guild_id=GUILD,
        model="test/vision",
        attempted_before=started,
        client=cdn(missing={13}),
    )
    assert sorted(asked) == ["dead", "huh"]
    assert result == {"described": 2, "failed": 1, "remaining": 0, "cost": pytest.approx(0.021)}
    # The described emoji are embedded with their names; the old one too.
    assert sorted(embed.client.embeddings.texts) == [
        ":dead: laughing so hard, crying",
        ":huh: a confused face, what?",
        ":old: already described",
    ]
    cursor = await db.execute(
        "SELECT id, description, describe_model FROM guild_emojis WHERE id IN (11, 13) ORDER BY id"
    )
    assert await cursor.fetchall() == [
        (11, "a confused face, what?", "test/vision"),
        (13, None, None),
    ]

    # Same round: the failed emoji isn't retried. A new round retries it.
    again = await emojis.describe_pending(
        db, describe, embedder(), guild_id=GUILD, model="m", attempted_before=started, client=cdn()
    )
    assert again["described"] + again["failed"] == 0
    asked.clear()
    retry = await emojis.describe_pending(
        db,
        lambda name, animated, image: _async(("a gone emoji", None)),
        embedder(),
        guild_id=GUILD,
        model="m",
        attempted_before=await database_now(db),
        client=cdn(),
    )
    assert (retry["described"], retry["remaining"]) == (1, 0)

    # Redescribe replaces every description written before the round.
    redo = await emojis.describe_pending(
        db,
        lambda name, animated, image: _async((f"new {name}", None)),
        embedder(),
        guild_id=GUILD,
        model="m",
        attempted_before=await database_now(db),
        redescribe=True,
        limit=2,
        client=cdn(),
    )
    assert (redo["described"], redo["remaining"]) == (2, 2)


async def _async(value):
    return value


async def test_search_finds_usable_emoji_by_meaning_and_name(db):
    await add(db, 11, "huh", "a confused face")
    await add(db, 12, "skull_laugh", "laughing so hard, dead")
    await add(db, 13, "cry", "crying, laughing", removed=True)
    await add(db, 14, "lol_old", "laughing, funny", available=False)
    await add(db, 15, "plain")
    await add(db, 21, "theirs_laugh", "laughing", guild=OTHER_GUILD)
    embed = embedder()

    result = await emojis.search(db, embed, GUILD, "what?")  # semantic only: "what" ~ confused
    assert [e["emoji"] for e in result["emojis"]] == ["<:huh:11>"]
    assert result["semantic"] == "full"
    result = await emojis.search(db, embed, GUILD, "funny")
    assert [e["name"] for e in result["emojis"]] == ["skull_laugh"]
    # Names are searchable, even without a description.
    result = await emojis.search(db, embedder(fail=True), GUILD, "plain")
    assert result["semantic"] == "off"
    assert result["emojis"] == [
        {"emoji": "<:plain:15>", "name": "plain", "description": "(not described yet)"}
    ]
    assert (await emojis.search(db, embed, GUILD, "zebra"))["emojis"] == []
    with pytest.raises(ValueError):
        await emojis.search(db, embed, GUILD, " ")


async def test_find_by_name_written_form_and_id(db):
    await add(db, 11, "huh", "a confused face")
    await add(db, 12, "huh_what", "very confused")
    await add(db, 13, "oop", "oops", removed=True)
    await add(db, 21, "huh", "theirs", guild=OTHER_GUILD)

    async def names(query):
        return [e["name"] for e in (await emojis.find(db, GUILD, query)).get("emojis", [])]

    assert await names("huh") == ["huh"]
    assert await names(":HUH:") == ["huh"]
    assert await names("what") == ["huh_what"]
    assert await names("hu") == ["huh", "huh_what"]
    assert await names("<:huh_what:12>") == ["huh_what"]
    # A removed emoji is explained when written out, but not offered by name.
    found = await emojis.find(db, GUILD, "<a:oop:13>")
    assert found["emojis"][0]["note"].startswith("Removed")
    assert await names("oop") == []
    # Another server's emoji by ID falls back to the name here.
    assert await names("<:huh:21>") == ["huh"]
    assert "error" in await emojis.find(db, GUILD, "nothing")
    # IDs too large for Postgres aren't Discord's: no query error.
    assert await names("<:huh:99999999999999999999>") == ["huh"]
    assert "error" in await emojis.find(db, GUILD, "99999999999999999999")
    with pytest.raises(ValueError):
        await emojis.find(db, GUILD, "::")


async def test_describe_endpoint_needs_auth_and_executor_and_reports_counts():
    class Fake:
        def __init__(self):
            self.requests = []

        async def describe_emojis(self, request: DescribeEmojisRequest):
            self.requests.append(request)
            return DescribeEmojisResult(described=2, failed=0, remaining=1, cost=0.01)

    body = {"guild_id": "1", "model": "test/vision", "attempted_before_ms": 1}
    fake = Fake()
    settings = Settings("postgresql://unreachable.invalid/kiki", "token")
    transport = httpx.ASGITransport(app=create_app(settings, fake))
    async with httpx.AsyncClient(transport=transport, base_url="http://host") as http:
        assert (await http.post("/v1/emojis/describe", json=body)).status_code == 401
        headers = {"Authorization": "Bearer token"}
        response = await http.post("/v1/emojis/describe", json=body, headers=headers)
        assert response.json() == {"described": 2, "failed": 0, "remaining": 1, "cost": 0.01}
        assert fake.requests[0].redescribe is False
        bad = {**body, "guild_id": "x"}
        assert (
            await http.post("/v1/emojis/describe", json=bad, headers=headers)
        ).status_code == 422
    transport = httpx.ASGITransport(app=create_app(settings))
    async with httpx.AsyncClient(transport=transport, base_url="http://host") as http:
        response = await http.post("/v1/emojis/describe", json=body, headers=headers)
        assert response.status_code == 501


async def test_emoji_images_are_fetched_and_described_like_read_image(
    database_url, db, monkeypatch
):
    """The executor's describer is `media.vision.look`, downloads are `media.fetch.download`."""
    await add(db, 11, "huh")
    await add(db, 12, "fake")  # the CDN answers with a page, not an image
    await add(db, 13, "busy")  # the model is rate limited for this one
    monkeypatch.setattr(
        fetch, "client", lambda: httpx.AsyncClient(transport=cdn_transport(html={12}))
    )
    prompts = []

    def respond(messages, info: AgentInfo) -> ModelResponse:
        [text, image] = messages[0].parts[0].content
        prompts.append(text)
        assert image.media_type == "image/png"
        if ":busy:" in text:
            raise ModelHTTPError(429, "test/vision")
        return ModelResponse(parts=[TextPart("a confused face")], provider_details={"cost": 0.002})

    class TestExecutor(PydanticExecutor):
        def model(self, name):
            assert name == "test/vision"
            return FunctionModel(respond)

    executor = TestExecutor(database_url, "unused-key")
    executor.embeddings = SimpleNamespace(embeddings=FakeEmbeddings())
    result = await executor.describe_emojis(
        DescribeEmojisRequest(guild_id="1", model="test/vision", attempted_before_ms=0)
    )
    assert (result.described, result.failed) == (1, 2)
    assert result.cost == pytest.approx(0.003)  # one description plus its embedding
    assert len(prompts) == 2 and all("custom Discord emoji" in p for p in prompts)
    cursor = await db.execute("SELECT description FROM guild_emojis WHERE id = 11")
    assert (await cursor.fetchone())[0] == "a confused face"


async def test_each_description_is_charged_when_it_is_made(db):
    """emoji_description_costs (an ingester migration's trigger) keeps one charge
    per paid description, so a redescription doesn't move earlier spend into
    today's window. Unpaid descriptions charge nothing."""
    await add(db, 11, "huh")
    await add(db, 12, "free")

    async def describe_all(cost, **options):
        return await emojis.describe_pending(
            db,
            lambda name, animated, image: _async(
                (f"a {name} face", None if name == "free" else cost)
            ),
            embedder(),
            guild_id=GUILD,
            model="m",
            attempted_before=await database_now(db),
            client=cdn(),
            **options,
        )

    assert (await describe_all(0.25))["described"] == 2
    assert (await describe_all(0.125, redescribe=True))["described"] == 2
    cursor = await db.execute(
        "SELECT emoji_id, guild_id, cost_usd FROM emoji_description_costs ORDER BY id"
    )
    assert await cursor.fetchall() == [(11, GUILD, 0.25), (11, GUILD, 0.125)]
    cursor = await db.execute("SELECT describe_cost_usd FROM guild_emojis WHERE id = 11")
    assert (await cursor.fetchone())[0] == 0.375


def catalog_of(*rows) -> emojis.Catalog:
    """A catalog from (id, name, animated, available, removed) rows."""
    full = [
        dict(zip(["id", "name", "animated", "available", "removed"], row, strict=True))
        for row in rows
    ]
    by_name: dict[str, list[dict]] = {}
    for row in full:
        if emojis.usable(row):
            by_name.setdefault(row["name"].lower(), []).append(row)
    return emojis.Catalog({r["id"]: r for r in full}, by_name, synced=True)


# Shaped like production's: two emoji named huh, an animated one, a removed one.
SERVER = catalog_of(
    (700413229767327835, "dstrike", False, True, False),
    (1085017182301339648, "huh", False, True, False),
    (1136064129405305052, "huh", True, True, False),
    (1444819174172922043, "gape_ape", True, True, False),
    (13, "oop", False, True, True),
    (14, "boosted", False, False, False),
)


def test_check_fixes_known_names_and_flags_invented_emoji():
    # reply:401 and reply:406 (2026-10-20): an invented emoji, then the same one
    # copied from how Discord stored it, without an ID.
    for invented in ["the L i deserved <:salute:1>", "certified <:salute:>"]:
        checked = emojis.check(invented, SERVER)
        assert checked.problems and "no custom emoji named salute" in checked.problems[0]
        assert "salute" not in checked.stripped
    # reply:72 (2026-10-18): a real name without its ID is fixed, not rejected.
    checked = emojis.check("Ethan's <:huh:> reply", SERVER)
    assert (checked.text, checked.problems, checked.fixed) == (
        "Ethan's <:huh:1085017182301339648> reply",
        [],
        1,
    )
    # A wrong animated flag or name for a real ID, and a shortcode that names one.
    checked = emojis.check("<:gape_ape:1444819174172922043> <:DSTRIKE:700413229767327835:>", SERVER)
    assert checked.text == "<a:gape_ape:1444819174172922043> <:dstrike:700413229767327835>"
    assert emojis.check("react: :dstrike:", SERVER).text == "react: <:dstrike:700413229767327835>"
    # Already right: untouched.
    fine = "ok <:dstrike:700413229767327835> 💀 <a:huh:1136064129405305052>"
    assert emojis.check(fine, SERVER) == emojis.Checked(fine, fine)


def test_check_flags_shortcodes_removed_and_unavailable_emoji():
    checked = emojis.check("lmao :skull:", SERVER)
    assert "plain text" in checked.problems[0] and checked.stripped == "lmao"
    removed = emojis.check("<:oop:13> <:boosted:14>", SERVER).problems
    assert ["removed" in removed[0], "unavailable" in removed[1]] == [True, True]
    # Close names help the model pick a real one.
    assert "<:dstrike:700413229767327835>" in emojis.check("<:dstrik:5>", SERVER).problems[0]
    # Times, ratios, code and URLs aren't emoji.
    for text in ["meet at 1:30:45", "`:salute:`", "```\n<:salute:1>\n```", "https://x.com/a:b"]:
        assert emojis.check(text, SERVER).problems == [], text


def test_check_without_a_synced_catalog_passes_written_emoji():
    nothing = emojis.Catalog({}, {})
    assert emojis.check("<:froge:81>", nothing).problems == []
    assert emojis.check(":froge:", nothing).problems  # never renders, synced or not


def test_cutting_emoji_leaves_no_empty_react_line():
    checked = emojis.check("react: <:salute:1>\nten\n---\nreact: 😂 :skull:\nbye", SERVER)
    assert checked.stripped == "ten\n---\nreact: 😂\nbye"


def reply_request(text="alex: rate my outfit"):
    return JobRequest.model_validate(
        {
            "id": "reply:1",
            "kind": "reply",
            "scope": {"guild_id": str(GUILD), "channel_id": "2", "conversation_id": "1"},
            "model": "test/model",
            "messages": [
                {"role": "system", "content": "You are Kiki."},
                {"role": "user", "content": text},
            ],
            "max_output_tokens": 100,
            "people": [],
        }
    )


def scripted(*answers):
    """A model that answers with `answers` in turn, recording what it was told."""
    seen = []

    def respond(messages, info: AgentInfo) -> ModelResponse:
        seen.append((messages, [t.name for t in info.function_tools]))
        return ModelResponse(parts=[TextPart(answers[min(len(seen), len(answers)) - 1])])

    class TestExecutor(PydanticExecutor):
        web_tools = ()

        def model(self, name):
            return FunctionModel(respond)

    return TestExecutor, seen


async def test_reply_with_an_invented_emoji_is_retried_then_cut(database_url, db):
    await add(db, 11, "huh", "a confused face")
    Executor, seen = scripted("ten <:salute:1>", "ten <:huh:11>")
    executor = Executor(database_url, "unused-key")
    executor.embeddings = SimpleNamespace(embeddings=FakeEmbeddings())
    result = await executor.execute(reply_request())
    assert result.text == "ten <:huh:11>"
    retry = seen[1][0][-1].parts[0]
    assert "no custom emoji named salute" in retry.model_response()
    assert {"search_emojis", "find_emoji"} <= set(seen[0][1])

    # A model that won't fix it: the emoji is cut rather than losing the reply.
    Executor, seen = scripted("ten <:salute:1>")
    executor = Executor(database_url, "unused-key")
    executor.embeddings = SimpleNamespace(embeddings=FakeEmbeddings())
    assert (await executor.execute(reply_request())).text == "ten"
    assert len(seen) == 2


async def test_emoji_tools_are_offered_only_once_emoji_are_synced(database_url):
    Executor, seen = scripted("hey <:froge:81>")
    executor = Executor(database_url, "unused-key")
    executor.embeddings = SimpleNamespace(embeddings=FakeEmbeddings())
    # Nothing synced: no emoji tools, and written-out emoji can't be checked.
    assert (await executor.execute(reply_request())).text == "hey <:froge:81>"
    assert not {"search_emojis", "find_emoji"} & set(seen[0][1])


# Review of PR #67 (GPT-6.1-Sol), one test per finding.


def turns(*script, db=None):
    """A model following `script`: a text answer, a ("tool", name, args) call, or
    an async callable run first (it returns the answer). Records the tools each
    request offered."""
    offered: list[list[str]] = []

    async def respond(messages, info: AgentInfo) -> ModelResponse:
        offered.append([t.name for t in info.function_tools])
        step = script[min(len(offered), len(script)) - 1]
        if callable(step):
            step = await step()
        if isinstance(step, tuple):
            _, name, args = step
            return ModelResponse(parts=[ToolCallPart(name, args, tool_call_id=f"c{len(offered)}")])
        return ModelResponse(parts=[TextPart(step)])

    class TestExecutor(PydanticExecutor):
        web_tools = ()

        def model(self, name):
            return FunctionModel(respond)

    return TestExecutor, offered


async def run_turns(database_url, *script):
    Executor, offered = turns(*script)
    executor = Executor(database_url, "unused-key")
    executor.embeddings = SimpleNamespace(embeddings=FakeEmbeddings())
    return (await executor.execute(reply_request())).text, offered


async def test_a_bad_emoji_on_the_last_allowed_request_is_cut_not_retried(
    database_url, db, monkeypatch
):
    """Finding 1: a retry past the request limit lost the whole reply."""
    await add(db, 11, "huh", "a confused face")
    monkeypatch.setattr(agent_executor, "MAX_MODEL_REQUESTS", 2)
    text, offered = await run_turns(
        database_url, ("tool", "find_emoji", {"name": "salute"}), "ten <:salute:1>"
    )
    assert text == "ten"
    assert offered[1] == [], "the last allowed request is offered no tools"
    assert len(offered) == 2


async def test_a_retry_that_searches_stays_within_the_request_limit(database_url, db, monkeypatch):
    """Finding 1: the retry may search first; the last request still answers."""
    await add(db, 11, "huh", "a confused face")
    monkeypatch.setattr(agent_executor, "MAX_MODEL_REQUESTS", 3)
    text, offered = await run_turns(
        database_url,
        "ten <:salute:1>",
        ("tool", "search_emojis", {"query": "salute"}),
        "ten <:huh:11>",
    )
    assert text == "ten <:huh:11>"
    assert "search_emojis" in offered[1] and offered[2] == []


async def test_a_reply_of_only_bad_emoji_keeps_its_kind(database_url, db):
    """Round 1, finding 2, and round 2, finding 1: stripping to nothing brought
    the invalid text back, then became silence, which a server may disallow and
    which ignores a direct question. A message stays a message, a reaction a
    reaction."""
    await add(db, 11, "huh", "a confused face")
    for only, kept in [
        ("<:salute:1>", "🙂"),
        ("react: 😂\n<:salute:1>", "react: 😂\n🙂"),
        ("react: <:salute:1>", "react: 🙂"),
        ("react: :salute:, :nope:", "react: 🙂"),
    ]:
        assert emojis.check(only, SERVER).stripped == kept, only
        text, _ = await run_turns(database_url, only)
        assert text == kept, only


def test_links_and_quotes_are_left_as_written():
    """Finding 3: shortcodes inside URLs and quotes were rewritten or cut."""
    for text in [
        "see https://example.com/:huh:/docs",
        "see https://example.com/:salute:/docs ok",
        "see <https://example.com/:salute:/x>",
        "[the doc](https://example.com/:salute:/x)",
        "> Alex said :salute:",
        ">>> quoting\nall of :salute: this",
    ]:
        checked = emojis.check(text, SERVER)
        assert (checked.text, checked.problems) == (text, []), text
    # Kiki's own words after a quote are still checked.
    checked = emojis.check("> Alex said :salute:\nme too :huh:", SERVER)
    assert checked.text == "> Alex said :salute:\nme too <:huh:1085017182301339648>"


def test_cutting_keeps_code_and_other_spacing_verbatim():
    """Finding 4: tidying collapsed spaces inside code."""
    text = '```py\ns = "a  b"\n```\nten  out of ten <:salute:1>\n`x  y`  ok'
    checked = emojis.check(text, SERVER)
    assert checked.stripped == '```py\ns = "a  b"\n```\nten  out of ten\n`x  y`  ok'
    assert emojis.check("lol <:salute:1>, ok", SERVER).stripped == "lol, ok"
    assert emojis.check("a <:salute:1> b", SERVER).stripped == "a b"


def test_code_delimiters_follow_commonmark():
    """Finding 5: inline code ignored backtick run lengths, and long fences
    closed early."""
    for text in [
        "``a ` :salute: ` b``",
        "````\n```\n:salute:\n```\n````",
        "~~~\n:salute:\n```\n:skull:\n~~~",
        "```\nunclosed :salute:",
    ]:
        assert emojis.check(text, SERVER).problems == [], text
    # An unmatched backtick is literal text, so what follows is checked.
    assert emojis.check("it's `odd :salute:", SERVER).problems
    # A one-line ```ls``` is inline code, not a fence.
    assert emojis.check("```ls``` then :salute:", SERVER).problems


async def test_an_emoji_synced_during_the_run_is_accepted(database_url, db):
    """Finding 6: the run's catalog went stale; it's reloaded before rejecting."""
    await add(db, 11, "huh", "a confused face")

    async def gateway_adds_one():
        await add(db, 12, "fresh")  # GUILD_EMOJIS_UPDATE synced it mid-run
        return "new one <:fresh:12>"

    text, offered = await run_turns(database_url, gateway_adds_one)
    assert (text, len(offered)) == ("new one <:fresh:12>", 1)


def test_react_lines_lose_leftover_separators():
    """Finding 7: `react: :salute:, :nope:` was left as `react: ,`."""
    cases = {
        "react: :salute:, :nope:\nok": "ok",
        "react: :salute:, 😂\nok": "react: 😂\nok",
        "react: 😂, <:salute:1>,💀\nok": "react: 😂 💀\nok",
        "Kiki: react: <:salute:1>\nok": "ok",
    }
    for text, stripped in cases.items():
        assert emojis.check(text, SERVER).stripped == stripped, text


# Second review of PR #67 (GPT-6.1-Sol), one test per finding. Finding 1 is
# test_a_reply_of_only_bad_emoji_keeps_its_kind; finding 2 is Rust's
# an_older_snapshot_never_overwrites_a_newer_one (services/ingester/src/emojis.rs).


async def test_the_catalog_is_current_for_every_answer(database_url, db):
    """Finding 3: one reload, spent on a first bad answer, left an emoji added
    later in the run rejected, and a deleted one accepted."""
    await add(db, 11, "huh", "a confused face")

    async def gateway_adds_one():
        await add(db, 12, "fresh")
        return "new one <:fresh:12>"

    text, offered = await run_turns(database_url, "ten <:salute:1>", gateway_adds_one)
    assert (text, len(offered)) == ("new one <:fresh:12>", 2)

    async def gateway_deletes_one():
        await db.execute("UPDATE guild_emojis SET removed_at = now() WHERE id = 11")
        return "same <:huh:11>"

    text, _ = await run_turns(database_url, gateway_deletes_one, "same")
    assert text == "same"


def test_shortcodes_run_together_are_each_checked():
    """Finding 4: `:salute::salute:` skipped the check."""
    checked = emojis.check("ok :salute::salute:", SERVER)
    assert len(checked.problems) == 2 and checked.stripped == "ok"
    assert emojis.check(":huh::dstrike:", SERVER).text == (
        "<:huh:1085017182301339648><:dstrike:700413229767327835>"
    )
    # Not shortcodes: a C++ path, a time, a word with colons inside.
    for text in ["call std::vector::push_back", "at 10:30:15", "re:zero:x"]:
        assert emojis.check(text, SERVER).problems == [], text


def test_link_destinations_are_never_parsed_into():
    """Findings 5 and 9: the link regex stopped at the first `)` and scanned
    quadratically. A destination is skipped up to the next whitespace instead."""
    for text in [
        "[quote](https://example.com/a_(b)/:salute:)",
        "[q](https://example.com/a_(b)/:salute:) and more",
        "[q](:salute:)",
    ]:
        checked = emojis.check(text, SERVER)
        assert (checked.text, checked.problems) == (text, []), text
    # The link's text is Kiki's own words, so it's still checked.
    assert emojis.check("[:salute:](https://x.com)", SERVER).problems


def test_scans_stay_linear_on_hostile_input():
    """Finding 9: an unmatched `[` cost 2.8 s at 40k characters."""
    import time

    from kiki_agent_host.chat import markdown

    for hostile in ["[" * 200_000, "[a](" * 50_000, "`a``b```c" * 20_000, "\\`" * 100_000]:
        started = time.monotonic()
        markdown.protected(hostile)
        assert time.monotonic() - started < 2, hostile[:10]
    # A reply of any length is checked.
    huge = "[" * 50_000 + " <:salute:1>"
    assert emojis.check(huge, SERVER).problems


def test_messages_left_empty_between_separators_are_dropped():
    """Finding 6: `<:salute:1>\\n---\\n<:salute:1>` stripped to `---`, which Rust
    rejects as a reply with no message."""
    cases = {
        "<:salute:1>\n---\n<:salute:1>": "🙂",
        "ok\n---\n<:salute:1>": "ok",
        "<:salute:1>\n--- 1.5s\nok": "ok",
        "a\n---\n<:salute:1>\n---\nb": "a\n---\nb",
        "react: 😂\n<:salute:1>\n---\nok": "react: 😂\nok",
        "```\n---\n```\n<:salute:1>": "```\n---\n```",
    }
    for text, stripped in cases.items():
        assert emojis.check(text, SERVER).stripped == stripped, text


async def test_a_synced_server_with_no_emoji_rejects_invented_ones(database_url, db):
    """Finding 7: no emoji looked the same as never synced, so every invented
    emoji was accepted forever."""
    synced_empty = emojis.Catalog({}, {}, synced=True)
    assert emojis.check("hey <:froge:81>", synced_empty).problems
    assert emojis.check("hey <:froge:81>", emojis.Catalog({}, {})).problems == []
    await db.execute(
        "INSERT INTO guild_emoji_syncs (guild_id, snapshot_at) VALUES (%s, now())", (GUILD,)
    )
    Executor, seen = scripted("hey <:froge:81>")
    executor = Executor(database_url, "unused-key")
    executor.embeddings = SimpleNamespace(embeddings=FakeEmbeddings())
    assert (await executor.execute(reply_request())).text == "hey"
    retry = seen[1][0][-1].parts[0].model_response()
    assert "This server has no custom emoji" in retry


def test_escaped_backticks_are_literal():
    """Finding 8: `\\`` was read as a code delimiter."""
    assert emojis.check("not \\`code :salute: `", SERVER).problems
    # Escaping the first of two leaves a one-backtick opener, closed by the last.
    assert emojis.check("\\``code :salute: `", SERVER).problems == []
    # Inside a span, backslashes are literal: this one doesn't escape the closer.
    assert emojis.check("`a\\` :salute:", SERVER).problems


# Third review of PR #67 (GPT-6.1-Sol), one test per finding.


async def test_an_id_too_long_for_int_is_invalid_not_a_crash(database_url, db):
    """Finding 1: `int()` on 4,301 digits raised, which lost the reply."""
    absurd = "<:huh:" + "9" * 4301 + ">"
    checked = emojis.check(f"ok {absurd}", SERVER)
    # Not a usable ID, so the name decides, as for <:huh:>: fixed to the real huh.
    assert checked.problems == [] and checked.text == "ok <:huh:1085017182301339648>"
    fake = "<:nothing:" + "9" * 4301 + ">"
    checked = emojis.check(f"ok {fake}", SERVER)
    assert checked.problems and checked.stripped == "ok"
    assert len(checked.problems[0]) < 200
    await add(db, 11, "huh", "a confused face")
    text, _ = await run_turns(database_url, f"ok {fake}")
    assert text == "ok"


def test_an_emoji_right_after_a_link_is_checked():
    """Finding 2: protection ran to the next whitespace, past the link."""
    for text in [
        "[x](https://example.com)<:fake:1>",
        "https://example.com<:fake:1>",
        "<https://example.com><:fake:1>",
        "[x](https://example.com/a_(b))<:fake:1>",
        "[x](https://example.com/a\\)b)<:fake:1>",
    ]:
        checked = emojis.check(text, SERVER)
        assert len(checked.problems) == 1 and "<:fake:1>" not in checked.stripped, text
    # The destination itself is still left alone.
    text = "[x](https://example.com/a_(b)/:salute:)<:dstrike:700413229767327835>"
    assert emojis.check(text, SERVER) == emojis.Checked(text, text)


def test_long_replies_are_checked_and_stay_fast():
    """Finding 3: replies over 20,000 characters skipped the check, and
    delivery splits them, so a fake emoji got posted."""
    import time

    long = "word " * 10_000 + "<:fake:1>"
    checked = emojis.check(long, SERVER)
    assert checked.problems and "<:fake:1>" not in checked.stripped
    many = " ".join(["<:fake:1>"] * 10_000 + ["ok"])
    started = time.monotonic()
    checked = emojis.check(many, SERVER)
    assert time.monotonic() - started < 2
    # Over the tidy cap (round 4), so cut without closing the gaps.
    assert len(checked.problems) == 10_000 and checked.stripped.strip() == "ok"


# Fourth review of PR #67 (GPT-6.1-Sol). Finding 1 (Gateway emoji sync never
# delays catch-up) is Rust's; finding 2:


def test_closing_gaps_stays_linear_after_long_whitespace():
    """Finding 2: `GAP.sub` took 0.9/3.6/14.4 s on `" " * n + "ok <:fake:1>"` at
    10k/20k/40k characters, in the event loop. So did `REACT` on a long blank line."""
    import time

    started = time.monotonic()
    assert emojis.close_gaps(" " * 200_000 + "ok \x00") == " " * 200_000 + "ok"
    assert emojis.close_gaps("a \x00 , \x00 b") == "a b"
    assert emojis.REACT.fullmatch(" " * 200_000 + "x") is None
    # Under the cap, the whole check, tidy included.
    text = " " * (emojis.MAX_TIDIED_CHARS - 100) + "ok <:fake:1>"
    checked = emojis.check(text, SERVER)
    assert checked.stripped == text.removesuffix(" <:fake:1>")
    assert time.monotonic() - started < 2


def test_over_the_tidy_cap_emoji_are_still_cut():
    """The safety cap: a reply over 16k characters is checked, and its bad emoji
    are cut, just without tidying around them."""
    long = "word " * 4_000 + "<:fake:1> end"
    checked = emojis.check(long, SERVER)
    assert checked.problems and "<:fake:1>" not in checked.stripped
    assert checked.stripped.endswith("word  end"), "cut, not tidied"
    only_bad = " ".join(["<:fake:1>"] * 2_000)
    assert emojis.check(only_bad, SERVER).stripped == "🙂"


# Fifth review of PR #67 (Opus 5.5). Finding 2 (snapshot clocks) is Rust's.


def test_separator_matching_stays_linear_and_unchanged():
    """Finding 1: `---(?:\\s*-?[0-9]*\\.?[0-9]+…` backtracked quadratically, so
    `"hi <:fake:1>\\n---" + "1" * 15900 + "x"` took 4.2 s in the event loop."""
    import time

    accepted = ["---", "--- 5", "---5.5s", "--- .5 ms", "--- -2s", "---5 ms  ", "--- 1500ms"]
    rejected = ["---5.", "---.", "---5s5", "----", "--- later"]
    assert all(emojis.SEPARATOR.fullmatch(t) for t in accepted)
    assert not any(emojis.SEPARATOR.fullmatch(t) for t in rejected)
    started = time.monotonic()
    assert emojis.SEPARATOR.fullmatch("---" + "1" * 200_000 + "x") is None
    assert emojis.SEPARATOR.fullmatch("--- " + " " * 100_000 + "1" + " " * 100_000 + "x") is None
    checked = emojis.check("hi <:fake:1>\n---" + "1" * 15_900 + "x", SERVER)
    assert checked.problems and checked.stripped.startswith("hi\n---1")
    assert time.monotonic() - started < 2


# #67's output validator and LastRequestAnswers with #68's FallbackModel: a
# response that is all thinking goes again without thinking within the same
# request, so the last request offers no tools on either attempt, and the emoji
# check runs on what the fallback wrote.


def overthinking(thinking_script, unthinking_script):
    """Like `turns`, for a model that thinks (`thinking_script`, where None is a
    response that thinks until the limit) and its fallback without thinking.
    Records, per attempt, which model it was and the tools offered."""
    attempts: list[tuple[str, list[str]]] = []

    def respond(name, script):
        async def respond(messages, info: AgentInfo) -> ModelResponse:
            attempts.append((name, [t.name for t in info.function_tools]))
            step = script[min(sum(1 for a in attempts if a[0] == name), len(script)) - 1]
            if step is None:
                return ModelResponse(parts=[ThinkingPart("hmm " * 50)], finish_reason="length")
            if isinstance(step, tuple):
                _, tool, args = step
                return ModelResponse(
                    parts=[ToolCallPart(tool, args, tool_call_id=f"c{len(attempts)}")]
                )
            return ModelResponse(parts=[TextPart(step)])

        return respond

    class TestExecutor(PydanticExecutor):
        web_tools = ()

        def model(self, name):
            return FunctionModel(respond("thinking", thinking_script))

        def unthinking(self, name):
            return FunctionModel(respond("unthinking", unthinking_script))

    return TestExecutor, attempts


async def run_overthinking(database_url, thinking_script, unthinking_script):
    Executor, attempts = overthinking(thinking_script, unthinking_script)
    executor = Executor(database_url, "unused-key")
    executor.embeddings = SimpleNamespace(embeddings=FakeEmbeddings())
    return (await executor.execute(reply_request())).text, attempts


async def test_the_last_request_offers_no_tools_to_the_fallback_either(
    database_url, db, monkeypatch
):
    await add(db, 11, "huh", "a confused face")
    monkeypatch.setattr(agent_executor, "MAX_MODEL_REQUESTS", 2)
    text, attempts = await run_overthinking(
        database_url,
        [("tool", "find_emoji", {"name": "salute"}), None],
        ["ten <:salute:1>"],
    )
    # The fallback's bad emoji is checked, and cut: no request is left to retry.
    assert text == "ten"
    assert [name for name, _ in attempts] == ["thinking", "thinking", "unthinking"]
    assert attempts[0][1] != []
    assert attempts[1][1] == [] and attempts[2][1] == [], "the last request, both attempts"


async def test_the_emoji_check_runs_on_the_fallbacks_answer(database_url, db, monkeypatch):
    await add(db, 11, "huh", "a confused face")
    monkeypatch.setattr(agent_executor, "MAX_MODEL_REQUESTS", 3)
    text, attempts = await run_overthinking(
        database_url, [None, "ten <:huh:11>"], ["ten <:salute:1>"]
    )
    # The fallback's emoji was rejected with a retry, and the retry fixed it.
    assert text == "ten <:huh:11>"
    assert [name for name, _ in attempts] == ["thinking", "unthinking", "thinking"]
