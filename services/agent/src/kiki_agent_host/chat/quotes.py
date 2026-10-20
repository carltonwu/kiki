"""The server's saved quotes: quote bots' posts (migration 0030), each read as the
message it saved, so the text is verbatim and the author is who said it.

A quote can be posted many times (`.quote 194` again and again); it counts once.
When the saved message was never ingested, the bot's copy stands in for it, if it
links a message of this guild and isn't attributed to someone Kiki doesn't read.
When it was deleted, the quote is left out like any deleted message."""

import asyncio
import logging
import re
from datetime import UTC, datetime

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from ..openrouter.embeddings import Embedder
from .scope import MENTION, NO_ROSTER, PEOPLES, Roster, Scope, resolve_person
from .search import (
    CITE_SECONDS,
    COLUMNS,
    EMBED_SECONDS,
    MIN_SIMILARITY,
    NO_DEADLINE,
    RESOLVE_SECONDS,
    RRF_K,
    STATEMENT_SECONDS,
    UNCITED_NOTE,
    Deadline,
    bounded,
    cite,
    cite_all,
)

logger = logging.getLogger(__name__)

MAX_LIMIT = 50
# A page's text, about 10k tokens (as channel_history's): quotes come whole, and
# a message can be 4,000 characters.
PAGE_CHARS = 40_000

# Every saved message once, with its newest quote post. A missing original
# (`found` false) wasn't ingested; one that's deleted, a bot's or an ignored
# author's isn't a quote Kiki shows.
QUOTES = f"""
SELECT DISTINCT ON (q.original) q.original, q.post_id, q.attribution, q.body,
       m.id IS NOT NULL AS found, {COLUMNS}
FROM (SELECT p.id AS post_id, quoted_message_id(p.raw) AS original, parts.attribution, parts.body
      FROM messages p CROSS JOIN LATERAL quote_parts(p.raw) parts
      WHERE p.guild_id = %(guild)s AND p.author_bot AND quoted_message_id(p.raw) IS NOT NULL
        AND p.deleted_at IS NULL) q
LEFT JOIN messages m ON m.id = q.original
WHERE m.id IS NULL OR (m.guild_id = %(guild)s AND m.deleted_at IS NULL AND {PEOPLES})
ORDER BY q.original, q.post_id DESC
"""

# The quotes' relevance to a query: keywords (any word, like search) over their
# text, the bot's copy included, and meaning for stored messages. Few rows, so
# exact distances, no index.
RANK = """
SELECT v.id, ts_rank_cd(to_tsvector('english', v.text), q.query) AS lexical,
       to_tsvector('english', v.text) @@ q.query AS matched,
       1 - (e.embedding <=> %(vector)s::halfvec) AS similarity
FROM unnest(%(ids)s::bigint[], %(texts)s::text[]) AS v(id, text)
LEFT JOIN message_embeddings e ON e.message_id = v.id,
     (SELECT replace(plainto_tsquery('english', %(query)s)::text, ' & ', ' | ')::tsquery
             AS query) q
"""

# Who a copy's attribution names, when it names them (old posts) rather than
# mentioning them: every account that has gone by the name.
NAMED = """
SELECT w.who, array_agg(DISTINCT n.user_id) FROM unnest(%(names)s::text[]) AS w(who)
JOIN user_names n ON n.guild_id = %(guild)s AND n.name_norm = name_norm(w.who)
                 AND n.name_norm <> ''
GROUP BY w.who
"""

# Of these accounts, the ones Kiki doesn't read: ignored authors and bots, known
# as bots from their messages or from any message that showed them (`people.bot`
# is set from mentions too).
UNREAD = """
SELECT a.id FROM unnest(%(ids)s::bigint[]) AS a(id)
WHERE a.id = ANY(ARRAY(SELECT user_id FROM ignored_authors))
   OR EXISTS (SELECT 1 FROM people p WHERE p.user_id = a.id AND p.bot)
   OR EXISTS (SELECT 1 FROM messages m WHERE m.author_id = a.id AND m.author_bot)
"""

# The attribution's link (the same one `quoted_message_id` takes).
LINK = re.compile(
    r"\]\(https://(?:(?:ptb|canary)\.)?discord(?:app)?\.com/channels/([0-9]+)/([0-9]+)/([0-9]+)\)",
    re.IGNORECASE,
)
# Who an attribution names, by each format's own delimiters, so a name with
# " - " or " at " in it stays whole. UB3R-B0T: everything between "- " and its
# jump link ("- Pilz - beta [(Jump)](…)"). PilzBrots: between "(Authored )by "
# and " at <date>" ("by Ann at Home at 2/24/21, …"), else its link.
UB3R_WHO = re.compile(
    r"^-\s*(.+?)\s*\[[^\]]*jump[^\]]*\]\(https://[^)\s]*\)\s*$", re.IGNORECASE | re.DOTALL
)
PILZ_WHO = re.compile(
    r"^(?:authored )?by\s+(.+?)\s+at\s+[0-9]{1,2}/[0-9]{1,2}/[0-9]{2,4}\b",
    re.IGNORECASE | re.DOTALL,
)
PILZ_WHO_UNDATED = re.compile(
    r"^(?:authored )?by\s+(.+?)\s*(?:-\s*)?\[[^\]]*view original message", re.IGNORECASE | re.DOTALL
)
BARE_ID = re.compile(r"^[0-9]{1,19}$")
DISCORD_EPOCH_MS = 1420070400000
MAX_SNOWFLAKE = 2**63 - 1


def snowflake(digits: str | None) -> int | None:
    """A Discord ID, or None if `digits` can't be one (as SQL's `snowflake`)."""
    if digits is None or not BARE_ID.match(digits) or int(digits) > MAX_SNOWFLAKE:
        return None
    return int(digits)


def attributed(attribution: str) -> str:
    """The person an attribution names, as written: a mention, an ID or a name."""
    line = attribution.strip()
    for pattern in (UB3R_WHO, PILZ_WHO, PILZ_WHO_UNDATED):
        if who := pattern.match(line):
            return who[1].strip()
    return ""


def saved_copy(row: dict, guild_id: int) -> dict | None:
    """A quote whose message wasn't ingested, as the bot copied it. None unless
    its link is to this guild: another guild's message isn't Kiki's to show."""
    link = LINK.search(row["attribution"] or "")
    if not link or snowflake(link[1]) != guild_id or snowflake(link[2]) is None:
        return None
    original = row["original"]
    who = attributed(row["attribution"])
    mention = MENTION.fullmatch(who)
    author = snowflake(mention[1] if mention else who)
    return {
        **row,
        "id": original,
        "channel_id": snowflake(link[2]),
        "author_id": author,
        # A name to check (`NAMED`), or "unknown".
        "author_name": None if author is not None else (who or "unknown"),
        "content": row["body"] or "",
        "created_at": datetime.fromtimestamp(((original >> 22) + DISCORD_EPOCH_MS) / 1000, tz=UTC),
        "attachments": None,
        "embeds": None,
        "components": None,
    }


async def readable_copies(
    connection: AsyncConnection, scope: Scope, copies: list[dict]
) -> list[dict]:
    """Copies that aren't by someone Kiki doesn't read (an ignored author or a
    bot), with a named author resolved to their account when only one has exactly
    that name (the whole name the attribution gives, never part of it)."""
    named = [c["author_name"] for c in copies if c["author_id"] is None]
    accounts: dict[str, list[int]] = {}
    if named:
        found = await connection.execute(NAMED, {"names": named, "guild": scope.guild_id})
        accounts = {who: ids for who, ids in await found.fetchall()}
    ids = {c["author_id"] for c in copies if c["author_id"] is not None}
    ids.update(i for group in accounts.values() for i in group)
    unread: set[int] = set()
    if ids:
        found = await connection.execute(UNREAD, {"ids": list(ids)})
        unread = {row[0] for row in await found.fetchall()}
    readable = []
    for copy in copies:
        if copy["author_id"] is None:
            group = accounts.get(copy["author_name"], [])
            if unread.intersection(group):
                continue
            if len(group) == 1:
                copy = {**copy, "author_id": group[0], "author_name": None}
        elif copy["author_id"] in unread:
            continue
        readable.append(copy)
    return readable


TOO_LONG = {"error": "Finding quotes took too long. Try again, or narrow it down."}


async def find_quotes(
    connection: AsyncConnection,
    embed: Embedder,
    scope: Scope,
    *,
    person: str | None = None,
    query: str | None = None,
    roster: Roster = NO_ROSTER,
    limit: int = 25,
    offset: int = 0,
    deadline: Deadline = NO_DEADLINE,
) -> dict:
    """Each step gets what's left of `deadline` (search's caps, `bounded`): a slow
    embedding ranks by keywords only; citing late shows IDs; anything else late is
    an error rather than running the tool out of time."""
    authors = None
    if person and person.strip():
        try:
            authors = await bounded(
                connection,
                min(RESOLVE_SECONDS, deadline.left(keep=STATEMENT_SECONDS + CITE_SECONDS)),
                lambda: resolve_person(connection, scope, roster, person),
            )
        except TimeoutError:
            return TOO_LONG
        if not authors:
            return {"error": f"No one named {person!r} has written in this server."}

    async def gather():
        cursor = connection.cursor(row_factory=dict_row)
        await cursor.execute(QUOTES, {"guild": scope.guild_id})
        rows = await cursor.fetchall()
        copies = [c for r in rows if not r["found"] and (c := saved_copy(r, scope.guild_id))]
        return [r for r in rows if r["found"]] + await readable_copies(connection, scope, copies)

    try:
        rows = await bounded(
            connection, min(STATEMENT_SECONDS, deadline.left(keep=CITE_SECONDS)), gather
        )
    except TimeoutError:
        logger.info("finding quotes timed out channel_id=%s", scope.channel_id)
        return TOO_LONG
    if authors:
        rows = [r for r in rows if r["author_id"] in authors]
    if query and query.strip():
        try:
            rows = await ranked(connection, embed, scope, query, rows, deadline)
        except TimeoutError:
            logger.info("ranking quotes timed out channel_id=%s", scope.channel_id)
            return TOO_LONG
    else:
        rows.sort(key=lambda r: r["created_at"], reverse=True)
    offset, limit = max(0, offset), max(1, min(limit, MAX_LIMIT))
    page = fit(rows[offset : offset + limit])
    # Whole texts, not search's snippets: a quote is quoted verbatim.
    result: dict = {"total": len(rows)}
    try:
        result["messages"] = await bounded(
            connection,
            deadline.left(),
            lambda: cite_all(connection, scope, roster, page, chars=None),
        )
    except TimeoutError:
        # The quotes stand; only the lookups that label them ran out of time.
        result["messages"] = [cite(scope, r, dict(roster.speakers), {}, None) for r in page]
        result["enriched"] = False
        result["note"] = UNCITED_NOTE
    for quote, row in zip(result["messages"], page, strict=True):
        if not row["found"]:
            quote["saved_copy"] = True  # the quote bot's copy; the message isn't stored
    if offset + len(page) < len(rows):
        result["next_offset"] = offset + len(page)
    if not rows:
        result["note"] = (
            "No saved quotes match. Quotes are what people saved with a quote bot;"
            " search_messages finds anything else they said."
        )
    return result


def fit(rows: list[dict]) -> list[dict]:
    """Rows within `PAGE_CHARS` of text, at least one: the rest are the next page."""
    kept, chars = [], 0
    for row in rows:
        chars += len(row["content"])
        if kept and chars > PAGE_CHARS:
            break
        kept.append(row)
    return kept


async def ranked(
    connection: AsyncConnection,
    embed: Embedder,
    scope: Scope,
    query: str,
    rows: list[dict],
    deadline: Deadline = NO_DEADLINE,
) -> list[dict]:
    """Rows matching `query` by keywords or meaning, best first (reciprocal rank
    fusion, as search). The bot's copies have no embedding: keywords only. Raises
    TimeoutError if the ranking query runs out of time."""
    try:
        async with asyncio.timeout(
            min(EMBED_SECONDS, deadline.left(keep=STATEMENT_SECONDS + CITE_SECONDS))
        ):
            [vector] = await embed([query], query=True)
    except Exception as error:  # noqa: BLE001 - lexical fallback without logging provider payloads
        logger.warning(
            "query embedding failed channel_id=%s error_type=%s; ranking quotes lexically",
            scope.channel_id,
            type(error).__name__,
        )
        vector = None
    by_id = {r["id"]: r for r in rows}
    params = {
        "query": query,
        "vector": vector,
        "ids": list(by_id),
        "texts": [r["content"] for r in by_id.values()],
    }

    async def rank():
        cursor = connection.cursor(row_factory=dict_row)
        await cursor.execute(RANK, params)
        return await cursor.fetchall()

    scores = await bounded(
        connection, min(STATEMENT_SECONDS, deadline.left(keep=CITE_SECONDS)), rank
    )
    lexical = sorted((s for s in scores if s["matched"]), key=lambda s: -s["lexical"])
    semantic = sorted(
        (s for s in scores if s["similarity"] is not None and s["similarity"] >= MIN_SIMILARITY),
        key=lambda s: -s["similarity"],
    )
    fused: dict[int, float] = {}
    for ranking in (lexical, semantic):
        for rank_, s in enumerate(ranking, 1):
            fused[s["id"]] = fused.get(s["id"], 0) + 1 / (RRF_K + rank_)
    return [by_id[i] for i in sorted(fused, key=lambda i: -fused[i])]
