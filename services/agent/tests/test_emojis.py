import json
import re
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from psycopg import AsyncConnection
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from kiki_agent_host import emojis, retrieval, vision
from kiki_agent_host.agent import PydanticExecutor
from kiki_agent_host.app import create_app
from kiki_agent_host.config import Settings
from kiki_agent_host.contracts import DescribeEmojisRequest, DescribeEmojisResult

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
    return retrieval.Embedder(SimpleNamespace(embeddings=FakeEmbeddings(fail)))


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
    """The executor's describer is `vision.look`, downloads are `vision.download`."""
    await add(db, 11, "huh")
    await add(db, 12, "fake")  # the CDN answers with a page, not an image
    await add(db, 13, "busy")  # the model is rate limited for this one
    monkeypatch.setattr(
        vision, "client", lambda: httpx.AsyncClient(transport=cdn_transport(html={12}))
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
