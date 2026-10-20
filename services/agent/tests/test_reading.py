import json

import httpx
import pytest
from psycopg import AsyncConnection
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import BinaryContent, ModelResponse, TextPart, ThinkingPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from kiki_agent_host.chat.attachments import images_in, sized
from kiki_agent_host.chat.scope import Roster, Scope
from kiki_agent_host.chat.search import message_context
from kiki_agent_host.media import fetch
from kiki_agent_host.media.fetch import allowed
from kiki_agent_host.media.reading import read_image
from kiki_agent_host.media.vision import REASONING_TOKENS, ImageRefused, Vision, look

GUILD, CHANNEL, CONVERSATION = 1, 2, 1
SCOPE = Scope(GUILD, CHANNEL, CONVERSATION)
CDN = "https://cdn.discordapp.com/attachments/2/3"
PNG = b"\x89PNG\r\n\x1a\nsim"
JPEG = b"\xff\xd8\xff\xe0sim"
WEBP = b"RIFF\x04\x00\x00\x00WEBPsim"


def attachment(name, content_type="image/png", **extra):
    url = f"{CDN}/{name}?ex=1&hm=2"
    return {"filename": name, "content_type": content_type, "url": url, **extra}


GALLERY = {
    "attachments": [
        attachment("a.png", width=800, height=600),
        attachment("notes.pdf", "application/pdf"),
        attachment("b.JPG", None),
        attachment("c.webp", "image/webp", description="a cat on a piano"),
    ],
    "embeds": [
        {
            "type": "gifv",
            "thumbnail": {
                "url": "https://media.tenor.com/x.png",
                "proxy_url": "https://images-ext-1.discordapp.net/x.png",
            },
        },
        {"type": "rich", "image": {"url": "https://example.com/y.jpg"}},
        {"type": "rich", "thumbnail": {"url": "https://example.com/link-preview.jpg"}},
        {"type": "image", "thumbnail": {"url": f"{CDN}/a.png?ex=1&hm=2"}},  # duplicate
    ],
}


def test_finds_image_attachments_then_image_embeds():
    found = images_in(GALLERY)
    assert [i.name for i in found] == ["a.png", "b.JPG", "c.webp", "linked image", "linked image"]
    assert found[2].alt == "a cat on a piano"
    assert found[3].url == "https://images-ext-1.discordapp.net/x.png"  # Discord's proxy
    assert images_in({"attachments": [attachment(f"{i}.png") for i in range(14)]})[-1].name == (
        "9.png"
    )
    assert images_in({}) == []


def test_large_images_use_the_resizing_proxy():
    small = {"url": "u", "proxy_url": "p", "width": 100, "height": 50}
    assert sized(small) == "u"
    big = {
        "url": "u",
        "proxy_url": "https://media.discordapp.net/a.png?ex=1&width=9",
        "width": 4000,
        "height": 2000,
    }
    assert sized(big) == "https://media.discordapp.net/a.png?ex=1&width=1568&height=784"
    heavy = {"url": "u", "proxy_url": "https://media.discordapp.net/a.png", "size": 9 << 20}
    assert sized(heavy) == "https://media.discordapp.net/a.png?width=1568"


def test_downloads_only_from_discord(monkeypatch):
    assert allowed(f"{CDN}/a.png")
    assert allowed("https://media.discordapp.net/a.png?width=10")
    assert not allowed("http://cdn.discordapp.com/a.png")
    assert not allowed("https://cdn.discordapp.com.evil.example/a.png")
    assert not allowed("https://cdn.discordapp.com:8443/a.png")
    assert not allowed("http://127.0.0.1:8090/cdn/a.png")
    monkeypatch.setenv("DISCORD_MEDIA_EXTRA_HOSTS", "127.0.0.1:8090")
    assert allowed("http://127.0.0.1:8090/cdn/a.png")


def serve(routes: dict[str, tuple[int, bytes, str]]):
    def handler(request: httpx.Request) -> httpx.Response:
        status, body, content_type = routes.get(request.url.path, (404, b"", "text/plain"))
        return httpx.Response(status, content=body, headers={"content-type": content_type})

    return httpx.MockTransport(handler)


async def test_download_checks_status_size_and_format(monkeypatch):
    monkeypatch.setattr(fetch, "MAX_BYTES", 10)
    transport = serve(
        {
            "/ok.png": (200, PNG[:9], "image/png"),
            "/untyped": (200, JPEG[:9], "application/octet-stream"),
            "/renamed.png": (200, JPEG[:9], "image/png"),
            "/big.png": (200, b"x" * 11, "image/png"),
            "/page.png": (200, b"<html>", "text/html"),
            "/svg": (200, b"<svg/>", "image/svg+xml"),
            "/down.png": (503, b"", "text/plain"),
        }
    )
    async with httpx.AsyncClient(transport=transport) as client:

        async def get(path, media_type=None):
            return await fetch.download(client, f"https://cdn.discordapp.com{path}", media_type)

        ok = await get("/ok.png")
        assert isinstance(ok, BinaryContent) and ok.media_type == "image/png"
        # The bytes decide the type, not the header or the file name.
        assert (await get("/untyped")).media_type == "image/jpeg"
        assert (await get("/renamed.png", "image/png")).media_type == "image/jpeg"
        assert await get("/gone.png") == "no longer available (deleted, or an expired Discord link)"
        assert await get("/big.png") == "too large"
        assert await get("/page.png", "image/png") == "unsupported format (text/html)"
        assert await get("/svg", "image/svg+xml") == "unsupported format (image/svg+xml)"
        assert await get("/down.png") == "download failed (HTTP 503)"
        assert await fetch.download(client, "https://example.com/a.png") == (
            "not hosted by Discord"
        )


# --- with Postgres -------------------------------------------------------------


@pytest.fixture
async def db(database_url):
    async with await AsyncConnection.connect(database_url, autocommit=True) as connection:
        await connection.execute(
            "INSERT INTO channels (id, guild_id, name, type) VALUES (%s, %s, 'general', 0)",
            (CHANNEL, GUILD),
        )
        yield connection


async def add(db, id, content, raw_extra, *, guild=GUILD, author_id=100, bot=False):
    raw = {"author": {"id": str(author_id), "username": "alex"}, "mentions": [], **raw_extra}
    await db.execute(
        "INSERT INTO messages (id, guild_id, channel_id, author_id, author_name, author_bot,"
        " content, created_at, raw) VALUES (%s, %s, %s, %s, 'alex', %s, %s, now(), %s)",
        (id, guild, CHANNEL, author_id, bot, content, json.dumps(raw)),
    )


ROSTER = Roster({"u1": 100}, {100: "u1 Alex"})
CDN_FILES = {
    "/attachments/2/3/a.png": (200, PNG, "image/png"),
    "/attachments/2/3/b.JPG": (200, JPEG, "image/jpeg"),
    "/attachments/2/3/c.webp": (200, WEBP, "image/webp"),
    "/x.png": (200, PNG, "image/png"),
}


def fake_vision(answer="Image 1: a stage.", error: Exception | None = None, fallback=False):
    seen = []

    def respond(messages, info: AgentInfo) -> ModelResponse:
        seen.append((messages, info))
        if error:
            raise error
        return ModelResponse(parts=[TextPart(answer)], provider_details={"cost": 0.002})

    return Vision(FunctionModel(respond), "test/vision", fallback), seen


async def read(db, vision_model, message_id=70, question=None, files=CDN_FILES):
    return await read_image(
        db, SCOPE, ROSTER, vision_model, message_id, question, transport=serve(files)
    )


async def test_reads_a_gallery_image_by_image_and_as_a_whole(db):
    await add(db, 70, "opening night!!", GALLERY)
    model, seen = fake_vision("Image 1: ...\nGallery: the whole show.")
    result = await read(db, model, question="who is on stage?")
    assert result["description"] == "Image 1: ...\nGallery: the whole show."
    assert result["message"]["author"] == "u1 Alex"
    assert result["message"]["url"] == "https://discord.com/channels/1/2/70"
    assert [i.get("error") for i in result["images"]] == [None] * 4 + ["not hosted by Discord"]
    assert model.cost == pytest.approx(0.002) and model.calls == 1

    [(messages, info)] = seen
    assert "Gallery:" in messages[0].instructions
    parts = messages[0].parts[0].content
    assert 'Message text: "opening night!!"' in parts[0]
    assert "who is on stage?" in parts[0]
    labels = [p for p in parts[1:] if isinstance(p, str)]
    assert labels[:4] == [
        "Image 1: a.png",
        "Image 2: b.JPG",
        'Image 3: c.webp (alt text: "a cat on a piano")',
        "Image 4: linked image",
    ]
    assert "Image(s) 5 couldn't be loaded" in labels[-1]
    assert [p.media_type for p in parts if isinstance(p, BinaryContent)] == [
        "image/png",
        "image/jpeg",
        "image/webp",
        "image/png",
    ]
    # 600 + 400 per image for the description, plus room to think first.
    assert info.model_settings["max_tokens"] == 2200 + REASONING_TOKENS


async def test_thinking_gets_room_beyond_the_answer():
    """A reasoning model thinks inside `max_tokens`: on production galleries
    DeepSeek used 400 to 1,100 tokens of a 1,400 cap, so descriptions failed
    (no text) or were cut off."""
    model, seen = fake_vision("Image 1: a cat.")
    assert await look(model, ["what is this"], max_tokens=300) == "Image 1: a cat."
    assert await look(model, ["yes or no"], max_tokens=300, think=False) == "Image 1: a cat."
    assert [info.model_settings["max_tokens"] for _, info in seen] == [
        300 + REASONING_TOKENS,
        300,
    ]


async def test_a_cut_off_description_says_so():
    def respond(messages, info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart("Image 1: a person in a wi")], finish_reason="length")

    vision = Vision(FunctionModel(respond), "test/vision")
    text = await look(vision, ["describe"], max_tokens=100)
    assert text == "Image 1: a person in a wi\n(The description was cut off here.)"


def thinks_too_long(answer="Image 1: a dog lying on the floor."):
    """Like DeepSeek V4.1 Flash on 10-20 (reply:399, 401, 402): with thinking on, it
    thinks until the token limit and writes nothing; without, it answers."""
    seen = []

    def respond(messages, info: AgentInfo) -> ModelResponse:
        seen.append(info.model_settings)
        if (info.model_settings.get("openrouter_reasoning") or {}).get("enabled") is False:
            return ModelResponse(parts=[TextPart(answer)], provider_details={"cost": 0.001})
        return ModelResponse(
            parts=[ThinkingPart("hmm " * 50)],
            finish_reason="length",
            provider_details={"cost": 0.003},
        )

    return Vision(FunctionModel(respond), "test/vision"), seen


async def test_thinking_that_runs_out_is_asked_again_without_thinking():
    vision, seen = thinks_too_long()
    assert await look(vision, ["describe"], max_tokens=100) == "Image 1: a dog lying on the floor."
    assert [s["max_tokens"] for s in seen] == [100 + REASONING_TOKENS, 100]
    assert "openrouter_reasoning" not in seen[0]
    assert seen[1]["openrouter_reasoning"] == {"enabled": False}
    # Both requests are billed, the one that only thought too.
    assert vision.calls == 2
    assert vision.cost == pytest.approx(0.004)


async def test_a_read_survives_thinking_that_runs_out(db):
    await add(
        db,
        80,
        "This is nougat. Identify his color pattern.",
        {"attachments": [attachment("a.png")]},
    )
    vision, _ = thinks_too_long()
    result = await read(db, vision, 80)
    assert result["description"] == "Image 1: a dog lying on the floor."
    assert "error" not in result


async def test_no_answer_even_without_thinking_is_an_error_for_kiki():
    def respond(messages, info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[ThinkingPart("hmm " * 50)], finish_reason="length")

    vision = Vision(FunctionModel(respond), "test/vision")
    with pytest.raises(ImageRefused, match="ran out of room") as refused:
        await look(vision, ["describe"], max_tokens=100)
    # Kiki is told not to fill the gap herself.
    assert "rather than guessing" in str(refused.value)
    assert vision.calls == 2
    with pytest.raises(ImageRefused, match="ran out of room"):
        await look(vision, ["yes or no"], max_tokens=100, think=False)
    assert vision.calls == 3  # without thinking there's nothing to turn off


async def test_reads_a_single_image(db):
    await add(db, 71, "", {"attachments": [attachment("a.png")]})
    model, seen = fake_vision()
    result = await read(db, model, 71)
    assert result["description"] == "Image 1: a stage."
    assert result["images"] == [{"image": 1, "name": "a.png"}]
    assert "No message text." in seen[0][0][0].parts[0].content[0]


async def test_explains_messages_it_cannot_read(db):
    await add(db, 72, "no pictures", {"attachments": [attachment("notes.pdf", "text/plain")]})
    await add(db, 73, "expired", {"attachments": [attachment("gone.png")]})
    await add(db, 74, "elsewhere", {"attachments": [attachment("a.png")]}, guild=7)
    model, seen = fake_vision()
    assert await read(db, model, 72) == {"error": "that message has no images"}
    assert (await read(db, model, 73))["error"] == "none of the images could be downloaded"
    assert await read(db, model, 74) == {"error": "no such message in this server"}
    assert await read(db, model, 999) == {"error": "no such message in this server"}
    assert seen == []


async def test_reads_only_messages_kiki_may_see(db):
    await add(db, 78, "deleted", {"attachments": [attachment("a.png")]})
    await db.execute("UPDATE messages SET deleted_at = now() WHERE id = 78")
    await add(db, 79, "ignored", {"attachments": [attachment("a.png")]}, author_id=200)
    await db.execute("INSERT INTO ignored_authors (user_id) VALUES (200)")
    model, seen = fake_vision()
    assert await read(db, model, 78) == {"error": "no such message in this server"}
    assert await read(db, model, 79) == {"error": "no such message in this server"}
    assert seen == []


@pytest.mark.parametrize(
    ("status", "fallback", "expected"),
    [
        (404, True, "No vision model is configured (OPENROUTER_VISION_MODEL)"),
        (400, False, "The image model test/vision rejected these images."),
        (503, True, "The image model is unavailable right now"),
        (429, False, "The image model is unavailable right now"),
    ],
)
async def test_vision_failures_become_errors_for_kiki(db, status, fallback, expected):
    await add(db, 75, "", {"attachments": [attachment("a.png")]})
    error = ModelHTTPError(status, "test/vision", body={"message": "No endpoints support images"})
    model, _ = fake_vision(error=error, fallback=fallback)
    result = await read(db, model, 75)
    assert expected in result["error"]
    assert result["images"] == [{"image": 1, "name": "a.png"}]
    assert model.cost == 0


async def test_search_results_say_which_messages_have_images(db):
    await add(db, 76, "plain", {})
    await add(db, 77, "pics", GALLERY)
    result = await message_context(db, SCOPE, 76)
    assert [m.get("images") for m in result["messages"]] == [None, 5]


async def test_kiki_reads_her_own_drawings_from_the_store(db):
    """generate_image's saved bytes, not Discord's (expiring) CDN link; guild-scoped."""
    for job, guild in (("reply:1", GUILD), ("reply:2", 7)):
        await db.execute(
            "INSERT INTO agent_host_jobs (id, request, status) VALUES (%s, %s, 'completed')",
            (job, json.dumps({"scope": {"guild_id": str(guild)}})),
        )
    cursor = await db.execute(
        "INSERT INTO agent_host_images (job_id, mime_type, data) VALUES"
        " ('reply:1', 'image/png', %s), ('reply:2', 'image/png', %s) RETURNING id",
        (PNG, PNG),
    )
    mine, theirs = [row[0] for row in await cursor.fetchall()]
    files = [attachment(f"kiki-{i}.png") for i in (mine, theirs)]
    await add(db, 80, "here you go", {"attachments": files}, author_id=5, bot=True)
    model, seen = fake_vision()
    # Only the other guild's drawing is downloaded, and the CDN has neither.
    result = await read(db, model, 80, files={})
    assert result["images"] == [
        {"image": 1, "name": f"kiki-{mine}.png", "drawn_by_kiki": True},
        {
            "image": 2,
            "name": f"kiki-{theirs}.png",
            "error": "no longer available (deleted, or an expired Discord link)",
        },
    ]
    parts = seen[0][0][0].parts[0].content
    assert f"Image 1: kiki-{mine}.png (Kiki drew this with an image model)" in parts

    # Someone else's upload with the same name is just an upload.
    await add(db, 81, "", {"attachments": [attachment(f"kiki-{mine}.png")]})
    result = await read(db, model, 81, files={})
    assert result["error"] == "none of the images could be downloaded"


async def test_a_slow_thinking_read_leaves_its_retry_time(monkeypatch):
    """reply:461 on 10-20: a thinking vision call ran 41 s into read_image's 45 s,
    so the request without thinking never happened. Thinking now stops in time
    to leave UNTHINKING_SECONDS of the deadline for it."""
    import asyncio
    import time

    from kiki_agent_host.deadline import run_deadline
    from kiki_agent_host.media import vision as vision_module

    seen = []

    async def respond(messages, info: AgentInfo) -> ModelResponse:
        seen.append(info.model_settings)
        if (info.model_settings.get("openrouter_reasoning") or {}).get("enabled") is False:
            return ModelResponse(parts=[TextPart("Image 1: a dog.")])
        await asyncio.sleep(30)  # thinking, and thinking

    monkeypatch.setattr(vision_module, "UNTHINKING_SECONDS", 0.4)
    vision = Vision(FunctionModel(respond), "test/vision")
    started = time.monotonic()
    with run_deadline(1.0):  # read_image's deadline, as the guard sets it
        assert await look(vision, ["describe"], max_tokens=100) == "Image 1: a dog."
    assert time.monotonic() - started < 1.0
    assert len(seen) == 2

    # Without a deadline (emoji descriptions, say), nothing is cut.
    fast = Vision(billed_answer("Image 1: a cat."), "test/vision")
    assert await look(fast, ["describe"], max_tokens=100) == "Image 1: a cat."


def billed_answer(text):
    return FunctionModel(lambda messages, info: ModelResponse(parts=[TextPart(text)]))
