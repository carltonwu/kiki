"""The images a stored message carries: its image attachments, then its image
embeds, as links sized for a vision model."""

import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit

from ..vision import MAX_BYTES

MAX_IMAGES = 10  # Discord's attachment limit per message
MAX_SIDE = 1568  # larger images are downscaled by Discord's media proxy


# Same test as the prompt's image markers (Rust `context::MESSAGE_COLUMNS`).
IMAGE_NAME = re.compile(r"\.(png|jpe?g|gif|webp)$", re.IGNORECASE)


@dataclass(frozen=True)
class Image:
    name: str
    url: str
    media_type: str | None = None
    alt: str | None = None
    # The attachment's ID (embeds have none), to record which image was used.
    id: str | None = None


def images_in(raw: dict) -> list[Image]:
    """Image attachments in order, then image embeds; at most `MAX_IMAGES`."""
    found: list[Image] = []
    for a in raw.get("attachments") or []:
        content_type = (a.get("content_type") or "").split(";")[0].strip().lower()
        name = a.get("filename") or "image"
        if not (content_type.startswith("image/") or IMAGE_NAME.search(name)):
            continue
        if url := sized(a):
            id = str(a["id"]) if a.get("id") else None
            found.append(Image(name, url, content_type or None, a.get("description"), id))
    for e in raw.get("embeds") or []:
        media = e.get("thumbnail") if e.get("type") in ("image", "gifv") else None
        media = media or e.get("image")
        # An embed's own URL is the linked site's; its proxy URL is Discord's.
        if media and (url := sized(media, proxied=True)):
            found.append(Image("linked image", url, media.get("content_type")))
    unique = {image.url: image for image in reversed(found)}  # first wins
    return [i for i in found if unique[i.url] is i][:MAX_IMAGES]


def sized(media: dict, proxied: bool = False) -> str | None:
    """The original, or Discord's proxy resized to fit `MAX_SIDE` and `MAX_BYTES`."""
    url, proxy = media.get("url"), media.get("proxy_url")
    width, height = media.get("width") or 0, media.get("height") or 0
    big = max(width, height) > MAX_SIDE or (media.get("size") or 0) > MAX_BYTES
    if not (big and proxy):
        return (proxy or url) if proxied else (url or proxy)
    scale = MAX_SIDE / max(width, height, MAX_SIDE)
    size = {"width": str(round(width * scale)), "height": str(round(height * scale))}
    if not (width and height):
        size = {"width": str(MAX_SIDE)}
    parts = urlsplit(proxy)
    query = [(k, v) for k, v in parse_qsl(parts.query) if k not in ("width", "height")]
    return parts._replace(query=urlencode(query + list(size.items()))).geturl()
