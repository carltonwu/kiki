"""`read_link` and its Reddit reader, against recorded-format JSON (`fixtures/reddit/`)
and an `httpx.MockTransport`; nothing here reaches the network."""

import json
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from psycopg import AsyncConnection
from pydantic_ai.messages import (
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from kiki_agent_host.agent.executor import PydanticExecutor
from kiki_agent_host.agent.tools import links as link_tools
from kiki_agent_host.chat.scope import Scope
from kiki_agent_host.contracts import JobRequest
from kiki_agent_host.media import links
from kiki_agent_host.media.links import reddit
from kiki_agent_host.media.vision import Vision

FIXTURES = Path(__file__).parent / "fixtures/reddit"
PNG = b"\x89PNG\r\n\x1a\nsim"
JPEG = b"\xff\xd8\xff\xe0sim"
TEXT_POST = "https://www.reddit.com/r/Musicals/comments/1abcde/why_does_the_second_act/"


def fixture(name: str) -> list:
    return json.loads((FIXTURES / f"{name}.json").read_text())


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    """No cache or token between tests, no credentials, and every request recorded."""
    reddit._cache.clear()
    monkeypatch.setattr(reddit, "_token", None)
    monkeypatch.delenv("REDDIT_CLIENT_ID", raising=False)
    monkeypatch.delenv("REDDIT_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("REDDIT_BASE_URL", raising=False)


def serve(monkeypatch, routes: dict) -> list[httpx.Request]:
    """Answers `host + path` from `routes`: (status, JSON or bytes[, headers])."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        status, body, *headers = routes.get(
            request.url.host + request.url.path, (404, {"reason": None})
        )
        if isinstance(body, bytes):
            return httpx.Response(status, content=body, headers=headers[0] if headers else {})
        return httpx.Response(status, json=body, headers=headers[0] if headers else {})

    monkeypatch.setattr(reddit, "TRANSPORT", httpx.MockTransport(handler))
    return seen


def fake_vision(answer="Image 1: a cat.\nGallery: before and after."):
    seen = []

    def respond(messages, info: AgentInfo) -> ModelResponse:
        seen.append(messages)
        return ModelResponse(parts=[TextPart(answer)], provider_details={"cost": 0.001})

    return Vision(FunctionModel(respond), "test/vision"), seen


class FakeEmbeddings:
    async def create(self, model, input):
        data = [SimpleNamespace(index=i, embedding=[0.1] * 1024) for i in range(len(input))]
        return SimpleNamespace(data=data, usage=SimpleNamespace(cost=0))


def deps(vision=None):
    return SimpleNamespace(vision=vision or fake_vision()[0])


# --- URLs ---------------------------------------------------------------------


def test_matches_reddit_hosts_only():
    for url in [
        TEXT_POST,
        "https://reddit.com/r/x/comments/abc",
        "https://old.reddit.com/r/x/comments/abc/t/",
        "https://new.reddit.com/r/x/s/AbC123",
        "https://redd.it/abc",
    ]:
        assert reddit.matches(url), url
    for url in [
        "https://reddit.com.evil.example/r/x/comments/abc",
        "https://notreddit.com/r/x/comments/abc",
        "https://i.redd.it/abc.png",
        "https://x.com/someone/status/1",
    ]:
        assert not reddit.matches(url), url


def test_post_ids_from_each_link_form():
    assert reddit.post_id(TEXT_POST) == ("1abcde", None)
    assert reddit.post_id("https://www.reddit.com/r/x/comments/1ABCDE/t/c00002/?ctx=3") == (
        "1abcde",
        "c00002",
    )
    assert reddit.post_id("https://redd.it/1abcde") == ("1abcde", None)
    assert reddit.post_id("https://www.reddit.com/gallery/1gal01") == ("1gal01", None)
    assert reddit.post_id("https://www.reddit.com/r/x/s/AbC123") is None  # resolved by request
    assert reddit.post_id("https://www.reddit.com/r/Musicals/") is None


def test_links_are_normalized_before_dispatch():
    assert links.normalize("<https://redd.it/abc>") == "https://redd.it/abc"
    assert links.normalize("reddit.com/r/x/comments/abc).") == "https://reddit.com/r/x/comments/abc"
    assert links.normalize("not a link") is None
    assert links.reader_for("https://redd.it/abc") is reddit
    assert links.reader_for("https://example.com/") is None


async def test_unknown_sites_point_to_web_fetch():
    result = await links.read_link(deps(), "https://example.com/post")
    assert result == {
        "url": "https://example.com/post",
        "error": "No reader for this site. Use web fetch to read the page instead.",
    }
    with pytest.raises(ValueError):
        await links.read_link(deps(), "two words")


async def test_share_links_follow_one_redirect(monkeypatch):
    seen = serve(
        monkeypatch,
        {
            "www.reddit.com/r/Musicals/s/AbC123": (
                301,
                b"",
                {"location": TEXT_POST + "?share_id=x&utm_source=share"},
            ),
            "www.reddit.com/comments/1abcde.json": (200, fixture("text")),
        },
    )
    result = await links.read_link(deps(), "https://www.reddit.com/r/Musicals/s/AbC123")
    assert result["post"]["title"].startswith("Why does the second act")
    assert [r.url.path for r in seen] == ["/r/Musicals/s/AbC123", "/comments/1abcde.json"]
    assert seen[1].headers["user-agent"] == reddit.USER_AGENT
    assert seen[1].url.params["raw_json"] == "1"


async def test_share_links_that_dont_redirect_to_a_post_are_errors(monkeypatch):
    serve(
        monkeypatch,
        {"www.reddit.com/r/x/s/Nope": (302, b"", {"location": "https://www.reddit.com/login"})},
    )
    result = await links.read_link(deps(), "https://www.reddit.com/r/x/s/Nope")
    assert "didn't resolve this share link" in result["error"]


# --- parsing ------------------------------------------------------------------


def test_a_text_post_with_its_top_comments():
    result, images = reddit.parse(fixture("text"))
    assert images == []
    assert result["post"] == {
        "title": "Why does the second act of every jukebox musical fall apart?",
        "subreddit": "r/Musicals",
        "author": "u/stagedoor_sam",
        "score": 1532,
        "upvote_ratio": 0.94,
        "comments": 214,
        "posted": "2025-10-09",
        "url": "https://www.reddit.com/r/Musicals/comments/1abcde/"
        "why_does_the_second_act_of_every_jukebox_musical/",
        "flair": "Discussion",
        "text": fixture("text")[0]["data"]["children"][0]["data"]["selftext"],
    }
    # The pinned AutoModerator note and the removed comment are left out.
    assert result["top_comments"] == [
        {
            "author": "u/orchestra_pit",
            "score": 812,
            "text": "The first act gets to use all the hits. By act two they're down to deep "
            "cuts, so the plot has to do the work it skipped earlier.",
        },
        {
            "author": "u/swing_understudy",
            "score": 97,
            "text": "Hot take: it's the cast. I saw the original company and act two was the "
            "best part.",
        },
    ]
    assert "error" not in result and "video" not in result


def test_long_text_and_comments_are_trimmed_and_a_linked_comment_comes_first(monkeypatch):
    data = fixture("text")
    data[0]["data"]["children"][0]["data"]["selftext"] = "word " * 2000
    data[1]["data"]["children"][3]["data"]["body"] = "x" * 5000
    result, _ = reddit.parse(data, comment="c00004")
    assert len(result["post"]["text"]) == reddit.MAX_TEXT_CHARS
    assert result["post"]["text"].endswith("…")
    assert result["linked_comment"]["author"] == "u/swing_understudy"
    assert len(result["linked_comment"]["text"]) == reddit.MAX_COMMENT_CHARS
    assert [c["author"] for c in result["top_comments"]] == ["u/orchestra_pit"]


def test_removed_posts_say_so_and_keep_what_is_left():
    data = fixture("text")
    post = data[0]["data"]["children"][0]["data"]
    post |= {"selftext": "[removed]", "removed_by_category": "moderator"}
    result, _ = reddit.parse(data)
    assert result["error"] == "This post was removed (moderator); only what's left of it is shown."
    assert "text" not in result["post"]
    assert result["top_comments"]


def test_a_gallery_lists_its_valid_images_at_preview_size():
    result, images = reddit.parse(fixture("gallery"))
    assert images == [
        ("https://preview.redd.it/img1aaa.jpg?width=1080&format=pjpg&s=3", "before"),
        ("https://i.redd.it/img2bbb.png", ""),
    ]
    assert "link" not in result["post"]  # the gallery URL is Reddit's own


def test_a_video_is_metadata_with_a_note():
    result, images = reddit.parse(fixture("video"))
    assert images == []
    assert result["video"]["duration_s"] == 34
    assert result["video"]["size"] == "720x1280"
    assert "isn't supported yet" in result["video"]["note"]
    assert result["top_comments"][0]["text"] == "Eleven seconds for the full gown. Unreal."


def test_a_link_post_carries_its_link_and_a_crosspost_uses_the_original_media():
    data = fixture("text")
    post = data[0]["data"]["children"][0]["data"]
    post |= {"is_self": False, "selftext": "", "url": "https://news.example/story"}
    result, _ = reddit.parse(data)
    assert result["post"]["link"] == "https://news.example/story"

    crossposted = fixture("text")
    original = fixture("video")[0]["data"]["children"][0]["data"]
    crossposted[0]["data"]["children"][0]["data"]["crosspost_parent_list"] = [original]
    result, _ = reddit.parse(crossposted)
    assert result["post"]["crosspost_of"] == "r/Broadway"
    assert result["video"]["duration_s"] == 34


# --- reading ------------------------------------------------------------------


async def test_a_gallery_is_described_by_the_vision_model_and_cached(monkeypatch):
    seen = serve(
        monkeypatch,
        {
            "www.reddit.com/comments/1gal01.json": (200, fixture("gallery")),
            "preview.redd.it/img1aaa.jpg": (200, JPEG, {"content-type": "image/jpeg"}),
            "i.redd.it/img2bbb.png": (200, PNG, {"content-type": "image/png"}),
        },
    )
    vision, prompts = fake_vision()
    url = "https://www.reddit.com/gallery/1gal01"
    result = await links.read_link(deps(vision), url, "is the cat ok?")
    assert result["description"] == "Image 1: a cat.\nGallery: before and after."
    assert result["images"] == [{"image": 1, "caption": "before"}, {"image": 2}]
    assert vision.calls == 1 and vision.cost == pytest.approx(0.001)
    parts = prompts[0][0].parts[0].content
    assert parts[0].startswith("From a Reddit post in r/cats by u/grey_cat_dad, 2 image(s).")
    assert "Kiki is wondering: is the cat ok?" in parts[0]
    assert parts[1] == 'Image 1 (caption: "before")' and parts[3] == "Image 2"

    again = await links.read_link(deps(vision), url, "is the cat ok?")
    assert again["cached"] is True and again["description"] == result["description"]
    assert len(seen) == 3 and vision.calls == 1


async def test_images_only_come_from_reddit_hosts(monkeypatch):
    data = fixture("gallery")
    meta = data[0]["data"]["children"][0]["data"]["media_metadata"]
    meta["img2bbb"]["s"]["u"] = "http://169.254.169.254/latest/meta-data"
    seen = serve(
        monkeypatch,
        {
            "www.reddit.com/comments/1gal01.json": (200, data),
            "preview.redd.it/img1aaa.jpg": (200, JPEG),
        },
    )
    result = await links.read_link(deps(), "https://redd.it/1gal01")
    assert result["images"][1] == {"image": 2, "error": "not hosted by Reddit"}
    assert "169.254.169.254" not in {r.url.host for r in seen}


async def test_a_video_post_skips_vision(monkeypatch):
    serve(monkeypatch, {"www.reddit.com/comments/1vid01.json": (200, fixture("video"))})
    vision, _ = fake_vision()
    result = await links.read_link(deps(vision), "https://redd.it/1vid01")
    assert result["video"]["duration_s"] == 34 and vision.calls == 0


@pytest.mark.parametrize(
    ("status", "body", "error"),
    [
        (403, {"reason": "private", "message": "Forbidden"}, "private subreddit"),
        (403, {"reason": "quarantined"}, "quarantined subreddit"),
        (404, {"reason": "banned"}, "has been banned"),
        (404, {"message": "Not Found", "error": 404}, "doesn't exist"),
        (403, b"<html>blocked by network security</html>", "Reddit blocked the request"),
        (429, {"message": "Too Many Requests"}, "rate limiting"),
        (500, b"", "unavailable right now"),
    ],
)
async def test_unreadable_posts_get_clear_errors(monkeypatch, status, body, error):
    serve(monkeypatch, {"www.reddit.com/comments/1abcde.json": (status, body)})
    result = await links.read_link(deps(), TEXT_POST)
    assert error in result["error"]
    assert not reddit._cache  # errors aren't cached


async def test_oversized_responses_are_refused(monkeypatch):
    monkeypatch.setattr(reddit, "MAX_BYTES", 100)
    serve(monkeypatch, {"www.reddit.com/comments/1abcde.json": (200, fixture("text"))})
    result = await links.read_link(deps(), TEXT_POST)
    assert result["error"] == "The post is too large to read."


async def test_credentials_use_an_app_only_oauth_token(monkeypatch):
    monkeypatch.setenv("REDDIT_CLIENT_ID", "id")
    monkeypatch.setenv("REDDIT_CLIENT_SECRET", "secret")
    seen = serve(
        monkeypatch,
        {
            "www.reddit.com/api/v1/access_token": (
                200,
                {"access_token": "tok", "token_type": "bearer", "expires_in": 86400},
            ),
            "oauth.reddit.com/comments/1abcde": (200, fixture("text")),
        },
    )
    result = await links.read_link(deps(), TEXT_POST)
    assert result["post"]["score"] == 1532
    token, post = seen
    assert token.headers["authorization"] == "Basic aWQ6c2VjcmV0"
    assert token.content == b"grant_type=client_credentials"
    assert post.headers["authorization"] == "Bearer tok"

    reddit._cache.clear()
    await links.read_link(deps(), TEXT_POST)
    assert [r.url.host for r in seen[2:]] == ["oauth.reddit.com"]  # the token is reused


# --- the tool -----------------------------------------------------------------


def tool_context(connection=None):
    return SimpleNamespace(
        deps=SimpleNamespace(
            connection=connection,
            scope=Scope(1, 2, 2),
            vision=fake_vision()[0],
            spent=Counter(),
            job_id="reply:links",
        )
    )


async def test_the_tool_reads_a_url_within_the_lookup_budget(monkeypatch):
    serve(monkeypatch, {"www.reddit.com/comments/1abcde.json": (200, fixture("text"))})
    ctx = tool_context()
    result = await link_tools.read_link(ctx, f" {TEXT_POST} ")
    assert result["url"] == TEXT_POST and result["site"] == "reddit"
    assert ctx.deps.spent["Tool"] == 1
    bad = await link_tools.read_link(ctx, "")
    assert bad == {"error": "url must be a link (https://...) or a message ID"}


@pytest.fixture
async def db(database_url):
    async with await AsyncConnection.connect(database_url, autocommit=True) as connection:
        await connection.execute(
            "INSERT INTO channels (id, guild_id, name, type) VALUES (2, 1, 'general', 0)"
        )
        yield connection


async def add(db, id, content, embeds=(), guild=1):
    raw = {"author": {"id": "100", "username": "alex"}, "embeds": list(embeds)}
    await db.execute(
        "INSERT INTO messages (id, guild_id, channel_id, author_id, author_name, author_bot,"
        " content, created_at, raw) VALUES (%s, %s, 2, 100, 'alex', false, %s, now(), %s)",
        (id, guild, content, json.dumps(raw)),
    )


async def test_a_message_id_reads_its_first_readable_link(db, monkeypatch):
    serve(monkeypatch, {"www.reddit.com/comments/1abcde.json": (200, fixture("text"))})
    await add(db, 80, f"lol <https://example.com/a> and {TEXT_POST}.")
    await add(db, 81, "no links here")
    await add(db, 82, "only a preview", [{"type": "link", "url": "https://redd.it/1abcde"}])
    await add(db, 83, TEXT_POST, guild=9)
    ctx = tool_context(db)
    assert (await link_tools.read_link(ctx, "80"))["post"]["score"] == 1532
    assert (await link_tools.read_link(ctx, "82"))["url"] == "https://redd.it/1abcde"
    assert await link_tools.read_link(ctx, "81") == {"error": "that message has no links"}
    assert await link_tools.read_link(ctx, "83") == {"error": "no such message in this server"}


async def test_the_reply_agent_offers_and_runs_read_link(database_url, monkeypatch):
    serve(monkeypatch, {"www.reddit.com/comments/1abcde.json": (200, fixture("text"))})
    returns: list[ToolReturnPart] = []

    def respond(messages, info: AgentInfo) -> ModelResponse:
        assert "read_link" in {t.name for t in info.function_tools}
        returns.extend(p for p in messages[-1].parts if isinstance(p, ToolReturnPart))
        if not returns:
            return ModelResponse(
                parts=[ToolCallPart("read_link", {"url_or_message_id": TEXT_POST})]
            )
        return ModelResponse(parts=[TextPart(returns[0].content["top_comments"][0]["author"])])

    class TestExecutor(PydanticExecutor):
        web_tools = ()

        def model(self, name):
            return FunctionModel(respond)

    request = JobRequest.model_validate(
        {
            "id": "reply:links",
            "kind": "reply",
            "scope": {"guild_id": "1", "channel_id": "2", "conversation_id": "2"},
            "model": "test/model",
            "messages": [{"role": "user", "content": f"alex: kiki thoughts? {TEXT_POST}"}],
            "max_output_tokens": 100,
        }
    )
    executor = TestExecutor(database_url, "unused-key")
    executor.embeddings = SimpleNamespace(embeddings=FakeEmbeddings())
    assert (await executor.execute(request)).text == "u/orchestra_pit"
