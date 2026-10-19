"""`read_link`'s X reader, against recorded FxTwitter responses
(`fixtures/fxtwitter/`) and an `httpx.MockTransport`; nothing here reaches the
network. The shared dispatcher and tool are covered in `test_links.py`."""

import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from pydantic_ai.messages import (
    BinaryContent,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from kiki_agent_host.agent.executor import PydanticExecutor
from kiki_agent_host.contracts import JobRequest
from kiki_agent_host.media import links
from kiki_agent_host.media.links import x
from kiki_agent_host.media.vision import Vision

FIXTURES = Path(__file__).parent / "fixtures/fxtwitter"
JPEG = b"\xff\xd8\xff\xe0sim"
TEXT, PHOTO, VIDEO = "20", "440322224407314432", "1460323737035677698"


def recorded(name: str) -> dict:
    return json.loads(next(FIXTURES.glob(f"*-{name}.json")).read_text())


@pytest.fixture(autouse=True)
def fresh_cache():
    x.cache.clear()
    yield
    x.cache.clear()


def fxtwitter(posts: dict[str, tuple[int, dict]], seen: list[str] | None = None):
    """FxTwitter and X's image host; images are JPEG bytes."""

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(str(request.url))
        if request.url.host == "pbs.twimg.com":
            return httpx.Response(200, content=JPEG, headers={"content-type": "image/jpeg"})
        id = request.url.path.rsplit("/", 1)[-1]
        status, body = posts.get(id, (404, {"code": 404, "message": "NOT_FOUND", "tweet": None}))
        return httpx.Response(status, json=body)

    return httpx.MockTransport(handler)


def serve(monkeypatch, posts=None, seen=None):
    posts = posts or {id: (200, recorded(id)) for id in (TEXT, PHOTO, VIDEO)}
    monkeypatch.setattr(x, "TRANSPORT", fxtwitter(posts, seen))


def fake_vision(answer="Image 1: a crowded selfie."):
    seen = []

    def respond(messages, info: AgentInfo) -> ModelResponse:
        seen.append(messages)
        return ModelResponse(parts=[TextPart(answer)], provider_details={"cost": 0.001})

    return SimpleNamespace(vision=Vision(FunctionModel(respond), "test/vision")), seen


# --- matching -------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://x.com/jack/status/20",
        "https://twitter.com/jack/status/20?s=46&t=abc",
        "https://mobile.twitter.com/jack/status/20/photo/1",
        "https://www.x.com/i/status/20",
        "https://x.com/i/web/status/20",
        "https://vxtwitter.com/jack/status/20",
        "https://fxtwitter.com/jack/status/20",
        "https://d.fxtwitter.com/jack/status/20",
        "https://fixupx.com/jack/status/20",
        "http://twitter.com/jack/statuses/20",
    ],
)
def test_matches_x_status_links(url):
    assert x.matches(url) and x.status_id(url) == "20"
    assert links.reader_for(url) is x


@pytest.mark.parametrize(
    "url",
    [
        "https://x.com/jack",
        "https://x.com/jack/likes",
        "https://notx.com/jack/status/20",
        "https://x.com.evil.example/jack/status/20",
        "https://example.com/x.com/jack/status/20",
        "ftp://x.com/jack/status/20",
        "https://www.reddit.com/r/x/comments/abc/",
    ],
)
def test_ignores_other_links(url):
    assert not x.matches(url)


# --- the X reader -----------------------------------------------------------------


async def test_reads_a_text_post_without_a_vision_call(monkeypatch):
    serve(monkeypatch)
    deps, seen = fake_vision()
    result = await links.read_link(deps, "https://twitter.com/jack/status/20")
    assert result == {
        "url": "https://twitter.com/jack/status/20",
        "site": "X",
        "post": {
            "url": "https://x.com/jack/status/20",
            "author": "jack (@jack)",
            "text": "just setting up my twttr",
            "posted_at": "2006-03-21T20:50+00:00",
            "stats": {
                "replies": 18094,
                "retweets": 124613,
                "quotes": 7322,
                "likes": 309200,
                "bookmarks": 21986,
            },
        },
    }
    assert seen == []


async def test_reads_a_photo_post_through_the_vision_model(monkeypatch):
    requested: list[str] = []
    serve(monkeypatch, seen=requested)
    deps, seen = fake_vision()
    url = "https://x.com/TheEllenShow/status/440322224407314432"
    result = await links.read_link(deps, url, "who is in it?")
    assert result["post"]["author"] == "The Ellen Show (@TheEllenShow)"
    assert result["post"]["text"].startswith("If only Bradley's arm was longer.")
    assert result["post"]["media"] == [{"type": "photo"}]
    assert result["description"] == "Image 1: a crowded selfie."
    assert "note" not in result
    assert deps.vision.cost == pytest.approx(0.001)
    # X's 1200 px rendition, not the original upload.
    assert requested[-1] == "https://pbs.twimg.com/media/BhxWutnCEAAtEQ6.jpg?name=medium"

    [messages] = seen
    parts = messages[0].parts[0].content
    assert "X post by The Ellen Show (@TheEllenShow)" in parts[0]
    assert "Kiki is wondering: who is in it?" in parts[0]
    assert parts[1] == "Image 1: photo"
    assert isinstance(parts[2], BinaryContent) and parts[2].media_type == "image/jpeg"


async def test_videos_get_their_thumbnail_read_and_a_note(monkeypatch):
    serve(monkeypatch)
    deps, seen = fake_vision("Image 1: a title card.")
    result = await links.read_link(deps, f"https://x.com/XDevelopers/status/{VIDEO}")
    assert result["post"]["media"] == [{"type": "video", "seconds": 11}]
    assert result["note"] == x.VIDEO_NOTE
    assert result["description"] == "Image 1: a title card."
    labels = [p for p in seen[0][0].parts[0].content if isinstance(p, str)]
    assert labels[1] == "Image 1: thumbnail of a 11 s video (one frame, not the whole video)"


async def test_quoted_posts_are_read_too(monkeypatch):
    body = recorded(TEXT)
    body["tweet"]["quote"] = recorded(PHOTO)["tweet"]
    serve(monkeypatch, {"21": (200, body)})
    deps, seen = fake_vision()
    result = await links.read_link(deps, "https://x.com/jack/status/21")
    assert result["quote"]["author"] == "The Ellen Show (@TheEllenShow)"
    assert result["quote"]["media"] == [{"type": "photo"}]
    parts = seen[0][0].parts[0].content
    assert 'It quotes a post by The Ellen Show (@TheEllenShow): "If only' in parts[0]
    assert parts[1] == "Image 1: photo in the quoted post"


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (404, {"code": 404, "message": "NOT_FOUND", "tweet": None}, "doesn't exist or was deleted"),
        (401, {"code": 401, "message": "PRIVATE_TWEET", "tweet": None}, "private account"),
        (500, {"code": 500, "message": "API_FAIL", "tweet": None}, "unavailable right now"),
        (502, "<html>bad gateway</html>", "unavailable right now (HTTP 502)"),
        (200, "<!DOCTYPE html><html>", "found no post with that ID"),
    ],
)
async def test_unreadable_posts_are_explained(monkeypatch, status, body, expected):
    def handler(request):
        if isinstance(body, str):
            return httpx.Response(status, text=body)
        return httpx.Response(status, json=body)

    monkeypatch.setattr(x, "TRANSPORT", httpx.MockTransport(handler))
    deps, seen = fake_vision()
    result = await links.read_link(deps, "https://x.com/jack/status/5")
    assert expected in result["error"] and seen == []
    assert x.cache == {}  # errors aren't cached


async def test_responses_are_bounded(monkeypatch):
    monkeypatch.setattr(x, "MAX_RESPONSE_BYTES", 100)
    serve(monkeypatch)
    result = await links.read_link(fake_vision()[0], "https://x.com/jack/status/20")
    assert result["error"] == "The X post's data was too large to read."


async def test_images_come_only_from_x(monkeypatch):
    body = recorded(PHOTO)
    body["tweet"]["media"]["all"][0]["url"] = "https://169.254.169.254/latest/meta-data"
    serve(monkeypatch, {PHOTO: (200, body)})
    deps, seen = fake_vision()
    result = await links.read_link(deps, f"https://x.com/a/status/{PHOTO}")
    assert result["description_error"] == "none of the images could be downloaded"
    assert seen == []


async def test_results_are_cached_per_post_and_question(monkeypatch):
    requested: list[str] = []
    serve(monkeypatch, seen=requested)
    deps, seen = fake_vision()
    first = await links.read_link(deps, f"https://x.com/a/status/{PHOTO}")
    again = await links.read_link(deps, f"https://vxtwitter.com/a/status/{PHOTO}")
    assert again["post"] == first["post"] and len(requested) == 2 and len(seen) == 1
    await links.read_link(deps, f"https://x.com/a/status/{PHOTO}", "what's the joke?")
    assert len(requested) == 4 and len(seen) == 2


# --- the tool, through a reply run (Postgres) ------------------------------------


async def test_kiki_reads_a_link_by_message_id_or_url(database_url, monkeypatch):
    from psycopg import AsyncConnection

    async with await AsyncConnection.connect(database_url, autocommit=True) as db:
        await db.execute("INSERT INTO channels (id, guild_id, name, type) VALUES (2, 1, 'g', 0)")
        for id, guild, content in (
            (70, 1, "lol https://example.com/a and <https://x.com/jack/status/20>"),
            (71, 1, "no links here"),
            (72, 7, "https://x.com/jack/status/20"),
        ):
            await db.execute(
                "INSERT INTO messages (id, guild_id, channel_id, author_id, author_name,"
                " content, created_at, raw) VALUES (%s, %s, 2, 100, 'alex', %s, now(), '{}')",
                (id, guild, content),
            )
    serve(monkeypatch)
    calls = [  # four: a reply's lookup budget
        {"url_or_message_id": "70"},
        {"url_or_message_id": "71"},
        {"url_or_message_id": "72"},
        {"url_or_message_id": "https://fxtwitter.com/jack/status/20"},
    ]
    returns: list[ToolReturnPart] = []

    def respond(messages, info: AgentInfo) -> ModelResponse:
        if not any(isinstance(m, ModelResponse) for m in messages):
            parts = [ToolCallPart("read_link", a, tool_call_id=str(i)) for i, a in enumerate(calls)]
            return ModelResponse(parts=parts)
        returns.extend(p for p in messages[-1].parts if isinstance(p, ToolReturnPart))
        return ModelResponse(parts=[TextPart("done")])

    class TestExecutor(PydanticExecutor):
        web_tools = ()

        def model(self, name):
            return FunctionModel(respond)

    executor = TestExecutor(database_url, "unused-key")
    request = JobRequest.model_validate(
        {
            "id": "reply:links",
            "kind": "reply",
            "scope": {"guild_id": "1", "channel_id": "2", "conversation_id": "2"},
            "model": "test/model",
            "messages": [{"role": "user", "content": "alex: what's this tweet"}],
            "max_output_tokens": 100,
            "people": [{"ref": "u1", "user_id": "100", "label": "alex", "names": ["alex"]}],
        }
    )
    assert (await executor.execute(request)).text == "done"
    by_call = {int(r.tool_call_id): r.content for r in returns}
    assert by_call[0]["post"]["text"] == "just setting up my twttr"  # the X link, not the first
    assert by_call[1] == {"error": "that message has no links"}
    assert by_call[2] == {"error": "no such message in this server"}  # another guild
    assert by_call[3]["post"]["author"] == "jack (@jack)"
