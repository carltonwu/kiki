"""The common image backing: reading (`reading.py`, `read_image`), drawing
(`images.py`, `generate_image`) and custom emoji descriptions (`emojis.py`).

- One set of image types, decided by the bytes (`sniff`), for downloads and for
  generated images alike.
- Downloads only from Discord's media hosts, refreshing expired attachment links.
- One vision model call that maps provider failures to `ImageRefused`, the one
  image error whose message is safe to show the chat model, and adds billed cost.
"""

import logging
import time
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

import httpx
from pydantic_ai import Agent
from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError, UnexpectedModelBehavior
from pydantic_ai.messages import BinaryContent, ModelResponse, UserContent
from pydantic_ai.models import Model
from pydantic_ai.usage import UsageLimits

from .config import discord_api, extra_media_hosts

logger = logging.getLogger(__name__)

MAX_BYTES = 5 * 1024 * 1024  # the strictest common provider limit per image
DOWNLOAD_TIMEOUT_SECONDS = 10
# Discord's refresh endpoint takes at most 50 links per call.
REFRESH_BATCH = 50

viewer = Agent()


@dataclass
class Vision:
    """The model that reads images; `fallback` means it's the chat model."""

    model: Model
    name: str
    fallback: bool = False
    cost: float = 0.0
    calls: int = 0
    # The bot token, to refresh expired attachment links (`refresh`); none skips it.
    discord_token: str = ""


class ImageRefused(ValueError):
    """No usable image (read, described or drawn); the message is safe to show the
    chat model. A ValueError, so tools report it rather than failing the reply."""


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


async def look(
    vision: Vision,
    prompt: list[UserContent],
    *,
    max_tokens: int,
    instructions: str | None = None,
    think: bool = True,
) -> str:
    """One vision model request; its billed cost is added to `vision.cost`.
    `think=False` turns off a reasoning model's thinking, for quick yes/no checks:
    over several photos it can take longer than the answer is worth."""
    vision.calls += 1
    settings: dict = {"max_tokens": max_tokens, "openrouter_usage": {"include": True}}
    if not think:
        settings["openrouter_reasoning"] = {"enabled": False}
        settings["temperature"] = 0  # the same photos get the same verdicts
    try:
        run = await viewer.run(
            prompt,
            model=vision.model,
            instructions=instructions,
            model_settings=settings,
            usage_limits=UsageLimits(request_limit=1),
        )
    except ModelHTTPError as error:
        logger.warning(
            "vision call failed model=%s fallback=%s status=%d",
            vision.name,
            vision.fallback,
            error.status_code,
        )
        if error.status_code == 429 or error.status_code >= 500:
            raise ImageRefused(
                "The image model is unavailable right now; try again later."
            ) from None
        if vision.fallback:
            raise ImageRefused(
                "No vision model is configured (OPENROUTER_VISION_MODEL), and the "
                f"chat model {vision.name} can't read images. Tell them you can't see "
                "images right now."
            ) from None
        raise ImageRefused(f"The image model {vision.name} rejected these images.") from None
    except (ModelAPIError, UnexpectedModelBehavior) as error:
        logger.warning(
            "vision call failed model=%s error_type=%s", vision.name, type(error).__name__
        )
        raise ImageRefused("The image model is unavailable right now; try again later.") from None
    vision.cost += billed_cost(run) or 0.0
    if not run.output.strip():
        raise ImageRefused("The image model returned no description.")
    return run.output.strip()


def billed_cost(result) -> float | None:
    """OpenRouter's billed cost per response; Pydantic's price table lags new models."""
    billed = [
        m.provider_details["cost"]
        for m in result.new_messages()
        if isinstance(m, ModelResponse) and "cost" in (m.provider_details or {})
    ]
    if billed:
        return sum(billed)
    return float(result.usage.cost) if result.usage.cost is not None else None
