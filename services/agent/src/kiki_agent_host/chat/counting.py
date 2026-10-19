"""Exact message counts over Postgres: one aggregate query, scoped to one guild.

Search returns a handful of hits; this answers "who says X the most" and "how
often per year" with real counts. Full text (`messages_fts_idx`) and an optional
case-insensitive regex select messages; there is no semantic match, since a
nearest neighbor always exists and would make every count meaningless.
"""

import re
from datetime import datetime
from typing import Literal

from psycopg import AsyncConnection, errors
from psycopg.rows import dict_row

from .scope import (
    HEARD,
    NO_ROSTER,
    Roster,
    Scope,
    channel_names,
    resolve_channel,
    resolve_person,
    speakers,
)

GroupBy = Literal["person", "channel", "month", "year"]

MAX_GROUPS = 50
MAX_PATTERN_CHARS = 200
EXAMPLE_CHARS = 200
# Below the tool's 10 s deadline, so a runaway regex comes back as advice to narrow
# the count rather than a bare timeout. 235k messages scan in well under a second
# without a pattern.
STATEMENT_TIMEOUT = "6s"

# Fixed SQL fragments, chosen by validated arguments; nothing from the model is
# formatted into the query.
KEYS = {
    None: "NULL::bigint",
    "person": "m.author_id",
    "channel": "m.channel_id",
    "month": "date_trunc('month', m.created_at AT TIME ZONE 'UTC')",
    "year": "date_trunc('year', m.created_at AT TIME ZONE 'UTC')",
}
FILTERS = {
    "channels": "m.channel_id = ANY(%(channels)s)",
    "authors": "m.author_id = ANY(%(authors)s)",
    "after": "m.created_at >= %(after)s",
    "before": "m.created_at < %(before)s",
    "query": "to_tsvector('english', m.content) @@ websearch_to_tsquery('english', %(query)s)",
    "pattern": "m.content ~* %(pattern)s",
}
# Examples come from outside the current conversation when they can: it's already
# in the prompt, and its newest match is usually the question being asked. Counts
# still include it. An InitPlan, evaluated once, like `HEARD`.
UNSHOWN = """m.id <> ALL (ARRAY(SELECT message_id FROM conversation_messages
                             WHERE conversation_id = %(conversation)s))"""
# Postgres regexes spell a word boundary \y; \b, which models write, is a backspace.
WORD_BOUNDARY = re.compile(r"(?<!\\)((?:\\\\)*)\\b")


def count_sql(group_by: GroupBy | None, filters: list[str]) -> str:
    where = " AND ".join(
        [f"m.guild_id = %(guild)s AND m.deleted_at IS NULL AND NOT m.author_bot AND {HEARD}"]
        + [FILTERS[f] for f in filters]
    )
    return f"""
SELECT {KEYS[group_by]} AS key, count(*) AS n, coalesce(max(m.id) FILTER (WHERE {UNSHOWN}),
                                                       max(m.id)) AS example
FROM messages m WHERE {where}
GROUP BY 1 ORDER BY n DESC, key
"""


async def count(
    connection: AsyncConnection,
    scope: Scope,
    *,
    query: str | None = None,
    pattern: str | None = None,
    person: str | None = None,
    channel: str | None = None,
    roster: Roster = NO_ROSTER,
    before: datetime | None = None,
    after: datetime | None = None,
    group_by: GroupBy | None = None,
    limit: int = 10,
) -> dict:
    """Counts messages, not occurrences. Takes `connection` for itself: it opens a
    transaction to scope the statement timeout."""
    query = query.strip() if query and query.strip() else None
    pattern = pattern.strip() if pattern and pattern.strip() else None
    if pattern:
        if len(pattern) > MAX_PATTERN_CHARS:
            raise ValueError(f"pattern must be at most {MAX_PATTERN_CHARS} characters")
        pattern = WORD_BOUNDARY.sub(r"\1\\y", pattern)
    params: dict = {
        "guild": scope.guild_id,
        "conversation": scope.conversation_id,
        "query": query,
        "pattern": pattern,
    }
    filters = [name for name in ("query", "pattern") if params[name]]
    authors = None
    if person and person.strip():
        authors = await resolve_person(connection, scope, roster, person)
        if not authors:
            return {"error": f"No one named {person!r} has written in this server."}
        params["authors"] = authors
        filters.append("authors")
    if channel and channel.strip():
        params["channels"] = await resolve_channel(connection, scope, channel)
        if not params["channels"]:
            return {"error": f"There's no channel named {channel!r} in this server."}
        filters.append("channels")
    for name, value in (("after", after), ("before", before)):
        if value:
            params[name] = value
            filters.append(name)
    if query:
        cursor = await connection.execute(
            "SELECT numnode(websearch_to_tsquery('english', %s))", (query,)
        )
        if (await cursor.fetchone())[0] == 0:
            raise ValueError(
                "query has only words full-text search ignores (like 'the' or 'so');"
                " use pattern to match them exactly"
            )

    try:
        async with connection.transaction():
            await connection.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")
            cursor = connection.cursor(row_factory=dict_row)
            await cursor.execute(count_sql(group_by, filters), params)
            groups = await cursor.fetchall()
            top = groups[: max(1, min(limit, MAX_GROUPS))]
            examples = await example_rows(connection, top)
    except errors.QueryCanceled:
        return {
            "error": "Counting took too long. Narrow it: add a query alongside the"
            " pattern, or a person, channel or time range."
        }
    except errors.InvalidRegularExpression as error:
        raise ValueError(f"pattern is not a valid regular expression: {error}") from None

    result = {"total": sum(g["n"] for g in groups)}
    if group_by:
        labels = await label(connection, scope, roster, group_by, {g["key"] for g in top})
        result["group_by"] = group_by
        result["groups_total"] = len(groups)
        result["groups"] = [
            {
                group_by: labels[g["key"]],
                "count": g["n"],
                "example": cite(scope, examples[g["example"]]),
            }
            for g in top
        ]
    elif groups:
        result["example"] = cite(scope, examples[groups[0]["example"]])
    if authors and len(authors) > 1:
        names = await speakers(connection, scope, roster, set(authors))
        result["note"] = (
            f"{person!r} matches {len(authors)} different people: "
            + "; ".join(names.get(a, str(a)) for a in authors)
            + ". Counts include all of them."
        )
    return result


async def example_rows(connection: AsyncConnection, groups: list[dict]) -> dict[int, dict]:
    """Each group's example message, to cite."""
    cursor = connection.cursor(row_factory=dict_row)
    await cursor.execute(
        "SELECT id, channel_id, content, created_at FROM messages WHERE id = ANY(%s)",
        ([g["example"] for g in groups],),
    )
    return {row["id"]: row for row in await cursor.fetchall()}


async def label(
    connection: AsyncConnection, scope: Scope, roster: Roster, group_by: GroupBy, keys: set
) -> dict:
    if group_by == "person":
        names = await speakers(connection, scope, roster, keys)
        return {k: names.get(k, str(k)) for k in keys}
    if group_by == "channel":
        names = await channel_names(connection, scope, keys)
        return {k: "#" + names.get(k, str(k)) for k in keys}
    return {k: k.strftime("%Y-%m" if group_by == "month" else "%Y") for k in keys}


def cite(scope: Scope, m: dict) -> dict:
    return {
        "url": f"https://discord.com/channels/{scope.guild_id}/{m['channel_id']}/{m['id']}",
        "time": m["created_at"].isoformat(timespec="minutes"),
        "text": m["content"][:EXAMPLE_CHARS],
    }
