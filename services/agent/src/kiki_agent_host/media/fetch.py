"""Image downloads from the chat platform's media hosts only, with expired
attachment links refreshed first. One set of image types, decided by the bytes
(`sniff`), for downloads and generated images alike."""

import logging
import time
from urllib.parse import parse_qs, urlsplit

import httpx
from pydantic_ai.messages import BinaryContent

from ..config import discord_api, extra_media_hosts

logger = logging.getLogger(__name__)


MAX_BYTES = 5 * 1024 * 1024  # the strictest common provider limit per image
DOWNLOAD_TIMEOUT_SECONDS = 10
# Discord's refresh endpoint takes at most 50 links per call.
REFRESH_BATCH = 50


def client(transport: httpx.AsyncBaseTransport | None = None) -> httpx.AsyncClient:
    """For `download`: no redirects, which could leave Discord's hosts."""
    return httpx.AsyncClient(
        timeout=DOWNLOAD_TIMEOUT_SECONDS, follow_redirects=False, transport=transport
    )


def allowed(url: str) -> bool:
    """Discord's CDN and media proxy, plus `DISCORD_MEDIA_EXTRA_HOSTS` (the sim)."""
    parts = urlsplit(url)
    extra = extra_media_hosts()
    if parts.netloc in extra:
        return parts.scheme in ("http", "https")
    host = parts.hostname or ""
    discord = host.endswith((".discordapp.com", ".discordapp.net", ".discord.com"))
    return parts.scheme == "https" and discord and parts.port is None


def expired(url: str, now: float | None = None) -> bool:
    """Whether a signed Discord attachment link has expired (or will within a
    minute). Links last about a day, so most history is past it."""
    query = parse_qs(urlsplit(url).query)
    try:
        expires = int(query["ex"][0], 16)
    except (KeyError, ValueError):
        return False
    return expires <= (now or time.time()) + 60


async def refresh(http: httpx.AsyncClient, token: str, urls: list[str]) -> dict[str, str]:
    """Fresh links for expired attachment links (Discord's `attachments/refresh-urls`),
    keyed by the old link. Links it can't refresh are left out, so their download
    reports them as expired."""
    stale = list(dict.fromkeys(u for u in urls if allowed(u) and expired(u)))
    if not stale or not token:
        return {}
    fresh: dict[str, str] = {}
    for i in range(0, len(stale), REFRESH_BATCH):
        try:
            response = await http.post(
                f"{discord_api()}/attachments/refresh-urls",
                json={"attachment_urls": stale[i : i + REFRESH_BATCH]},
                headers={"Authorization": f"Bot {token}"},
            )
            response.raise_for_status()
            for item in response.json().get("refreshed_urls") or []:
                if allowed(item.get("refreshed") or ""):
                    fresh[item["original"]] = item["refreshed"]
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as error:
            logger.warning("refreshing Discord links failed error_type=%s", type(error).__name__)
    return fresh


async def download(
    http: httpx.AsyncClient, url: str, declared: str | None = None
) -> BinaryContent | str:
    """The image, or why it couldn't be read. `declared` (the payload's content
    type) only words the error for an unsupported format."""
    if not allowed(url):
        return "not hosted by Discord"
    try:
        async with http.stream("GET", url) as response:
            if response.status_code in (403, 404, 410):
                return "no longer available (deleted, or an expired Discord link)"
            if response.status_code != 200:
                return f"download failed (HTTP {response.status_code})"
            header = response.headers.get("content-type", "").split(";")[0].strip().lower()
            data = bytearray()
            async for chunk in response.aiter_bytes():
                data += chunk
                if len(data) > MAX_BYTES:
                    return "too large"
    except httpx.HTTPError as error:
        return f"download failed ({type(error).__name__})"
    # The bytes decide: a renamed file (a JPEG saved as .png) or a mislabeled
    # response would otherwise be sent under the wrong type, and providers reject
    # an image whose bytes don't match its declared type.
    media_type = sniff(bytes(data[:12]))
    if media_type is None:
        return f"unsupported format ({header or declared or 'unknown'})"
    return BinaryContent(bytes(data), media_type=media_type)


def sniff(head: bytes) -> str | None:
    """Which image type these leading bytes start, if any: PNG, JPEG, GIF or
    WebP, what vision models take and Discord shows inline."""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return None
