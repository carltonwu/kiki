"""Message search over Postgres, independent of the agent framework.

Hybrid: full text plus pgvector, fused by reciprocal rank with a small recency
vote. Everything is scoped to one guild: Kiki knows every channel she can read.
"""

import asyncio
import logging
import math
import re
import time
from datetime import datetime
from typing import Literal

from psycopg import AsyncConnection, errors
from psycopg.rows import dict_row

from ..openrouter.embeddings import Embedder
from .attachments import images_in
from .cards import shown_text
from .scope import (
    AUTHOR,
    HEARD,
    MENTION,
    NO_ROSTER,
    PEOPLES,
    TEXT,
    Roster,
    Scope,
    channel_names,
    resolve_channel,
    resolve_person,
    speakers,
)

logger = logging.getLogger(__name__)


CANDIDATES = 50
RRF_K = 60
# Nearest neighbors exist even for nonsense; below this they're noise. A starting
# point: calibrate from the top_similarity values that searches log at DEBUG.
MIN_SIMILARITY = 0.25
# Recency adds a fraction of a top-ranked vote, halving every HALF_LIFE_DAYS.
RECENCY_WEIGHT = 0.15
HALF_LIFE_DAYS = 30
SNIPPET_CHARS = 1000
# Semantic candidates are reranked from this many index neighbours: 200 found
# 96% of the exact top 50 on production's embeddings.
SHORTLIST = 200
# The HNSW index returns its nearest `ef_search` before filters apply, so a
# scoped search (one person, one channel) could come back short. Iterative
# scans keep walking the graph until the LIMIT is met; relaxed order is fine
# because the shortlist is reranked.
INDEX_SETTINGS = "SET hnsw.ef_search = 200; SET hnsw.iterative_scan = relaxed_order"
# Caps on a search's steps, each also cut to what's left of its deadline
# (`Deadline`; the search_messages tool gives it 9 s of its 10). A cold HNSW
# cache made one search query take 5.7 s in production.
EMBED_SECONDS = 4  # the query embedding; past it, the search is lexical
STATEMENT_SECONDS = 3  # the hybrid query; past it, a lexical one is tried
LEXICAL_SECONDS = 1  # kept back for that lexical query
CITE_SECONDS = 1  # kept back for citing the results (names, channels); past it, IDs
RESOLVE_SECONDS = 1  # resolving `person` and `channel`
MIN_STEP_SECONDS = 0.05  # less than this left, a step isn't started
UNCITED_NOTE = (
    "Some names and channels couldn't be looked up in time: they're shown as IDs or "
    "as they were when posted."
)


# The client-side backstop's grace past `seconds`, so the server's statement
# timeout normally fires first and the transaction ends cleanly; the deadline
# leaves a second before the tool's own.
BACKSTOP_GRACE_SECONDS = 0.2


async def bounded(connection: AsyncConnection, seconds: float, work):
    """`await work()`, whose queries run on `connection`, within about `seconds`:
    each statement under a server-side statement_timeout (SET LOCAL, in a
    transaction), the whole under a client-side backstop. Raises TimeoutError."""
    if seconds < MIN_STEP_SECONDS:
        raise TimeoutError
    if math.isinf(seconds):  # no deadline (tests, the CLI)
        async with connection.transaction():
            return await work()
    try:
        async with (
            asyncio.timeout(seconds + BACKSTOP_GRACE_SECONDS),
            connection.transaction(),
        ):
            await connection.execute(f"SET LOCAL statement_timeout = '{int(seconds * 1000)}ms'")
            return await work()
    except errors.QueryCanceled as error:
        raise TimeoutError from error


class Deadline:
    """One wall-clock deadline that a search's steps share. Local to search on
    purpose: each use is `min(cap, deadline.left(keep=...))`, so it maps onto
    a shared deadline helper (one that caps a step to the time left) if the
    host gets one."""

    def __init__(self, seconds: float):
        self.at = time.monotonic() + seconds

    def left(self, keep: float = 0) -> float:
        """Seconds left, less `keep` held back for later steps (never below 0)."""
        return max(0.0, self.at - time.monotonic() - keep)


NO_DEADLINE = Deadline(math.inf)


Semantic = Literal["full", "partial", "off"]


# `own` is Kiki's user ID when the search is for her own messages: bots' are
# otherwise left out, hers included.
SCOPE = f"""
m.guild_id = %(guild)s AND m.deleted_at IS NULL
AND ({PEOPLES} OR m.author_id = %(own)s::bigint)
AND (%(channels)s::bigint[] IS NULL OR m.channel_id = ANY(%(channels)s))
AND (%(authors)s::bigint[] IS NULL OR {AUTHOR} = ANY(%(authors)s))
AND (%(after)s::timestamptz IS NULL OR m.created_at >= %(after)s)
AND (%(before)s::timestamptz IS NULL OR m.created_at < %(before)s)
AND NOT EXISTS (SELECT 1 FROM conversation_messages cm
                WHERE cm.message_id = m.id AND cm.conversation_id = %(conversation)s)
"""


COLUMNS = (
    "m.id, m.channel_id, CASE WHEN m.relayed_for IS NULL THEN m.author_name END AS author_name,"
    f" {AUTHOR} AS author_id, {TEXT} AS content, m.created_at,"
    " m.raw->'attachments' AS attachments, m.raw->'embeds' AS embeds,"
    " m.raw->'components' AS components"
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
), nearest AS (
    -- The index holds the embeddings' first 512 dimensions (Qwen's are
    -- Matryoshka, so a prefix is a coarser embedding): it shortlists by those,
    -- then the full vectors rank the shortlist. Ordered by the bare indexed
    -- expression and limited, so the index can serve it; the similarity floor
    -- applies after, or Postgres would scan every vector.
    SELECT id, distance FROM (
        SELECT m.id, e.embedding <=> %(vector)s::halfvec AS distance
        FROM messages m JOIN message_embeddings e ON e.message_id = m.id
        WHERE %(vector)s::halfvec IS NOT NULL AND {SCOPE}
        ORDER BY subvector(e.embedding, 1, 512)::halfvec(512)
                 <=> subvector(%(vector)s::halfvec, 1, 512)::halfvec(512)
        LIMIT %(shortlist)s
    ) shortlist
    ORDER BY distance LIMIT %(candidates)s
), semantic AS (
    SELECT id, 1 - distance AS similarity, row_number() OVER (ORDER BY distance) AS r
    FROM nearest WHERE distance <= 1 - %(min_similarity)s
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


# How the search refers to Kiki herself (`person`).
YOU = {"you", "yourself", "kiki"}

# Kiki's user ID: the author of her latest reply in the guild. The host isn't told
# it; her replies are recorded on their runs.
OWN_ID = """
SELECT m.author_id FROM agent_runs r JOIN messages m ON m.id = r.reply_message_ids[1]
WHERE m.guild_id = %s AND cardinality(r.reply_message_ids) > 0
ORDER BY r.id DESC LIMIT 1
"""


async def own_id(connection: AsyncConnection, scope: Scope) -> int | None:
    row = await (await connection.execute(OWN_ID, (scope.guild_id,))).fetchone()
    return row[0] if row else None


async def run_search(connection: AsyncConnection, params: dict, seconds: float) -> list | None:
    """SEARCH within `seconds` (`bounded`); None if it ran out (or no time was
    left to start it)."""
    cursor = connection.cursor(row_factory=dict_row)

    async def work():
        await cursor.execute(INDEX_SETTINGS)
        await cursor.execute(SEARCH, params)
        return await cursor.fetchall()

    try:
        return await bounded(connection, seconds, work)
    except TimeoutError:
        return None


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
    deadline: Deadline = NO_DEADLINE,
) -> dict:
    """`semantic` is the coverage of the guild's embeddings going in. Each step
    gets what's left of `deadline`: a slow embedding or hybrid query falls back
    to a lexical search rather than running the caller out of time."""
    if not query.strip():
        raise ValueError("query must not be empty")
    authors = channels = own = None
    yours = bool(person) and person.strip().removeprefix("@").lower() in YOU
    if (person and person.strip()) or (channel and channel.strip()):

        async def resolve():
            found_authors = found_channels = found_own = None
            if yours:
                found_own = await own_id(connection, scope)
                found_authors = [] if found_own is None else [found_own]
            elif person and person.strip():
                found_authors = await resolve_person(connection, scope, roster, person)
            if channel and channel.strip():
                found_channels = await resolve_channel(connection, scope, channel)
            return found_authors, found_channels, found_own

        try:
            authors, channels, own = await bounded(
                connection,
                min(RESOLVE_SECONDS, deadline.left(keep=LEXICAL_SECONDS + CITE_SECONDS)),
                resolve,
            )
        except TimeoutError:
            # No results yet to fall back on.
            return {"error": "The search took too long. Try again, or narrow it down."}
        if authors is not None and not authors:
            if yours:
                return {"error": "You haven't posted in this server yet."}
            return {
                "error": f"No one named {person!r} has written in this server. Names match"
                " exactly: try their ref, @username or another name they go by, or search"
                " without `person` and check who wrote each hit."
            }
        if channels is not None and not channels:
            return {"error": f"There's no channel named {channel!r} in this server."}
    try:
        # A total, unlike the client's own timeouts (per connect, read, ...).
        async with asyncio.timeout(
            min(EMBED_SECONDS, deadline.left(keep=LEXICAL_SECONDS + CITE_SECONDS))
        ):
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
        "own": own,
        "before": before,
        "after": after,
        "query": query,
        "vector": vector,
        "candidates": CANDIDATES,
        "shortlist": SHORTLIST,
        "k": RRF_K,
        "min_similarity": MIN_SIMILARITY,
        "half_life": HALF_LIFE_DAYS,
        # An explicit date range is a historical question: don't favor recent.
        "recency": 0 if before or after else RECENCY_WEIGHT,
        "limit": max(1, min(limit, 10)),
    }
    rows = None
    if vector is not None:
        rows = await run_search(
            connection,
            params,
            min(STATEMENT_SECONDS, deadline.left(keep=LEXICAL_SECONDS + CITE_SECONDS)),
        )
        if rows is None:
            logger.info(
                "hybrid search timed out channel_id=%s conversation_id=%s; searching lexically",
                scope.channel_id,
                scope.conversation_id,
            )
            params["vector"], semantic = None, "off"
    if rows is None:
        rows = await run_search(
            connection, params, min(STATEMENT_SECONDS, deadline.left(keep=CITE_SECONDS))
        )
    if rows is None:
        logger.info(
            "search timed out channel_id=%s conversation_id=%s semantic=%s",
            scope.channel_id,
            scope.conversation_id,
            semantic,
        )
        return {"error": "The search took too long. Try again, or narrow it down."}
    similarities = [m["similarity"] for m in rows if m["similarity"] is not None]
    logger.debug(
        "search completed channel_id=%s conversation_id=%s hits=%d top_similarity=%s semantic=%s",
        scope.channel_id,
        scope.conversation_id,
        len(rows),
        max(similarities, default=None),
        semantic,
    )

    async def enrich():
        cited = await cite_all(connection, scope, roster, rows)
        names = {}
        if authors and len(authors) > 1:
            names = await speakers(connection, scope, roster, set(authors))
        return cited, names

    try:
        cited, names = await bounded(connection, deadline.left(), enrich)
    except TimeoutError:
        # The results stand; only the lookups that label them ran out of time.
        logger.info(
            "search citations timed out channel_id=%s conversation_id=%s hits=%d",
            scope.channel_id,
            scope.conversation_id,
            len(rows),
        )
        names = dict(roster.speakers)
        cited = [cite(scope, m, names, {}) for m in rows]
        result = {"semantic": semantic, "messages": cited, "enriched": False}
        result["note"] = UNCITED_NOTE
        return result
    result = {"semantic": semantic, "messages": cited}
    if own is not None:
        # Her messages aren't embedded (indexing.PENDING): words only.
        result["note"] = "Your own messages match by their words only, not by meaning."
    elif not rows:
        result["note"] = (
            "Nothing matched. If it should be here, try the words they'd have used"
            " (names, slang, a distinctive phrase) or fewer filters."
        )
    if authors and len(authors) > 1:
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
    result = {"messages": await cite_all(connection, scope, roster, rows)}
    if all(row["id"] != message_id for row in rows):
        # It exists (else no rows), but its author is one Kiki doesn't read.
        result["note"] = (
            "That message is from an author you don't read (another chatbot), so it"
            " isn't shown; these are the messages around it. It wasn't deleted."
        )
    return result


async def cite_all(
    connection: AsyncConnection,
    scope: Scope,
    roster: Roster,
    rows: list[dict],
    *,
    chars: int | None = SNIPPET_CHARS,
) -> list[dict]:
    """Hits labeled by who the author is now, with mentions as names: the first
    `chars` of their text, or all of it."""
    ids = {m["author_id"] for m in rows}
    for m in rows:
        ids.update(int(i) for i in MENTION.findall(shown_text(m)))
    names = await speakers(connection, scope, roster, ids)
    channels = await channel_names(connection, scope, {m["channel_id"] for m in rows})
    return [cite(scope, m, names, channels, chars) for m in rows]


def image_count(m: dict) -> int:

    return len(images_in({"attachments": m.get("attachments"), "embeds": m.get("embeds")}))


def cite(
    scope: Scope,
    m: dict,
    names: dict[int, str],
    channels: dict[int, str],
    chars: int | None = SNIPPET_CHARS,
) -> dict:
    def mention(match: re.Match) -> str:
        user_id = int(match[1])
        return "@" + names.get(user_id, match[0])

    cited = {
        "id": str(m["id"]),
        "author": names.get(m["author_id"]) or m["author_name"] or str(m["author_id"]),
        "channel": "#" + channels.get(m["channel_id"], str(m["channel_id"])),
        "time": m["created_at"].isoformat(timespec="minutes"),
        "url": f"https://discord.com/channels/{scope.guild_id}/{m['channel_id']}/{m['id']}",
        "text": MENTION.sub(mention, shown_text(m))[:chars],
    }
    if images := image_count(m):
        cited["images"] = images  # read_image can look at them
    return cited
