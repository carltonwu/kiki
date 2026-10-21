"""X (Twitter) posts, through the FxTwitter API (`api.fxtwitter.com/status/<id>`):
text, author, stats, the quoted post and media, with no X API keys. Links from
x.com, twitter.com and the embed fixers (vxtwitter, fxtwitter, fixupx) all carry
the same status ID.

Discord's own preview of an X link (the embed in `messages.raw`) isn't used: it
has the text and at most one image, but no stats, quoted post or video, and
links pasted in <...> or in a bot's rewrite have none at all.

Photos, and video and GIF thumbnails, are read by the vision model like
`read_image`'s. Videos themselves aren't read yet (a shared video reader comes
later); the result says so.
"""

import asyncio
import json
import re
import time
from collections import OrderedDict
from datetime import UTC, datetime
from urllib.parse import parse_qsl, urlencode, urlsplit

import httpx
from pydantic_ai.messages import UserContent

from ... import faults
from ...config import extra_media_hosts, fxtwitter_api
from ..fetch import download
from ..reading import INSTRUCTIONS
from ..vision import ImageRefused, look

HOSTS = ("x.com", "twitter.com", "vxtwitter.com", "fxtwitter.com", "fixupx.com", "fixvx.com")
# /<user>/status/<id>, /i/status/<id>, /i/web/status/<id>, and the old /statuses/.
STATUS = re.compile(r"^/(?:i/web|i|[A-Za-z0-9_]{1,15})/status(?:es)?/([0-9]{1,20})(?:/|$)")
MEDIA_HOSTS = ("pbs.twimg.com",)

API_TIMEOUT_SECONDS = 8
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_TEXT = 4000  # long posts run to 25,000 characters
MAX_IMAGES = 8  # four per post, the post and its quote
MAX_OUTPUT_TOKENS = 3000
CACHE_SECONDS = 600
CACHE_SIZE = 256
# FxTwitter turns away httpx's default user agent (403).
USER_AGENT = "KikiBot/1.0 (+https://github.com/carltonwu/kiki)"
VIDEO_NOTE = (
    "Full video reading isn't supported yet: only the video's thumbnail was described. "
    "Say so if what happens in the video matters."
)

# Tests swap in an `httpx.MockTransport`.
TRANSPORT: httpx.AsyncBaseTransport | None = None
# (status ID, question) → (expiry, result). Only complete results are kept.
cache: OrderedDict[tuple[str, str], tuple[float, dict]] = OrderedDict()


class Unreadable(Exception):
    """The post can't be read; the message is safe to show the chat model."""


def status_id(url: str) -> str | None:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https"):
        return None
    if not any(host == h or host.endswith("." + h) for h in HOSTS):
        return None
    m = STATUS.match(parts.path)
    return m[1] if m else None


def matches(url: str) -> bool:
    return status_id(url) is not None


async def read(deps, url: str, question: str | None = None) -> dict:
    """The post at `url`, its quoted post, and a description of their images."""
    id = status_id(url)
    if id is None:
        raise ValueError("not a link to an X post")
    question = (question or "").strip()
    key = (id, question.lower())
    if (hit := cache.get(key)) and hit[0] > time.monotonic():
        cache.move_to_end(key)
        return hit[1]

    async with httpx.AsyncClient(
        timeout=API_TIMEOUT_SECONDS,
        follow_redirects=False,
        headers={"User-Agent": USER_AGENT},
        transport=TRANSPORT,
    ) as http:
        try:
            tweet = await fetch(http, id)
        except Unreadable as error:
            return {"error": str(error)}
        result = {"site": "X", "post": summary(tweet)}
        if quote := tweet.get("quote"):
            result["quote"] = summary(quote)
        if any(m["type"] != "photo" for m in media(tweet) + media(quote or {})):
            result["note"] = VIDEO_NOTE
        images = pictures(tweet, "") + pictures(quote or {}, " in the quoted post")
        images = images[:MAX_IMAGES]
        if not images:
            return remember(key, result)
        downloads = await asyncio.gather(
            *(download(http, image, allow=media_host) for _, image in images)
        )

    readable = [
        (label, got)
        for (label, _), got in zip(images, downloads, strict=True)
        if not isinstance(got, str)
    ]
    if not readable:
        return result | {"description_error": "none of the images could be downloaded"}
    post = result["post"]
    prompt: list[UserContent] = [
        f"These are the images of an X post by {post['author']}, shared in the chat."
        + (f' Post text: "{post["text"]}"' if post["text"] else " No post text.")
        + (
            f' It quotes a post by {q["author"]}: "{q["text"]}"'
            if (q := result.get("quote"))
            else ""
        )
        + (f"\nKiki is wondering: {question}" if question else "")
    ]
    for n, (label, content) in enumerate(readable, 1):
        prompt += [f"Image {n}: {label}", content]
    try:
        result["description"] = await look(
            deps.vision,
            prompt,
            instructions=INSTRUCTIONS,
            max_tokens=min(MAX_OUTPUT_TOKENS, 600 + 400 * len(readable)),
        )
    except ImageRefused as error:
        return result | {"description_error": str(error)}
    return remember(key, result)


async def photos(url: str) -> list[str]:
    """The post's photos (and video thumbnails), then its quoted post's, as `read`
    lists them; empty when it can't be read."""
    id = status_id(url)
    if id is None:
        return []
    async with httpx.AsyncClient(
        timeout=API_TIMEOUT_SECONDS,
        follow_redirects=False,
        headers={"User-Agent": USER_AGENT},
        transport=TRANSPORT,
    ) as http:
        try:
            tweet = await fetch(http, id)
        except Unreadable:
            return []
    found = pictures(tweet, "") + pictures(tweet.get("quote") or {}, " in the quoted post")
    return [link for _, link in found[:MAX_IMAGES]]


async def fetch(http: httpx.AsyncClient, id: str) -> dict:
    """The API's `tweet` object, or `Unreadable` with the reason."""
    try:
        async with http.stream("GET", f"{fxtwitter_api()}/status/{id}") as response:
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body += chunk
                if len(body) > MAX_RESPONSE_BYTES:
                    raise Unreadable("The X post's data was too large to read.")
            status = response.status_code
    except httpx.HTTPError as error:
        faults.degraded("read_link.x", "x.fetch", error=error)
        raise Unreadable(f"Couldn't reach the X reader ({type(error).__name__}).") from None
    try:
        data = json.loads(body)
        code = int(data.get("code", status))
    except (ValueError, TypeError, AttributeError):
        data, code = {}, status
    if code == 404:
        raise Unreadable("That X post doesn't exist or was deleted.")
    if code == 401:
        raise Unreadable("That X post is from a private account, so it can't be read.")
    if code == 200 and status == 200:
        # Very old or odd IDs get a web page instead of JSON.
        if not isinstance(data.get("tweet"), dict):
            raise Unreadable("The X reader found no post with that ID.")
        return data["tweet"]
    faults.degraded("read_link.x", "x.fetch", http_status=status)
    raise Unreadable(f"The X reader is unavailable right now (HTTP {status}).")


def summary(tweet: dict) -> dict:
    """What Kiki needs from one post (the API object has much more)."""
    author = tweet.get("author") or {}
    handle = author.get("screen_name") or "unknown"
    text = (tweet.get("text") or "").strip()
    post = {
        "url": tweet.get("url"),
        "author": f"{author.get('name') or handle} (@{handle})",
        "text": text[:MAX_TEXT] + ("…" if len(text) > MAX_TEXT else ""),
    }
    if stamp := tweet.get("created_timestamp"):
        post["posted_at"] = datetime.fromtimestamp(stamp, UTC).isoformat(timespec="minutes")
    if tweet.get("replying_to"):
        post["replying_to"] = "@" + tweet["replying_to"]
    stats = ("replies", "retweets", "quotes", "likes", "bookmarks", "views")
    post["stats"] = {k: tweet[k] for k in stats if isinstance(tweet.get(k), int)}
    if note := (tweet.get("community_note") or {}).get("text"):
        post["community_note"] = note
    if poll := tweet.get("poll"):
        post["poll"] = [
            f"{c.get('label')}: {c.get('percentage')}%" for c in poll.get("choices") or []
        ]
    if found := media(tweet):
        post["media"] = found
    return post


def media(tweet: dict) -> list[dict]:
    found = []
    for m in (tweet.get("media") or {}).get("all") or []:
        item = {"type": m.get("type") or "unknown"}
        if alt := m.get("altText"):
            item["alt"] = alt
        if duration := m.get("duration"):
            item["seconds"] = round(duration)
        found.append(item)
    return found


def pictures(tweet: dict, where: str) -> list[tuple[str, str]]:
    """(label, link) for each photo, and each video's or GIF's thumbnail."""
    found = []
    for m in (tweet.get("media") or {}).get("all") or []:
        if m.get("type") == "photo" and m.get("url"):
            label = "photo" + where
            if alt := m.get("altText"):
                label += f' (alt text: "{alt}")'
            found.append((label, sized(m["url"])))
        elif m.get("thumbnail_url"):
            kind = "GIF" if m.get("type") == "gif" else "video"
            length = f"{round(m['duration'])} s " if m.get("duration") else ""
            label = f"thumbnail of a {length}{kind}{where} (one frame, not the whole {kind})"
            found.append((label, sized(m["thumbnail_url"])))
    return found


def sized(url: str) -> str:
    """X's images at 1200 px (`name=medium`) rather than the original upload."""
    parts = urlsplit(url)
    if parts.hostname not in MEDIA_HOSTS:
        return url
    query = [(k, v) for k, v in parse_qsl(parts.query) if k != "name"] + [("name", "medium")]
    return parts._replace(query=urlencode(query)).geturl()


def media_host(url: str) -> bool:
    """X's image host, plus `DISCORD_MEDIA_EXTRA_HOSTS` (the sim)."""
    parts = urlsplit(url)
    if parts.netloc in extra_media_hosts():
        return parts.scheme in ("http", "https")
    return parts.scheme == "https" and parts.hostname in MEDIA_HOSTS and parts.port is None


def remember(key: tuple[str, str], result: dict) -> dict:
    cache[key] = (time.monotonic() + CACHE_SECONDS, result)
    cache.move_to_end(key)
    while len(cache) > CACHE_SIZE:
        cache.popitem(last=False)
    return result
