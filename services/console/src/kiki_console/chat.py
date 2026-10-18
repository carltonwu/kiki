"""Console chat threads: stored here, answered by the ingester (real prompt + host)."""

import time
from uuid import UUID

from psycopg import AsyncConnection

from .content import Invalid, audit, text
from .ingester import Ingester, IngesterError

MESSAGE_CHARS = 4000
# Must stay within the ingester's MAX_TURNS (60), odd so the window starts with the user.
TURNS = 39


async def thread(db: AsyncConnection, guild_id: int, user_id: int, thread_id: UUID) -> list[tuple]:
    cursor = await db.execute(
        "SELECT id, role, content, model, cost_usd, latency_ms, error, created_at "
        "FROM console_chat_messages WHERE thread_id = %s AND guild_id = %s AND user_id = %s "
        "ORDER BY id",
        (thread_id, guild_id, user_id),
    )
    return await cursor.fetchall()


async def threads(db: AsyncConnection, guild_id: int, user_id: int) -> list[tuple]:
    cursor = await db.execute(
        "SELECT thread_id, min(created_at) AS started, count(*) AS messages, "
        "(array_agg(content ORDER BY id))[1] AS first "
        "FROM console_chat_messages WHERE guild_id = %s AND user_id = %s "
        "GROUP BY thread_id ORDER BY started DESC LIMIT 20",
        (guild_id, user_id),
    )
    return await cursor.fetchall()


def turns(rows: list[tuple]) -> list[dict]:
    """The latest answered exchanges plus the new message, starting with the user.
    Failed user messages (error set) never got an answer and are skipped."""
    kept = [{"role": r[1], "content": r[2]} for r in rows if r[6] is None]
    kept = kept[-TURNS:]
    if kept and kept[0]["role"] != "user":
        kept = kept[1:]
    return kept


async def send(
    db: AsyncConnection,
    ingester: Ingester,
    guild_id: int,
    user_id: int,
    user_name: str,
    thread_id: UUID,
    message: str,
) -> None:
    message = text(message, "Message", MESSAGE_CHARS)
    async with db.transaction():
        # One message at a time per thread, so turns keep alternating.
        await db.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (str(thread_id),))
        cursor = await db.execute(
            "SELECT DISTINCT user_id, guild_id FROM console_chat_messages WHERE thread_id = %s",
            (thread_id,),
        )
        owners = await cursor.fetchall()
        if owners and owners != [(user_id, guild_id)]:
            raise Invalid("That chat belongs to someone else.")
        cursor = await db.execute(
            "INSERT INTO console_chat_messages (thread_id, guild_id, user_id, role, content) "
            "VALUES (%s, %s, %s, 'user', %s) RETURNING id",
            (thread_id, guild_id, user_id, message),
        )
        message_id = (await cursor.fetchone())[0]
        history = turns(await thread(db, guild_id, user_id, thread_id))
    started = time.monotonic()
    try:
        reply = await ingester.chat(guild_id, f"console-{message_id}", user_id, user_name, history)
    except IngesterError as error:
        await db.execute(
            "UPDATE console_chat_messages SET error = %s, latency_ms = %s WHERE id = %s",
            (str(error)[:500], int((time.monotonic() - started) * 1000), message_id),
        )
        raise
    await db.execute(
        "INSERT INTO console_chat_messages (thread_id, guild_id, user_id, role, content, model, "
        "prompt_tokens, completion_tokens, cost_usd, latency_ms) "
        "VALUES (%s, %s, %s, 'assistant', %s, %s, %s, %s, %s, %s)",
        (
            thread_id,
            guild_id,
            user_id,
            reply["text"],
            reply.get("model"),
            reply.get("prompt_tokens"),
            reply.get("completion_tokens"),
            reply.get("cost_usd"),
            reply.get("latency_ms"),
        ),
    )
    await audit(
        db,
        user_id,
        guild_id,
        "chat",
        {"thread": str(thread_id), "message": message_id, "cost_usd": reply.get("cost_usd")},
    )
