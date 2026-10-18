"""Message search over Postgres, independent of the agent framework.

Hybrid: full text plus pgvector, fused by reciprocal rank with a small recency
vote. Everything is scoped to one guild: Kiki knows every channel she can read.
Embeddings are computed lazily for the searched guild, or all at once by
`kiki-agent-host index`.
"""

import asyncio
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from openai import AsyncOpenAI
from psycopg import AsyncConnection
from psycopg.rows import dict_row

logger = logging.getLogger(__name__)

EMBEDDING_MODEL = "qwen/qwen3-embedding-8b"
DIMENSIONS = 1024  # Qwen3 embeddings are Matryoshka: a prefix is a valid embedding.
QUERY_INSTRUCTION = (
    "Instruct: Given a chat search query, retrieve relevant Discord messages\nQuery: "
)
EMBED_BATCH = 64
INDEX_LIMIT = 256
CANDIDATES = 50
RRF_K = 60
# Nearest neighbors exist even for nonsense; below this they're noise. A starting
# point: calibrate from the top_similarity values that searches log at DEBUG.
MIN_SIMILARITY = 0.25
# Recency adds a fraction of a top-ranked vote, halving every HALF_LIFE_DAYS.
RECENCY_WEIGHT = 0.15
HALF_LIFE_DAYS = 30
SNIPPET_CHARS = 1000

Semantic = Literal["full", "partial", "off"]


class Embedder:
    """OpenRouter embeddings as halfvec literals; accumulates reported cost."""

    def __init__(self, client: AsyncOpenAI):
        self.client = client
        self.cost = 0.0

    async def __call__(
        self, texts: list[str], *, query: bool = False, instruction: str = QUERY_INSTRUCTION
    ) -> list[str]:
        if query:
            texts = [instruction + t for t in texts]
        response = await self.client.embeddings.create(model=EMBEDDING_MODEL, input=texts)
        self.cost += float(getattr(response.usage, "cost", None) or 0)
        return [
            "[" + ",".join(f"{x:.5g}" for x in item.embedding[:DIMENSIONS]) + "]"
            for item in sorted(response.data, key=lambda item: item.index)
        ]


def openrouter_base() -> str:
    """OpenRouter's API root; `OPENROUTER_BASE_URL` points at the local simulator."""
    return (os.getenv("OPENROUTER_BASE_URL") or "https://openrouter.ai/api").rstrip("/")


def openrouter_client(api_key: str, **options) -> AsyncOpenAI:
    return AsyncOpenAI(base_url=f"{openrouter_base()}/v1", api_key=api_key, **options)


@dataclass(frozen=True)
class Scope:
    guild_id: int
    channel_id: int
    conversation_id: int


@dataclass(frozen=True)
class Roster:
    """The prompt's people: ref (`u2`) to user ID, and user ID to `u2 Alex`."""

    ids: dict[str, int] = field(default_factory=dict)
    speakers: dict[int, str] = field(default_factory=dict)

    @classmethod
    def of(cls, people) -> "Roster":
        return cls(
            {p.ref: int(p.user_id) for p in people},
            {int(p.user_id): f"{p.ref} {p.label}" for p in people},
        )


NO_ROSTER = Roster()
REF = re.compile(r"^u[0-9]+$")
USER_ID = re.compile(r"^<@!?([0-9]+)>$|^([0-9]{15,20})$")
MENTION = re.compile(r"<@!?([0-9]+)>")
CHANNEL_ID = re.compile(r"^<#([0-9]+)>$|^([0-9]{15,20})$")
MAX_ID = 2**63 - 1  # Postgres bigint


def discord_id(text: str, match: re.Match) -> int:
    """The ID a USER_ID/CHANNEL_ID match holds; bigger ones would fail a bigint cast."""
    if (value := int(match[1] or match[2])) > MAX_ID:
        raise ValueError(f"{text!r} is not a Discord ID")
    return value


RESOLVE = """
SELECT n.user_id FROM user_names n
WHERE n.guild_id = %(guild)s AND n.name_norm = name_norm(%(name)s) AND n.name_norm <> ''
  AND (NOT %(username_only)s OR n.kind = 'username')
  AND EXISTS (SELECT 1 FROM messages m WHERE m.author_id = n.user_id
              AND m.guild_id = %(guild)s AND m.deleted_at IS NULL AND NOT m.author_bot)
GROUP BY n.user_id ORDER BY n.user_id
"""


async def resolve_person(
    connection: AsyncConnection, scope: Scope, roster: Roster, person: str
) -> list[int]:
    """User IDs `person` can mean: a ref, a mention or ID, an @username, or a name."""
    person = person.strip()
    if REF.match(person):
        if person not in roster.ids:
            raise ValueError(f"{person} is not in this conversation's people list")
        return [roster.ids[person]]
    if match := USER_ID.match(person):
        return [discord_id(person, match)]
    username_only = person.startswith("@")
    cursor = await connection.execute(
        RESOLVE,
        {
            "guild": scope.guild_id,
            "name": person.removeprefix("@"),
            "username_only": username_only,
        },
    )
    return [row[0] for row in await cursor.fetchall()]


async def resolve_channel(connection: AsyncConnection, scope: Scope, channel: str) -> list[int]:
    """Channel IDs `channel` can mean: a <#mention>, an ID, or a name (with or without #)."""
    channel = channel.strip()
    if match := CHANNEL_ID.match(channel):
        ids, names = [discord_id(channel, match)], None
    else:
        ids, names = None, channel.removeprefix("#").strip().lower()
    cursor = await connection.execute(
        "SELECT id FROM channels WHERE guild_id = %s AND (id = ANY(%s) OR lower(name) = %s)"
        " ORDER BY id",
        (scope.guild_id, ids or [], names),
    )
    return [row[0] for row in await cursor.fetchall()]


async def channel_names(
    connection: AsyncConnection, scope: Scope, channel_ids: set[int]
) -> dict[int, str]:
    cursor = await connection.execute(
        "SELECT id, name FROM channels WHERE guild_id = %s AND id = ANY(%s) AND name IS NOT NULL",
        (scope.guild_id, list(channel_ids)),
    )
    return {channel_id: name for channel_id, name in await cursor.fetchall()}


async def speakers(
    connection: AsyncConnection, scope: Scope, roster: Roster, user_ids: set[int]
) -> dict[int, str]:
    """`u2 Alex` for people in the roster, `Alex (@alex_99)` for anyone else."""
    found = {i: roster.speakers[i] for i in user_ids if i in roster.speakers}
    missing = [i for i in user_ids if i not in found]
    if missing:
        cursor = await connection.execute(
            "SELECT user_id, coalesce(nick, global_name, username), username FROM people"
            " WHERE guild_id = %s AND user_id = ANY(%s)",
            (scope.guild_id, missing),
        )
        for user_id, name, username in await cursor.fetchall():
            found[user_id] = name if not username or name == username else f"{name} (@{username})"
    return found


# Authors Kiki never reads (migration 0010), for messages aliased `m`. Not an
# anti-join: the one-row table is never auto-analyzed, and the planner's guess
# of 1,200 rows turned the guild-wide pending scan into an 84 s nested loop.
HEARD = "m.author_id <> ALL (ARRAY(SELECT user_id FROM ignored_authors))"

PENDING = f"""
SELECT m.id, m.content, md5(m.content) AS hash FROM messages m
WHERE (%(guild)s::bigint IS NULL OR m.guild_id = %(guild)s)
  AND (%(below)s::bigint IS NULL OR m.id < %(below)s)
  AND m.deleted_at IS NULL AND NOT m.author_bot AND {HEARD} AND btrim(m.content) <> ''
  AND NOT EXISTS (SELECT 1 FROM message_embeddings e WHERE e.message_id = m.id)
ORDER BY m.id DESC LIMIT %(limit)s
"""

# Writes only if the text is unchanged; FOR SHARE waits out a concurrent edit,
# whose trigger would otherwise miss this row.
SAVE = """
INSERT INTO message_embeddings (message_id, embedding)
SELECT m.id, v.embedding
FROM unnest(%s::bigint[], %s::text[], %s::halfvec[]) AS v(id, hash, embedding)
JOIN messages m ON m.id = v.id AND md5(m.content) = v.hash AND m.deleted_at IS NULL
FOR SHARE OF m
ON CONFLICT (message_id) DO NOTHING
"""


async def index_pending(
    connection: AsyncConnection,
    embed: Embedder,
    *,
    guild_id: int | None = None,
    below: int | None = None,
    limit: int = INDEX_LIMIT,
) -> tuple[int, int | None]:
    """Embeds up to `limit` pending messages, newest first.

    Returns how many were found and, if that filled the limit, the lowest ID
    seen: more may be pending below it.
    """
    cursor = await connection.execute(PENDING, {"guild": guild_id, "below": below, "limit": limit})
    rows = await cursor.fetchall()
    batches = [rows[i : i + EMBED_BATCH] for i in range(0, len(rows), EMBED_BATCH)]
    vectors = await asyncio.gather(*(embed([r[1] for r in batch]) for batch in batches))
    for batch, embeddings in zip(batches, vectors, strict=True):
        await connection.execute(SAVE, ([r[0] for r in batch], [r[2] for r in batch], embeddings))
    return len(rows), (rows[-1][0] if len(rows) == limit else None)


SCOPE = f"""
m.guild_id = %(guild)s AND m.deleted_at IS NULL AND NOT m.author_bot AND {HEARD}
AND (%(channels)s::bigint[] IS NULL OR m.channel_id = ANY(%(channels)s))
AND (%(authors)s::bigint[] IS NULL OR m.author_id = ANY(%(authors)s))
AND (%(after)s::timestamptz IS NULL OR m.created_at >= %(after)s)
AND (%(before)s::timestamptz IS NULL OR m.created_at < %(before)s)
AND NOT EXISTS (SELECT 1 FROM conversation_messages cm
                WHERE cm.message_id = m.id AND cm.conversation_id = %(conversation)s)
"""

COLUMNS = (
    "m.id, m.channel_id, m.author_name, m.author_id, m.content, m.created_at,"
    " m.raw->'attachments' AS attachments, m.raw->'embeds' AS embeds"
)

SEARCH = f"""
WITH lexical AS (
    SELECT m.id, row_number() OVER (
        ORDER BY ts_rank_cd(to_tsvector('english', m.content), q.query) DESC, m.id DESC) AS r
    FROM messages m,
         (SELECT replace(plainto_tsquery('english', %(query)s)::text, ' & ', ' | ')::tsquery
                 AS query) q
    WHERE {SCOPE} AND to_tsvector('english', m.content) @@ q.query
    ORDER BY r LIMIT %(candidates)s
), semantic AS (
    SELECT m.id, 1 - (e.embedding <=> %(vector)s::halfvec) AS similarity,
           row_number() OVER (ORDER BY e.embedding <=> %(vector)s::halfvec) AS r
    FROM messages m JOIN message_embeddings e ON e.message_id = m.id
    WHERE %(vector)s::halfvec IS NOT NULL AND {SCOPE}
      AND e.embedding <=> %(vector)s::halfvec <= 1 - %(min_similarity)s
    ORDER BY r LIMIT %(candidates)s
), fused AS (
    SELECT coalesce(l.id, s.id) AS id, s.similarity,
           coalesce(1.0 / (%(k)s + l.r), 0) + coalesce(1.0 / (%(k)s + s.r), 0) AS relevance
    FROM lexical l FULL JOIN semantic s ON s.id = l.id
)
SELECT {COLUMNS}, f.similarity
FROM fused f JOIN messages m ON m.id = f.id
ORDER BY f.relevance + %(recency)s / (%(k)s + 1)
         * power(2, -extract(epoch FROM now() - m.created_at) / 86400 / %(half_life)s) DESC
LIMIT %(limit)s
"""


async def search(
    connection: AsyncConnection,
    embed: Embedder,
    scope: Scope,
    query: str,
    *,
    person: str | None = None,
    channel: str | None = None,
    roster: Roster = NO_ROSTER,
    before: datetime | None = None,
    after: datetime | None = None,
    limit: int = 10,
    semantic: Semantic = "full",
) -> dict:
    """`semantic` is the coverage of the guild's embeddings going in."""
    if not query.strip():
        raise ValueError("query must not be empty")
    authors = None
    if person and person.strip():
        authors = await resolve_person(connection, scope, roster, person)
        if not authors:
            return {"error": f"No one named {person!r} has written in this server."}
    channels = None
    if channel and channel.strip():
        channels = await resolve_channel(connection, scope, channel)
        if not channels:
            return {"error": f"There's no channel named {channel!r} in this server."}
    try:
        [vector] = await embed([query], query=True)
    except Exception as error:  # noqa: BLE001 - lexical fallback without logging provider payloads
        logger.warning(
            "query embedding failed channel_id=%s conversation_id=%s error_type=%s; "
            "searching lexically",
            scope.channel_id,
            scope.conversation_id,
            type(error).__name__,
        )
        vector, semantic = None, "off"
    params = {
        "guild": scope.guild_id,
        "channels": channels,
        "conversation": scope.conversation_id,
        "authors": authors,
        "before": before,
        "after": after,
        "query": query,
        "vector": vector,
        "candidates": CANDIDATES,
        "k": RRF_K,
        "min_similarity": MIN_SIMILARITY,
        "half_life": HALF_LIFE_DAYS,
        # An explicit date range is a historical question: don't favor recent.
        "recency": 0 if before or after else RECENCY_WEIGHT,
        "limit": max(1, min(limit, 10)),
    }
    cursor = connection.cursor(row_factory=dict_row)
    await cursor.execute(SEARCH, params)
    rows = await cursor.fetchall()
    similarities = [m["similarity"] for m in rows if m["similarity"] is not None]
    logger.debug(
        "search completed channel_id=%s conversation_id=%s hits=%d top_similarity=%s semantic=%s",
        scope.channel_id,
        scope.conversation_id,
        len(rows),
        max(similarities, default=None),
        semantic,
    )
    result = {"semantic": semantic, "messages": await cite_all(connection, scope, roster, rows)}
    if authors and len(authors) > 1:
        names = await speakers(connection, scope, roster, set(authors))
        result["note"] = (
            f"{person!r} matches {len(authors)} different people: "
            + "; ".join(names.get(a, str(a)) for a in authors)
            + ". Results are from all of them; check who wrote each."
        )
    return result


# The target's own channel, wherever in the guild it is.
CONTEXT = f"""
WITH t AS (SELECT id, channel_id FROM messages
           WHERE id = %(id)s AND guild_id = %(guild)s AND deleted_at IS NULL)
SELECT * FROM (
    (SELECT {COLUMNS} FROM messages m JOIN t ON m.channel_id = t.channel_id
     WHERE m.deleted_at IS NULL AND {HEARD} AND m.id < t.id ORDER BY m.id DESC LIMIT %(before)s)
    UNION
    (SELECT {COLUMNS} FROM messages m JOIN t ON m.channel_id = t.channel_id
     WHERE m.deleted_at IS NULL AND {HEARD} AND m.id >= t.id ORDER BY m.id LIMIT %(after)s + 1)
    UNION
    SELECT {COLUMNS.replace("m.", "p.")} FROM messages m JOIN messages p ON p.id = m.reply_to_id
    WHERE m.id = %(id)s AND p.guild_id = %(guild)s AND p.deleted_at IS NULL
      AND {HEARD.replace("m.", "p.")}
) around
WHERE EXISTS (SELECT 1 FROM t)
ORDER BY id
"""


async def message_context(
    connection: AsyncConnection,
    scope: Scope,
    message_id: int,
    before: int = 5,
    after: int = 5,
    roster: Roster = NO_ROSTER,
) -> dict:
    cursor = connection.cursor(row_factory=dict_row)
    await cursor.execute(
        CONTEXT,
        {
            "guild": scope.guild_id,
            "id": message_id,
            "before": max(0, min(before, 10)),
            "after": max(0, min(after, 10)),
        },
    )
    rows = await cursor.fetchall()
    if not rows:
        return {"error": "no such message in this server"}
    return {"messages": await cite_all(connection, scope, roster, rows)}


async def cite_all(
    connection: AsyncConnection, scope: Scope, roster: Roster, rows: list[dict]
) -> list[dict]:
    """Hits labeled by who the author is now, with mentions as names."""
    ids = {m["author_id"] for m in rows}
    for m in rows:
        ids.update(int(i) for i in MENTION.findall(m["content"]))
    names = await speakers(connection, scope, roster, ids)
    channels = await channel_names(connection, scope, {m["channel_id"] for m in rows})
    return [cite(scope, m, names, channels) for m in rows]


def image_count(m: dict) -> int:
    from .reading import images_in  # reading.py imports this module

    return len(images_in({"attachments": m.get("attachments"), "embeds": m.get("embeds")}))


def cite(scope: Scope, m: dict, names: dict[int, str], channels: dict[int, str]) -> dict:
    def mention(match: re.Match) -> str:
        user_id = int(match[1])
        return "@" + names.get(user_id, match[0])

    cited = {
        "id": str(m["id"]),
        "author": names.get(m["author_id"]) or m["author_name"] or str(m["author_id"]),
        "channel": "#" + channels.get(m["channel_id"], str(m["channel_id"])),
        "time": m["created_at"].isoformat(timespec="minutes"),
        "url": f"https://discord.com/channels/{scope.guild_id}/{m['channel_id']}/{m['id']}",
        "text": MENTION.sub(mention, m["content"])[:SNIPPET_CHARS],
    }
    if images := image_count(m):
        cited["images"] = images  # read_image can look at them
    return cited


async def index_all(database_url: str, api_key: str) -> None:
    """`kiki-agent-host index`: embeds every pending message, then exits."""
    embed, below, total = Embedder(openrouter_client(api_key)), None, 0
    async with await AsyncConnection.connect(database_url, autocommit=True) as connection:
        while True:
            found, below = await index_pending(connection, embed, below=below)
            total += found
            logger.info("indexed %d messages, cost $%.6f", total, embed.cost)
            if below is None:
                return
