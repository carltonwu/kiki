"""A channel's messages in time order, for catch-ups: one page per call with a
cursor for the next. Unlike search, nothing is ranked or left out but deleted
messages and ignored authors: bots post too (voice transcripts), and the current
conversation belongs in the timeline."""

import re
from datetime import UTC, datetime

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from .scope import HEARD, NO_ROSTER, Roster, Scope, resolve_channel
from .search import COLUMNS, SNIPPET_CHARS, cite_all

MAX_LIMIT = 100
# About 10k tokens of text: a page stops early rather than flood the context window.
PAGE_CHARS = 40_000
MAX_FILES = 10
MESSAGE_ID = re.compile(r"^[0-9]{1,20}$")
CURSOR = re.compile(r"^(newer|older):([0-9]{1,20})$")


# A channel and its threads. Bounds are `(created_at, id)` pairs taken from a
# message of the guild (message IDs and cursors) or timestamps alone.
RANGE = f"""
m.guild_id = %(guild)s AND m.deleted_at IS NULL AND {HEARD}
AND (m.channel_id = ANY(%(channels)s) OR m.channel_id IN (
     SELECT id FROM channels WHERE guild_id = %(guild)s AND parent_id = ANY(%(channels)s)))
AND (%(after)s::timestamptz IS NULL OR m.created_at >= %(after)s)
AND (%(before)s::timestamptz IS NULL OR m.created_at < %(before)s)
AND (%(after_id)s::bigint IS NULL OR (m.created_at, m.id) > (
     SELECT created_at, id FROM messages WHERE id = %(after_id)s))
AND (%(before_id)s::bigint IS NULL OR (m.created_at, m.id) < (
     SELECT created_at, id FROM messages WHERE id = %(before_id)s))
"""

# One more row than the limit, to know whether there's another page. `{order}`
# is ASC reading forward from a start, DESC reading back from the end.
PAGE = f"""
SELECT * FROM (
    SELECT {COLUMNS}, m.author_bot AND m.relayed_for IS NULL AS author_bot FROM messages m
    WHERE {RANGE}
      AND (%(newer)s::bigint IS NULL OR (m.created_at, m.id) > (
           SELECT created_at, id FROM messages WHERE id = %(newer)s))
      AND (%(older)s::bigint IS NULL OR (m.created_at, m.id) < (
           SELECT created_at, id FROM messages WHERE id = %(older)s))
    ORDER BY m.created_at {{order}}, m.id {{order}}
    LIMIT %(limit)s + 1
) page ORDER BY created_at, id
"""

COUNT = f"SELECT count(*) FROM messages m WHERE {RANGE}"


async def channel_history(
    connection: AsyncConnection,
    scope: Scope,
    *,
    channel: str | None = None,
    after: str | None = None,
    before: str | None = None,
    limit: int = MAX_LIMIT,
    cursor: str | None = None,
    roster: Roster = NO_ROSTER,
) -> dict:
    """`after`/`before` are an ISO 8601 time (UTC unless it has an offset) or a
    message ID. `cursor` comes from the previous page of the same call."""
    channels = [scope.channel_id]
    if channel and channel.strip():
        channels = await resolve_channel(connection, scope, channel)
        if not channels:
            return {"error": f"There's no channel named {channel!r} in this server."}
    after_time, after_id = bound("after", after)
    before_time, before_id = bound("before", before)
    newer = older = None
    if cursor and cursor.strip():
        match = CURSOR.match(cursor.strip())
        if not match:
            raise ValueError("cursor must be the `cursor` value from the previous page")
        newer, older = (int(match[2]), None) if match[1] == "newer" else (None, int(match[2]))
    referenced = {i for i in (after_id, before_id, newer, older) if i is not None}
    if referenced and not await in_guild(connection, scope, referenced):
        return {"error": "no such message in this server"}
    params = {
        "guild": scope.guild_id,
        "channels": channels,
        "after": after_time,
        "before": before_time,
        "after_id": after_id,
        "before_id": before_id,
        "newer": newer,
        "older": older,
        "limit": max(1, min(limit, MAX_LIMIT)),
    }
    # With a start, read forward from it; otherwise the newest messages.
    forward = after_time is not None or after_id is not None or newer is not None
    rows_cursor = connection.cursor(row_factory=dict_row)
    await rows_cursor.execute(PAGE.format(order="ASC" if forward else "DESC"), params)
    rows = await rows_cursor.fetchall()
    more = len(rows) > params["limit"]
    if more:
        rows = rows[:-1] if forward else rows[1:]
    rows, cut = fit(rows, forward=forward)
    more = more or cut
    total = (await (await connection.execute(COUNT, params)).fetchone())[0]
    messages = await cite_all(connection, scope, roster, rows)
    for m, row in zip(messages, rows, strict=True):
        if row["author_bot"]:
            m["bot"] = True
        if files := [f.get("filename") or "file" for f in row["attachments"] or []][:MAX_FILES]:
            m["files"] = files
    result = {"messages": messages, "total_in_range": total}
    if more and rows:
        if forward:
            result["cursor"] = f"newer:{rows[-1]['id']}"
            result["note"] = "More messages follow; pass `cursor` to read the next page."
        else:
            result["cursor"] = f"older:{rows[0]['id']}"
            result["note"] = (
                "These are the newest; earlier ones exist. Pass `cursor` to read back further."
            )
    return result


def bound(name: str, value: str | None) -> tuple[datetime | None, int | None]:
    """A time or a message ID; blank means no bound (some models send "")."""
    if value is None or not value.strip():
        return None, None
    value = value.strip()
    if MESSAGE_ID.match(value):
        return None, int(value)
    try:
        time = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError(
            f"{name} must be an ISO 8601 time (like 2026-10-18T02:00-04:00) or a message ID"
        ) from None
    return (time if time.tzinfo else time.replace(tzinfo=UTC)), None


async def in_guild(connection: AsyncConnection, scope: Scope, ids: set[int]) -> bool:
    found = await connection.execute(
        "SELECT count(*) FROM messages WHERE guild_id = %s AND id = ANY(%s)",
        (scope.guild_id, list(ids)),
    )
    return (await found.fetchone())[0] == len(ids)


def fit(rows: list[dict], *, forward: bool) -> tuple[list[dict], bool]:
    """Rows within `PAGE_CHARS`, kept from the end the page reads from; at least one."""
    ordered = rows if forward else rows[::-1]
    kept, chars = [], 0
    for row in ordered:
        chars += min(len(row["content"]), SNIPPET_CHARS)
        if kept and chars > PAGE_CHARS:
            break
        kept.append(row)
    cut = len(kept) < len(rows)
    return (kept if forward else kept[::-1]), cut
