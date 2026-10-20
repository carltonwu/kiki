import json
from datetime import UTC, datetime, timedelta

import pytest
from psycopg import AsyncConnection

from kiki_agent_host.chat import history
from kiki_agent_host.chat.history import channel_history
from kiki_agent_host.chat.scope import Scope

GUILD, OTHER_GUILD, GENERAL, LORE, THREAD, CONVERSATION = 1, 7, 2, 9, 3, 1
START = datetime(2026, 10, 18, 6, 0, tzinfo=UTC)
SCOPE = Scope(GUILD, GENERAL, CONVERSATION)


@pytest.fixture
async def db(database_url):
    async with await AsyncConnection.connect(database_url, autocommit=True) as connection:
        await connection.execute(
            "INSERT INTO channels (id, guild_id, name, type, parent_id) VALUES"
            " (%s, %s, 'general', 0, NULL), (%s, %s, 'lore', 0, NULL),"
            " (%s, %s, 'plans', 11, %s)",
            (GENERAL, GUILD, LORE, GUILD, THREAD, GUILD, GENERAL),
        )
        yield connection


async def add(db, id, content, *, minute, channel=GENERAL, guild=GUILD, bot=False, attachments=()):
    raw = {"author": {"id": "100", "username": "alex"}, "attachments": list(attachments)}
    await db.execute(
        "INSERT INTO messages (id, guild_id, channel_id, author_id, author_name, author_bot,"
        " content, created_at, raw) VALUES (%s, %s, %s, 100, 'alex', %s, %s, %s, %s)",
        (id, guild, channel, bot, content, START + timedelta(minutes=minute), json.dumps(raw)),
    )


async def read(db, **kwargs):
    return await channel_history(db, SCOPE, **kwargs)


def ids(result):
    return [int(m["id"]) for m in result["messages"]]


async def test_reads_a_channel_and_its_threads_in_order(db):
    await add(db, 10, "morning", minute=0)
    await add(db, 11, "in the thread", minute=1, channel=THREAD)
    await add(db, 12, "voice transcript", minute=2, bot=True)
    await add(db, 13, "elsewhere", minute=3, channel=LORE)
    await add(db, 14, "another server", minute=4, guild=OTHER_GUILD, channel=50)
    await add(db, 15, "deleted", minute=5)
    await add(db, 16, "a file", minute=6, attachments=[{"filename": "notes.pdf"}])
    await db.execute("UPDATE messages SET deleted_at = now() WHERE id = 15")

    result = await read(db)
    assert ids(result) == [10, 11, 12, 16]
    assert result["total_in_range"] == 4
    assert "cursor" not in result
    first, thread, bot, file = result["messages"]
    assert first["url"] == f"https://discord.com/channels/{GUILD}/{GENERAL}/10"
    assert first["time"] == "2026-10-18T06:00+00:00"
    assert thread["channel"] == "#plans"
    assert bot["bot"] is True and "bot" not in first
    assert file["files"] == ["notes.pdf"]
    # Another channel by name; one in another server doesn't exist here.
    assert ids(await read(db, channel="#lore")) == [13]
    assert "error" in await read(db, channel="#random")


async def test_bounds_take_times_or_message_ids(db):
    for minute in range(10):
        await add(db, 20 + minute, f"message {minute}", minute=minute)
    await add(db, 40, "another server", minute=5, guild=OTHER_GUILD, channel=50)

    assert ids(await read(db, after="2026-10-18T06:07:00Z")) == [27, 28, 29]
    # An offset is honored; a time without one is UTC.
    assert ids(await read(db, before="2026-10-18T02:02-04:00")) == [20, 21]
    assert ids(await read(db, before="2026-10-18T06:02")) == [20, 21]
    # Message IDs are exclusive.
    assert ids(await read(db, after="25", before="28")) == [26, 27]
    # Blank means unbounded; nonsense and other servers' messages are errors.
    assert len(ids(await read(db, after="", before=" "))) == 10
    with pytest.raises(ValueError):
        await read(db, after="last night")
    assert "error" in await read(db, after="40")


async def test_long_ranges_page_with_a_cursor(db):
    for minute in range(7):
        await add(db, 50 + minute, f"message {minute}", minute=minute)

    # Forward from a start, a page at a time.
    page = await read(db, after="50", limit=3)
    assert ids(page) == [51, 52, 53]
    assert page["total_in_range"] == 6
    page = await read(db, after="50", limit=3, cursor=page["cursor"])
    assert ids(page) == [54, 55, 56]
    assert "cursor" not in page

    # Without a start: the newest, then back.
    page = await read(db, limit=3)
    assert ids(page) == [54, 55, 56]
    page = await read(db, limit=3, cursor=page["cursor"])
    assert ids(page) == [51, 52, 53]
    page = await read(db, limit=3, cursor=page["cursor"])
    assert ids(page) == [50]
    assert "cursor" not in page
    with pytest.raises(ValueError):
        await read(db, cursor="next please")


async def test_pages_stop_before_flooding_the_context(db, monkeypatch):
    monkeypatch.setattr(history, "PAGE_CHARS", 25)
    for minute in range(4):
        await add(db, 60 + minute, "x" * 10, minute=minute)
    page = await read(db, after="2026-10-18T00:00Z")
    assert ids(page) == [60, 61]
    page = await read(db, after="2026-10-18T00:00Z", cursor=page["cursor"])
    assert ids(page) == [62, 63]
    # Reading back keeps the newest.
    assert ids(await read(db)) == [62, 63]


async def test_failure_notes_are_left_out(db):
    """The ingester's "(couldn't answer that one, try again?)" isn't Kiki's words
    (migration 0033): no tool reads it back. By the run that posted it, or by its
    text when its send response was lost, alone or under the redirect header.
    Kiki's other messages are hers."""
    note = "(couldn't answer that one, try again?)"
    header = "-# ↪ <@100> in https://discord.com/channels/1/2/20"
    await add(db, 20, "help kiki where my meme at", minute=0)
    await add(db, 21, note, minute=1, bot=True)  # recorded on its run
    await add(db, 22, note, minute=2, bot=True)  # response lost: no run has it
    await add(db, 23, f"{header}\n{note}", minute=3, bot=True)  # redirected
    await add(db, 24, "found it, one sec", minute=4, bot=True)
    await add(db, 25, f"{note} jk", minute=5, bot=True)  # not the note
    await db.execute(
        "INSERT INTO conversations (id, guild_id, channel_id, started_by) VALUES (1, %s, %s, 100)",
        (GUILD, GENERAL),
    )
    await db.execute(
        "INSERT INTO agent_runs (conversation_id, trigger_message_ids, status, reply_message_ids,"
        " failure_note) VALUES (1, '{20}', 'failed', '{21}', true), (1, '{20}', 'done', '{24}', false)"
    )
    assert ids(await read(db)) == [20, 24, 25]


def test_the_failure_note_filter_is_the_ingesters():
    """Rust's `agent::NOT_A_FAILURE_NOTE`, character for character."""
    import re
    from pathlib import Path

    from kiki_agent_host.chat.scope import NOT_A_FAILURE_NOTE

    rust = (Path(__file__).parents[2] / "ingester/src/agent/mod.rs").read_text()
    [fragment] = re.findall(
        r'pub\(crate\) const NOT_A_FAILURE_NOTE: &str = r"(.*?)";', rust, re.DOTALL
    )
    assert fragment == NOT_A_FAILURE_NOTE
    [note] = re.findall(r'pub\(crate\) const FAILURE_NOTE: &str = "(.*?)";', rust)
    assert note.replace("'", "''") in NOT_A_FAILURE_NOTE
