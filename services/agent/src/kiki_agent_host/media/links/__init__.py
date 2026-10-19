"""`read_link`: what a shared link actually contains, from a reader per site.

Web fetch reads ordinary pages, but social sites hide posts behind scripts, logins
or blocks. A reader uses its site's own API instead and returns structured text
(author, body, stats, top comments), with images described by the vision model
like `read_image`. A reader is a module with `matches(url) -> bool` and
`async read(deps, url, question) -> dict`, plus `async photos(url)` (its images'
links, for drawings) and `media_host(url)`; a new site is one entry in `READERS`.

Readers request only their own site's hosts, so a URL from the model can't make
the host fetch an arbitrary address. Links no reader takes come back as an error
that sends the model to web fetch.
"""

import re
from collections.abc import Callable
from urllib.parse import urlsplit

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from ...chat.scope import HEARD, Scope
from . import reddit, x

READERS = [reddit, x]

# Ends at whitespace and at the `<>` of Discord's no-preview links.
LINK = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)


def normalize(url: str) -> str | None:
    """An http(s) link with a host, or None. Bare `site.com/...` gets https."""
    url = url.strip().strip("<>").rstrip(".,!?;:'\")]}")
    if not re.match(r"https?://", url, re.IGNORECASE):
        url = "https://" + url
    parts = urlsplit(url)
    return url if parts.hostname and " " not in url else None


def reader_for(url: str):
    return next((reader for reader in READERS if reader.matches(url)), None)


async def photos(url: str) -> tuple[list[str], Callable[[str], bool]]:
    """A post's images as its reader finds them (all of a tweet's photos, where
    Discord's preview shows one or none), in `read_link`'s order, and the hosts
    they may be downloaded from. Empty when no reader takes the link or it fails."""
    link = normalize(url)
    reader = reader_for(link) if link else None
    if reader is None:
        return [], nowhere
    return await reader.photos(link), reader.media_host


def nowhere(url: str) -> bool:
    return False


def post_links(content: str, raw: dict) -> list[str]:
    """The links in a message that a reader takes: the text first, then previews."""
    embeds = (raw or {}).get("embeds") or []
    found = [normalize(u) for u in LINK.findall(content or "")]
    found += [normalize(e["url"]) for e in embeds if isinstance(e.get("url"), str)]
    return list(dict.fromkeys(u for u in found if u and reader_for(u)))


async def read_link(deps, url: str, question: str | None = None) -> dict:
    """Reads one link with its site's reader. `deps` is the run's trusted context;
    readers use its vision model."""
    link = normalize(url)
    if link is None:
        raise ValueError("url must be a link (https://...) or a message ID")
    reader = reader_for(link)
    if reader is None:
        return {
            "url": link,
            "error": "No reader for this site. Use web fetch to read the page instead.",
        }
    return {"url": link} | await reader.read(deps, link, question)


MESSAGE = f"""
SELECT m.content, m.raw FROM messages m
WHERE m.id = %(id)s AND m.guild_id = %(guild)s AND m.deleted_at IS NULL AND {HEARD}
"""


async def link_in_message(connection: AsyncConnection, scope: Scope, message_id: int) -> str:
    """The first link in a stored message that a reader takes, else its first link:
    the text first, then link previews."""
    cursor = connection.cursor(row_factory=dict_row)
    await cursor.execute(MESSAGE, {"id": message_id, "guild": scope.guild_id})
    m = await cursor.fetchone()
    if m is None:
        raise ValueError("no such message in this server")
    embeds = (m["raw"] or {}).get("embeds") or []
    found = [normalize(u) for u in LINK.findall(m["content"] or "")]
    found += [normalize(e["url"]) for e in embeds if isinstance(e.get("url"), str)]
    found = [u for u in found if u]
    if not found:
        raise ValueError("that message has no links")
    return next((u for u in found if reader_for(u)), found[0])
