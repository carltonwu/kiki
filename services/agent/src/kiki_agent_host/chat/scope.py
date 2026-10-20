"""Who and where a job is about: its scope, the people in the prompt, and how
names, mentions and channels resolve to IDs within the scope."""

import re
from dataclasses import dataclass, field

from psycopg import AsyncConnection


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
        return [int(match[1] or match[2])]
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
        ids, names = [int(match[1] or match[2])], None
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


# Authors Kiki never reads (migration 0010), for messages aliased `m`, except
# their reposts of someone's message (a link fixer's; migration 0024). Not an
# anti-join: the one-row table is never auto-analyzed, and the planner's guess
# of 1,200 rows turned the guild-wide pending scan into an 84 s nested loop.
AUTHORS_HEARD = (
    "(m.author_id <> ALL (ARRAY(SELECT user_id FROM ignored_authors)) OR m.relayed_for IS NOT NULL)"
)
# The ingester's failure notes: a bot's message with their exact text, alone or
# under the redirect header (which covers one whose send response was lost), or
# one their run recorded (migration 0033); only bots' rows get any of it. Rust's
# `agent::NOT_A_FAILURE_NOTE`, character for character (test_history.py checks).
NOT_A_FAILURE_NOTE = (
    r"NOT (m.author_bot AND m.relayed_for IS NULL AND"
    r" (m.content = '(couldn''t answer that one, try again?)'"
    r" OR (starts_with(m.content, '-# ↪ <@') AND m.content ~ '^-# ↪ <@[0-9]+> in"
    r" https://discord\.com/channels/[0-9]+/[0-9]+/[0-9]+\n\(couldn''t answer that one,"
    r" try again\?\)$')"
    r" OR m.id = ANY (ARRAY(SELECT unnest(n.reply_message_ids) FROM agent_runs n"
    r" WHERE n.failure_note))))"
)
HEARD = f"({AUTHORS_HEARD} AND {NOT_A_FAILURE_NOTE})"
# People's messages Kiki reads: no bots', but a link fixer's reposts of theirs.
# Failure notes are Kiki's, so excluded already; this stays the rule migration
# 0029's embedding queue repeats.
PEOPLES = f"(NOT m.author_bot OR m.relayed_for IS NOT NULL) AND {AUTHORS_HEARD}"
# Who wrote a message and what they wrote, a repost's as its author's.
AUTHOR = "coalesce(m.relayed_for, m.author_id)"
TEXT = "coalesce(m.relayed_text, m.content)"
