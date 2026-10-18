"""The common image backing: reading (`reading.py`, `read_image`), drawing
(`images.py`, `generate_image`) and custom emoji descriptions (`emojis.py`).

- One set of image types, decided by the bytes (`sniff`), for downloads and for
  generated images alike.
- Downloads only from Discord's media hosts.
- One vision model call that maps provider failures to `ImageRefused`, the one
  image error whose message is safe to show the chat model, and adds billed cost.
"""

import logging
import os
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx
from pydantic_ai import Agent
from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError, UnexpectedModelBehavior
from pydantic_ai.messages import BinaryContent, ModelResponse, UserContent
from pydantic_ai.models import Model
from pydantic_ai.usage import UsageLimits

logger = logging.getLogger(__name__)

MAX_BYTES = 5 * 1024 * 1024  # the strictest common provider limit per image
DOWNLOAD_TIMEOUT_SECONDS = 10

viewer = Agent()


@dataclass
class Vision:
    """The model that reads images; `fallback` means it's the chat model."""

    model: Model
    name: str
    fallback: bool = False
    cost: float = 0.0
    calls: int = 0


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
    extra = {h.strip() for h in os.getenv("DISCORD_MEDIA_EXTRA_HOSTS", "").split(",")} - {""}
    if parts.netloc in extra:
        return parts.scheme in ("http", "https")
    host = parts.hostname or ""
    discord = host.endswith((".discordapp.com", ".discordapp.net", ".discord.com"))
    return parts.scheme == "https" and discord and parts.port is None


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
) -> str:
    """One vision model request; its billed cost is added to `vision.cost`."""
    vision.calls += 1
    try:
        run = await viewer.run(
            prompt,
            model=vision.model,
            instructions=instructions,
            model_settings={"max_tokens": max_tokens, "openrouter_usage": {"include": True}},
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
