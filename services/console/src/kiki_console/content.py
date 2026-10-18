"""Facts, lingo and prompt overrides: written directly (the ingester reads them
per reply), validated here and by the tables' CHECK constraints."""

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

MAX_FACTS = 100
TITLE_CHARS, BODY_CHARS = 120, 2000
PROMPT_CHARS = 8000
PHRASE_CHARS, MEANING_CHARS = 100, 300
PROMPT_PARTS = ("persona", "guidance")
# Matches FACTS_BUDGET_CHARS in services/ingester/src/agent/prompt.rs.
FACTS_BUDGET_CHARS = 12_000


class Invalid(Exception):
    pass


def normalize(value: str | None) -> str:
    return (value or "").replace("\r\n", "\n").strip()


def text(value: str | None, name: str, limit: int) -> str:
    value = normalize(value)
    if not value:
        raise Invalid(f"{name} can't be empty.")
    if len(value) > limit:
        raise Invalid(f"{name} is limited to {limit} characters.")
    return value


def same_text(a: str | None, b: str | None) -> bool:
    """Equal after the normalization `text` applies."""
    return normalize(a) == normalize(b)


async def audit(
    db: AsyncConnection, actor: int, guild_id: int | None, action: str, detail: dict
) -> None:
    await db.execute(
        "INSERT INTO console_audit (actor_id, guild_id, action, detail) VALUES (%s, %s, %s, %s)",
        (actor, guild_id, action, Jsonb(detail)),
    )


async def facts(db: AsyncConnection, guild_id: int) -> list[tuple]:
    cursor = await db.execute(
        "SELECT id, title, body, enabled, updated_at FROM guild_facts WHERE guild_id = %s "
        "ORDER BY id",
        (guild_id,),
    )
    return await cursor.fetchall()


async def add_fact(db: AsyncConnection, guild_id: int, actor: int, title: str, body: str) -> None:
    title, body = text(title, "Title", TITLE_CHARS), text(body, "Text", BODY_CHARS)
    async with db.transaction():
        # Serializes adds per server so two can't both pass the count check.
        await db.execute("SELECT pg_advisory_xact_lock(hashtextextended('facts', %s))", (guild_id,))
        cursor = await db.execute(
            "SELECT count(*) FROM guild_facts WHERE guild_id = %s", (guild_id,)
        )
        if (await cursor.fetchone())[0] >= MAX_FACTS:
            raise Invalid(f"A server can have at most {MAX_FACTS} facts.")
        cursor = await db.execute(
            "INSERT INTO guild_facts (guild_id, title, body, updated_by) VALUES (%s, %s, %s, %s) "
            "RETURNING id",
            (guild_id, title, body, actor),
        )
        fact_id = (await cursor.fetchone())[0]
        await audit(db, actor, guild_id, "fact.add", {"id": fact_id, "title": title})


async def edit_fact(
    db: AsyncConnection,
    guild_id: int,
    actor: int,
    fact_id: int,
    title: str,
    body: str,
    enabled: bool,
) -> None:
    title, body = text(title, "Title", TITLE_CHARS), text(body, "Text", BODY_CHARS)
    async with db.transaction():
        cursor = await db.execute(
            "UPDATE guild_facts SET title = %s, body = %s, enabled = %s, updated_by = %s, "
            "updated_at = now() WHERE id = %s AND guild_id = %s",
            (title, body, enabled, actor, fact_id, guild_id),
        )
        if cursor.rowcount != 1:
            raise Invalid("That fact doesn't exist.")
        await audit(
            db, actor, guild_id, "fact.edit", {"id": fact_id, "title": title, "enabled": enabled}
        )


async def delete_fact(db: AsyncConnection, guild_id: int, actor: int, fact_id: int) -> None:
    async with db.transaction():
        cursor = await db.execute(
            "DELETE FROM guild_facts WHERE id = %s AND guild_id = %s RETURNING title",
            (fact_id, guild_id),
        )
        row = await cursor.fetchone()
        if not row:
            raise Invalid("That fact doesn't exist.")
        await audit(db, actor, guild_id, "fact.delete", {"id": fact_id, "title": row[0]})


async def lingo(db: AsyncConnection, guild_id: int) -> list[tuple]:
    cursor = await db.execute(
        "SELECT phrase, meaning FROM guild_lingo WHERE guild_id = %s ORDER BY phrase", (guild_id,)
    )
    return await cursor.fetchall()


async def set_lingo(
    db: AsyncConnection, guild_id: int, actor: int, phrase: str, meaning: str
) -> None:
    phrase = text(phrase, "Phrase", PHRASE_CHARS)
    meaning = text(meaning, "Meaning", MEANING_CHARS)
    async with db.transaction():
        await db.execute(
            "INSERT INTO guild_lingo (guild_id, phrase, meaning) VALUES (%s, %s, %s) "
            "ON CONFLICT (guild_id, phrase) DO UPDATE SET meaning = EXCLUDED.meaning",
            (guild_id, phrase, meaning),
        )
        await audit(db, actor, guild_id, "lingo.set", {"phrase": phrase, "meaning": meaning})


async def delete_lingo(db: AsyncConnection, guild_id: int, actor: int, phrase: str) -> None:
    async with db.transaction():
        cursor = await db.execute(
            "DELETE FROM guild_lingo WHERE guild_id = %s AND phrase = %s", (guild_id, phrase)
        )
        if cursor.rowcount != 1:
            raise Invalid("That phrase doesn't exist.")
        await audit(db, actor, guild_id, "lingo.delete", {"phrase": phrase})


async def prompts(db: AsyncConnection, guild_id: int) -> dict[str, tuple]:
    cursor = await db.execute(
        "SELECT part, body, updated_by, updated_at FROM guild_prompts WHERE guild_id = %s",
        (guild_id,),
    )
    return {row[0]: row[1:] for row in await cursor.fetchall()}


async def set_prompt(
    db: AsyncConnection, guild_id: int, actor: int, part: str, body: str | None
) -> None:
    """`body=None` resets the part to the built-in default."""
    if part not in PROMPT_PARTS:
        raise Invalid("Unknown prompt part.")
    if body is not None:
        body = text(body, "Prompt", PROMPT_CHARS)
    async with db.transaction():
        cursor = await db.execute(
            "SELECT body FROM guild_prompts WHERE guild_id = %s AND part = %s FOR UPDATE",
            (guild_id, part),
        )
        old = await cursor.fetchone()
        if body is None:
            await db.execute(
                "DELETE FROM guild_prompts WHERE guild_id = %s AND part = %s", (guild_id, part)
            )
        else:
            await db.execute(
                "INSERT INTO guild_prompts (guild_id, part, body, updated_by) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT (guild_id, part) DO UPDATE SET "
                "body = EXCLUDED.body, updated_by = EXCLUDED.updated_by, updated_at = now()",
                (guild_id, part, body, actor),
            )
        await audit(
            db,
            actor,
            guild_id,
            "prompt.reset" if body is None else "prompt.set",
            {"part": part, "old": old[0] if old else None, "new": body},
        )
