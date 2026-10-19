"""Reddit posts for `read_link`: the post, its top comments, and its images.

Posts come from Reddit's JSON (`/comments/<id>.json`). Share links
(`/r/<sub>/s/<code>`) redirect to the post, so they're resolved first; `redd.it/<id>`
already names it. Reddit blocks anonymous requests from many datacenter IPs (403)
and rate-limits them (429). With `REDDIT_CLIENT_ID` and `REDDIT_CLIENT_SECRET`
set, requests use a read-only app-only OAuth token on `oauth.reddit.com` instead.

Gallery and image posts go to the vision model like `read_image`. Videos
(`v.redd.it`) come back as metadata with a note, until there's a shared video
reader (issue #43).

Results are cached per post and question for a few minutes, so asking about the
same post again doesn't refetch it or pay for vision again.
"""

import asyncio
import base64
import json
import logging
import re
import time
from datetime import UTC, datetime
from urllib.parse import urljoin, urlsplit

import httpx
from pydantic_ai.messages import BinaryContent, UserContent

from ...config import extra_media_hosts, reddit_base, reddit_credentials
from ..fetch import download
from ..reading import INSTRUCTIONS
from ..vision import ImageRefused, look

logger = logging.getLogger(__name__)

USER_AGENT = "python:kiki-agent-host:0.1 (Discord chat bot; +https://github.com/carltonwu/kiki)"
OAUTH_API = "https://oauth.reddit.com"
TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
TIMEOUT_SECONDS = 10
MAX_BYTES = 2 * 1024 * 1024  # a post with 25 comments is about 100 KB
MAX_TEXT_CHARS = 4000
MAX_COMMENTS = 8
MAX_COMMENT_CHARS = 600
MAX_IMAGES = 10  # like read_image
PREVIEW_WIDTH = 1080  # Reddit's largest preview; full-size images can pass 5 MB
CACHE_SECONDS = 600
CACHE_SIZE = 128
MAX_OUTPUT_TOKENS = 3000
# Tests swap in an `httpx.MockTransport`.
TRANSPORT: httpx.AsyncBaseTransport | None = None

SITE = re.compile(r"(?:[a-z0-9-]+\.)*reddit\.com|redd\.it")
MEDIA_HOSTS = {"i.redd.it", "preview.redd.it", "external-preview.redd.it"}
POST = re.compile(r"/comments/([a-z0-9]+)(?:/[^/]*/([a-z0-9]+))?", re.IGNORECASE)
GALLERY = re.compile(r"/gallery/([a-z0-9]+)", re.IGNORECASE)
SHARE = re.compile(r"/r/\w+/s/\w+/?", re.IGNORECASE)
SHORT = re.compile(r"/([a-z0-9]+)/?", re.IGNORECASE)  # redd.it/<post id>
GONE = ("[removed]", "[deleted]")

_cache: dict[tuple, tuple[float, dict]] = {}
_token: tuple[str, float] | None = None  # OAuth bearer and when it expires


def matches(url: str) -> bool:
    return SITE.fullmatch(urlsplit(url).hostname or "") is not None


def post_id(url: str) -> tuple[str, str | None] | None:
    """(post, comment) IDs a link names directly, or None (a share link or not a post)."""
    parts = urlsplit(url)
    if parts.hostname == "redd.it":
        m = SHORT.fullmatch(parts.path)
        return (m[1].lower(), None) if m else None
    if m := POST.search(parts.path):
        return m[1].lower(), m[2].lower() if m[2] else None
    if m := GALLERY.match(parts.path):
        return m[1].lower(), None
    return None


async def resolve(http: httpx.AsyncClient, url: str) -> tuple[str, str | None] | None:
    """A share link's post: its redirect names it. Only the redirect's address is
    read; nothing is fetched from it."""
    if found := post_id(url):
        return found
    path = urlsplit(url).path
    if not SHARE.fullmatch(path):
        return None
    try:
        response = await http.get(reddit_base() + path)
    except httpx.HTTPError as error:
        logger.warning("reddit share link failed error_type=%s", type(error).__name__)
        return None
    location = response.headers.get("location", "")
    if response.is_redirect and location and matches(urljoin("https://www.reddit.com", location)):
        return post_id(urljoin("https://www.reddit.com", location))
    logger.warning("reddit share link not resolved status=%d", response.status_code)
    return None


async def read(deps, url: str, question: str | None = None) -> dict:
    async with httpx.AsyncClient(
        timeout=TIMEOUT_SECONDS,
        follow_redirects=False,
        headers={"User-Agent": USER_AGENT},
        transport=TRANSPORT,
    ) as http:
        ids = await resolve(http, url)
        if ids is None and SHARE.fullmatch(urlsplit(url).path):
            return {"error": "Reddit didn't resolve this share link to a post right now."}
        if ids is None:
            return {"error": "Not a Reddit post link (only posts and comments can be read)."}
        key = (*ids, (question or "").strip().lower())
        if (hit := _cache.get(key)) and hit[0] > time.monotonic():
            return hit[1] | {"cached": True}

        status, data = await fetch(http, *ids)
        if status != 200:
            return {"error": failure(status, data)}
        try:
            result, images = parse(data, ids[1])
        except (KeyError, IndexError, TypeError, ValueError):
            return {"error": "Reddit returned something that isn't a post."}
        if images and "error" not in result:
            result |= await describe(http, deps.vision, result, images, question)

    if "error" not in result:
        if len(_cache) >= CACHE_SIZE:
            _cache.pop(next(iter(_cache)))
        _cache[key] = (time.monotonic() + CACHE_SECONDS, result)
    return result


async def photos(url: str) -> list[str]:
    """The post's images as `read` describes them; empty when it can't be read."""
    async with httpx.AsyncClient(
        timeout=TIMEOUT_SECONDS,
        follow_redirects=False,
        headers={"User-Agent": USER_AGENT},
        transport=TRANSPORT,
    ) as http:
        try:
            ids = await resolve(http, url)
            if ids is None:
                return []
            status, data = await fetch(http, *ids)
        except httpx.HTTPError as error:
            logger.warning("reddit photos failed error_type=%s", type(error).__name__)
            return []
    if status != 200:
        return []
    try:
        _, images = parse(data, ids[1])
    except (KeyError, IndexError, TypeError, ValueError):
        return []
    return [link for link, _ in images]


async def fetch(http: httpx.AsyncClient, post: str, comment: str | None) -> tuple[int, object]:
    """The post's JSON (status 200) or the failing status with Reddit's error body.
    Anonymous unless credentials are set."""
    params = {"raw_json": "1", "sort": "top", "limit": str(MAX_COMMENTS * 3), "depth": "1"}
    if comment:
        params["comment"] = comment
    for attempt in range(2):
        if credentials := reddit_credentials():
            token = await oauth_token(http, credentials)
            if token is None:
                return 401, None
            url = f"{OAUTH_API}/comments/{post}"
            headers = {"Authorization": f"Bearer {token}"}
        else:
            url, headers = f"{reddit_base()}/comments/{post}.json", {}
        status, data = await get_json(http, url, params, headers)
        if status == 401 and credentials and attempt == 0:
            global _token
            _token = None  # expired early; one more try with a new token
            continue
        if status in (403, 429) and not credentials:
            logger.warning(
                "reddit refused an anonymous request status=%d; "
                "set REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET to use OAuth",
                status,
            )
        return status, data
    return 401, None


async def get_json(
    http: httpx.AsyncClient, url: str, params: dict, headers: dict
) -> tuple[int, object]:
    try:
        async with http.stream("GET", url, params=params, headers=headers) as response:
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body += chunk
                if len(body) > MAX_BYTES:
                    return 413, None
            status = response.status_code
    except httpx.HTTPError as error:
        logger.warning("reddit request failed error_type=%s", type(error).__name__)
        return 0, None
    try:
        return status, json.loads(body)
    except ValueError:
        return (502 if status == 200 else status), None


async def oauth_token(http: httpx.AsyncClient, credentials: tuple[str, str]) -> str | None:
    """An app-only bearer token (client credentials grant), reused until it expires."""
    global _token
    if _token and _token[1] > time.monotonic() + 60:
        return _token[0]
    basic = base64.b64encode(":".join(credentials).encode()).decode()
    try:
        response = await http.post(
            TOKEN_URL,
            data={"grant_type": "client_credentials"},
            headers={"Authorization": f"Basic {basic}"},
        )
        response.raise_for_status()
        grant = response.json()
        _token = (grant["access_token"], time.monotonic() + float(grant.get("expires_in", 3600)))
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as error:
        logger.warning("reddit oauth token failed error_type=%s", type(error).__name__)
        return None
    return _token[0]


def failure(status: int, data: object) -> str:
    """What to tell Kiki about a post that couldn't be read."""
    reason = data.get("reason") if isinstance(data, dict) else None
    if reason == "private":
        return "This post is in a private subreddit, so it can't be read."
    if reason in ("quarantined", "gold_only"):
        return f"This post is in a {reason.replace('_', '-')} subreddit, so it can't be read."
    if reason == "banned":
        return "This subreddit has been banned, so the post can't be read."
    if status == 404:
        return "Reddit says this post doesn't exist (deleted, or a wrong link)."
    if status in (401, 403):
        return "Reddit blocked the request, so Reddit posts can't be read right now."
    if status == 429:
        return "Reddit is rate limiting requests; try again later."
    if status == 413:
        return "The post is too large to read."
    return "Reddit is unavailable right now; try again later."


def parse(data: list, comment: str | None = None) -> tuple[dict, list[tuple[str, str]]]:
    """A post listing as Kiki's result, and its images to describe (URL, caption).
    Removed posts get an error but keep what's left (title, comments)."""
    post = data[0]["data"]["children"][0]["data"]
    # A crosspost's media lives on the original post.
    media = (post.get("crosspost_parent_list") or [post])[0]
    text = post.get("selftext") or ""
    result: dict = {
        "site": "reddit",
        "post": {
            "title": post["title"],
            "subreddit": post.get("subreddit_name_prefixed") or f"r/{post.get('subreddit')}",
            "author": f"u/{post.get('author', '[deleted]')}",
            "score": post.get("score"),
            "upvote_ratio": post.get("upvote_ratio"),
            "comments": post.get("num_comments"),
            "posted": date(post.get("created_utc")),
            "url": "https://www.reddit.com" + post.get("permalink", ""),
        },
    }
    about = result["post"]
    if post.get("link_flair_text"):
        about["flair"] = post["link_flair_text"]
    for flag, name in (("over_18", "nsfw"), ("spoiler", "spoiler"), ("locked", "locked")):
        if post.get(flag):
            about[name] = True
    if media is not post:
        about["crosspost_of"] = media.get("subreddit_name_prefixed")
    if text and text not in GONE:
        about["text"] = trim(text, MAX_TEXT_CHARS)
    link = media.get("url_overridden_by_dest") or media.get("url") or ""
    if not media.get("is_self") and link and not is_reddit(link):
        about["link"] = link

    if removed := post.get("removed_by_category") or (text in GONE and text.strip("[]")):
        how = "deleted by its author" if removed in ("author", "deleted") else "removed"
        result["error"] = f"This post was {how} ({removed}); only what's left of it is shown."

    comments = [c["data"] for c in data[1]["data"]["children"] if c.get("kind") == "t1"]
    # Pinned moderator notes and removed comments say nothing about the post.
    picked = [
        c
        for c in comments
        if c.get("body") not in GONE and not (c.get("stickied") and c.get("distinguished"))
    ]
    # A link to one comment: Reddit returns that comment's thread.
    if linked := next((c for c in picked if comment and c.get("id") == comment), None):
        picked.remove(linked)
        result["linked_comment"] = shown(linked)
    picked.sort(key=lambda c: c.get("score") or 0, reverse=True)
    result["top_comments"] = [shown(c) for c in picked[:MAX_COMMENTS]]

    if video := (media.get("secure_media") or media.get("media") or {}).get("reddit_video"):
        result["video"] = {
            "duration_s": video.get("duration"),
            "size": f"{video.get('width')}x{video.get('height')}",
            "note": "Watching Reddit videos isn't supported yet: describe it from the title, "
            "text and comments, and say you couldn't watch it.",
        }
        return result, []
    return result, images(media)


def images(post: dict) -> list[tuple[str, str]]:
    """A gallery's images in order, or an image post's image, each at preview size."""
    if post.get("is_gallery"):
        meta = post.get("media_metadata") or {}
        found = []
        for item in (post.get("gallery_data") or {}).get("items") or []:
            m = meta.get(item.get("media_id")) or {}
            if m.get("status") != "valid":
                continue
            full = (m.get("s") or {}).get("u")
            if url := sized(m.get("p") or [], lambda r: (r["x"], r["u"])) or full:
                found.append((url, item.get("caption") or ""))
        return found[:MAX_IMAGES]
    if post.get("post_hint") == "image":
        preview = ((post.get("preview") or {}).get("images") or [{}])[0]
        url = sized(preview.get("resolutions") or [], lambda r: (r["width"], r["url"]))
        url = url or (preview.get("source") or {}).get("url") or post.get("url")
        return [(url, "")] if url else []
    return []


def sized(resolutions: list[dict], read) -> str | None:
    """The widest preview up to `PREVIEW_WIDTH`."""
    fits = [read(r) for r in resolutions if read(r)[0] <= PREVIEW_WIDTH]
    return max(fits)[1] if fits else None


async def describe(
    http: httpx.AsyncClient, vision, result: dict, images: list[tuple[str, str]], question
) -> dict:
    """The post's images through the vision model, as one gallery."""
    downloads = await asyncio.gather(
        *(
            download(http, url, allow=media_host) if media_host(url) else offsite()
            for url, _ in images
        )
    )
    listed = [
        {"image": n}
        | ({"caption": c} if c else {})
        | ({"error": got} if isinstance(got, str) else {})
        for n, ((_, c), got) in enumerate(zip(images, downloads, strict=True), 1)
    ]
    readable = [
        (n, caption, got)
        for n, ((_, caption), got) in enumerate(zip(images, downloads, strict=True), 1)
        if isinstance(got, BinaryContent)
    ]
    if not readable:
        return {"images": listed, "images_error": "none of the images could be downloaded"}
    about = result["post"]
    prompt: list[UserContent] = [
        f"From a Reddit post in {about['subreddit']} by {about['author']}, "
        f'{len(images)} image(s). Title: "{about["title"]}"'
        + (f'\nPost text: "{trim(about["text"], 1000)}"' if about.get("text") else "")
        + (f"\nKiki is wondering: {question.strip()}" if question and question.strip() else "")
    ]
    for n, caption, content in readable:
        prompt += [f"Image {n}" + (f' (caption: "{caption}")' if caption else ""), content]
    try:
        description = await look(
            vision,
            prompt,
            instructions=INSTRUCTIONS,
            max_tokens=min(MAX_OUTPUT_TOKENS, 600 + 400 * len(readable)),
        )
    except ImageRefused as error:
        return {"images": listed, "images_error": str(error)}
    return {"images": listed, "description": description}


def media_host(url: str) -> bool:
    """Reddit's image hosts, plus `DISCORD_MEDIA_EXTRA_HOSTS` (the sim)."""
    parts = urlsplit(url)
    if parts.netloc in extra_media_hosts():
        return parts.scheme in ("http", "https")
    return parts.scheme == "https" and parts.hostname in MEDIA_HOSTS and parts.port is None


async def offsite() -> str:
    return "not hosted by Reddit"


def shown(comment: dict) -> dict:
    return {
        "author": f"u/{comment.get('author', '[deleted]')}",
        "score": comment.get("score"),
        "text": trim(comment.get("body") or "", MAX_COMMENT_CHARS),
    }


def is_reddit(url: str) -> bool:
    host = urlsplit(url).hostname or ""
    return matches(url) or host in MEDIA_HOSTS or host == "v.redd.it"


def date(created: float | None) -> str | None:
    return datetime.fromtimestamp(created, UTC).strftime("%Y-%m-%d") if created else None


def trim(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"
