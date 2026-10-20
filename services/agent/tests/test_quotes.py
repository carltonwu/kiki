"""Quote lookups (seen in prod, reply:432, 442 and 251): saved quotes inside quote
bots' posts, bots' cards in history, Kiki's own messages, and what a lookup says
when it finds nothing."""

import asyncio
import json
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from psycopg import AsyncConnection

from kiki_agent_host.chat.cards import card_text
from kiki_agent_host.chat.history import channel_history
from kiki_agent_host.chat.indexing import index_pending
from kiki_agent_host.chat.quotes import PAGE_CHARS, attributed, find_quotes
from kiki_agent_host.chat.scope import Roster, Scope
from kiki_agent_host.chat.search import SNIPPET_CHARS, Deadline, message_context, search
from kiki_agent_host.openrouter.embeddings import Embedder

GUILD, GENERAL, QUOTES, CONVERSATION = 1, 2, 3, 1
ALEX, SAM, KIKI, UB3R, PILZ = 100, 101, 900, 800, 801
NOW = datetime.now(UTC)
SCOPE = Scope(GUILD, GENERAL, CONVERSATION)
ROSTER = Roster({"u1": ALEX, "u2": SAM}, {ALEX: "u1 Alex", SAM: "u2 Sam"})


class FakeEmbeddings:
    """Meaning by topic: words about food or dogs share a dimension; any other
    text is its own dimension, unlike the rest."""

    async def create(self, model, input):
        def vector(text):
            words = set(text.lower().split())
            values = [0.0] * 2048
            values[0] = float(len(words & {"pizza", "food", "lunch", "dinner"}))
            values[1] = float(len(words & {"dog", "dogs", "puppy"}))
            if not any(values):
                values[2 + sum(map(ord, text)) % 500] = 1.0
            return values

        data = [SimpleNamespace(index=i, embedding=vector(t)) for i, t in enumerate(input)]
        return SimpleNamespace(data=data, usage=SimpleNamespace(cost=0))


def embedder():
    return Embedder(SimpleNamespace(embeddings=FakeEmbeddings()))


@pytest.fixture
async def db(database_url):
    async with await AsyncConnection.connect(database_url, autocommit=True) as connection:
        await connection.execute(
            "INSERT INTO channels (id, guild_id, name, type)"
            " VALUES (%s, %s, 'general', 0), (%s, %s, 'quotes', 0)",
            (GENERAL, GUILD, QUOTES, GUILD),
        )
        await connection.execute(
            "INSERT INTO conversations (id, guild_id, channel_id, started_by) VALUES (%s, %s, %s, 5)",
            (CONVERSATION, GUILD, GENERAL),
        )
        yield connection


async def add(db, id, content, *, author_id=ALEX, channel=GENERAL, bot=False, days_ago=0, **raw):
    name = {ALEX: "alex", SAM: "sam", KIKI: "Kiki", UB3R: "UB3R-B0T", PILZ: "PilzBrots"}[author_id]
    raw = {"author": {"id": str(author_id), "username": name, "bot": bot}, "mentions": [], **raw}
    await db.execute(
        "INSERT INTO messages (id, guild_id, channel_id, author_id, author_name, author_bot,"
        " content, created_at, raw) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            id,
            GUILD,
            channel,
            author_id,
            name,
            bot,
            content,
            NOW - timedelta(days=days_ago),
            json.dumps(raw),
        ),
    )


def link(message_id, channel=GENERAL, host="discord.com"):
    return f"https://{host}/channels/{GUILD}/{channel}/{message_id}"


async def ub3r_quote(
    db, id, number, original, author_id, text, *, jump="(Jump)", who=None, url=None
):
    """UB3R-B0T's reply to `.quote N`: an embed, no content."""
    who = who or f"<@{author_id}>"
    url = url or link(original, host="discordapp.com")
    embed = {
        "type": "rich",
        "author": {"name": f"#{number}"},
        "description": f"{text}\n- {who} [{jump}]({url})",
    }
    await add(db, id, "", author_id=UB3R, channel=QUOTES, bot=True, embeds=[embed])


async def pilz_quote(db, id, number, original, author_id, text, *, who=None, url=None, title=None):
    """PilzBrots' quote card: Components V2, no content."""
    who = who or f"Authored by <@{author_id}>"
    card = [
        *([{"type": 10, "content": title}] if title else []),
        {"type": 10, "content": f"**Quote #{number}**"},
        {"type": 10, "content": text},
        {
            "type": 10,
            "content": f"{who} at 9/19/26, 7:01 AM. Nominated by"
            f" <@{SAM}>. [View Original Message]({url or link(original)})\n\nScore: +2",
        },
        {"type": 10, "content": "-# It took the bot 7 milliseconds to render this quote."},
    ]
    components = [{"type": 17, "components": card}]
    await add(
        db, id, "", author_id=PILZ, channel=QUOTES, bot=True, flags=32768, components=components
    )


async def quotes(db, **kwargs):
    return await find_quotes(db, embedder(), SCOPE, roster=ROSTER, **kwargs)


async def test_saved_quotes_are_the_messages_they_saved(db):
    await db.execute("INSERT INTO ignored_authors (user_id, note) VALUES (%s, 'bot')", (PILZ,))
    await add(db, 10, "pizza is a personality trait", days_ago=30)
    await add(db, 11, "i would die for this dog", author_id=SAM, days_ago=20)
    await add(db, 12, "the condolences room is for flirting", days_ago=10)
    await add(db, 13, "deleted after it was quoted", days_ago=5)
    await db.execute("UPDATE messages SET deleted_at = now() WHERE id = 13")
    await add(db, 14, ".quote 1", channel=QUOTES)
    await ub3r_quote(db, 20, 1, 10, ALEX, "pizza is a personality trait")
    await ub3r_quote(db, 21, 1, 10, ALEX, "pizza is a personality trait")  # `.quote 1` again
    await ub3r_quote(db, 22, 2, 11, SAM, "i would die for this dog", jump="(jump)")
    await pilz_quote(db, 23, 417, 12, ALEX, "the condolences room is for flirting")
    await ub3r_quote(db, 24, 3, 13, ALEX, "deleted after it was quoted")
    # Never ingested (before the backfill): the bot's copy, attributed by its mention.
    await ub3r_quote(db, 25, 4, 1000000000000000000, SAM, "old one\nsecond line")
    # A bot embed that links a message but isn't a quote.
    embed = {"type": "rich", "description": f"[(Jump)]({link(10)})"}
    await add(db, 26, "", author_id=UB3R, bot=True, embeds=[embed])

    result = await quotes(db)
    assert result["total"] == 4
    by_id = {int(q["id"]): q for q in result["messages"]}
    assert set(by_id) == {10, 11, 12, 1000000000000000000}
    assert [int(q["id"]) for q in result["messages"]][:3] == [12, 11, 10]  # newest first
    # The original's text, author, channel and link, not the bot's.
    assert by_id[12]["text"] == "the condolences room is for flirting"
    assert by_id[12]["author"] == "u1 Alex"
    assert by_id[12]["url"] == link(12)
    assert by_id[11]["author"] == "u2 Sam"
    assert "saved_copy" not in by_id[10]
    old = by_id[1000000000000000000]
    assert old == old | {"text": "old one\nsecond line", "author": "u2 Sam", "saved_copy": True}
    assert old["channel"] == "#general"

    # Quotes of one person, by who said them.
    assert [int(q["id"]) for q in (await quotes(db, person="u2"))["messages"]] == [
        11,
        1000000000000000000,
    ]
    assert (await quotes(db, person="u1"))["total"] == 2
    assert "error" in await quotes(db, person="nobody")

    # Paging.
    page = await quotes(db, limit=3)
    assert len(page["messages"]) == 3 and page["next_offset"] == 3
    rest = await quotes(db, limit=3, offset=3)
    assert len(rest["messages"]) == 1 and "next_offset" not in rest

    # Ranked by a query: keywords or meaning (only stored messages can be ranked).
    await index_pending(db, embedder(), guild_id=GUILD)
    assert [int(q["id"]) for q in (await quotes(db, query="dog"))["messages"]] == [11]
    assert [int(q["id"]) for q in (await quotes(db, query="food"))["messages"]] == [10]
    empty = await quotes(db, query="spaceship")
    assert empty["total"] == 0 and "search_messages" in empty["note"]


async def test_a_bots_card_reads_as_its_text_in_history_and_context(db):
    await add(db, 10, "pizza is a personality trait", days_ago=1)
    await add(db, 11, ".quote 1", channel=QUOTES)
    await ub3r_quote(db, 12, 1, 10, ALEX, "pizza is a personality trait")
    result = await channel_history(db, SCOPE, channel="#quotes", roster=ROSTER)
    command, post = result["messages"]
    assert command["text"] == ".quote 1"
    assert post["bot"] is True
    assert (
        post["text"]
        == "#1\npizza is a personality trait\n- @u1 Alex [(Jump)]("
        + link(10, host="discordapp.com")
        + ")"
    )
    [*_, around] = (await message_context(db, SCOPE, 12, roster=ROSTER))["messages"]
    assert around["text"].startswith("#1\npizza is a personality trait")


def test_card_text_covers_rich_embeds_and_nested_components_only():
    embeds = [
        {"type": "link", "title": "a link preview", "description": "not a card"},
        {
            "type": "rich",
            "author": {"name": "Daily stats"},
            "title": "Wordle 1,234",
            "description": "3/6",
            "fields": [{"name": "Streak", "value": "12"}, {"name": "", "value": "solo"}],
            "footer": {"text": "today"},
        },
    ]
    section = {
        "type": 9,
        "components": [{"type": 10, "content": "in a section"}],
        "accessory": {"type": 11, "media": {"url": "x"}},
    }
    components = [{"type": 17, "components": [{"type": 10, "content": "**Title**"}, section]}]
    assert card_text(embeds, components) == (
        "Daily stats\nWordle 1,234\n3/6\nStreak: 12\nsolo\ntoday\n**Title**\nin a section"
    )
    assert card_text(None, None) == ""


async def test_kiki_can_search_what_she_said(db):
    # reply:251: asked for her own earlier message, she searched and said she'd
    # never said it. Bots' messages, hers too, are left out of search.
    await add(
        db,
        10,
        "chrissy has real taste, ionnalee and perfume genius",
        author_id=KIKI,
        bot=True,
        days_ago=1,
    )
    await add(db, 11, "kiki says chrissy has taste", author_id=SAM)
    await db.execute(
        "INSERT INTO agent_runs (conversation_id, trigger_message_ids, reply_message_ids, status)"
        " VALUES (%s, '{11}', '{10}', 'done')",
        (CONVERSATION,),
    )
    plain = await search(db, embedder(), SCOPE, "chrissy taste", roster=ROSTER)
    assert [int(m["id"]) for m in plain["messages"]] == [11]
    for you in ("you", "Kiki", "@kiki"):
        own = await search(db, embedder(), SCOPE, "chrissy taste", person=you, roster=ROSTER)
        assert [int(m["id"]) for m in own["messages"]] == [10], you
        assert "words only" in own["note"]


async def test_kiki_has_no_own_messages_before_her_first_reply(db):
    result = await search(db, embedder(), SCOPE, "anything", person="you")
    assert result == {"error": "You haven't posted in this server yet."}


async def test_a_lookup_that_finds_nothing_says_how_to_try_again(db):
    await add(db, 10, "pizza for lunch")
    result = await search(db, embedder(), SCOPE, "spaceship", roster=ROSTER)
    assert result["messages"] == [] and "Nothing matched" in result["note"]
    missing = await search(db, embedder(), SCOPE, "pizza", person="Chris", roster=ROSTER)
    assert "@username" in missing["error"]


async def test_context_of_an_ignored_bots_message_says_it_exists(db):
    # reply:72: a link to PilzBrots' message got "it either got deleted or I can't see it".
    await db.execute("INSERT INTO ignored_authors (user_id, note) VALUES (%s, 'bot')", (PILZ,))
    await add(db, 10, "before", days_ago=1)
    await add(db, 11, "a chatbot's answer", author_id=PILZ, bot=True)
    await add(db, 12, "after")
    result = await message_context(db, SCOPE, 11, roster=ROSTER)
    assert [int(m["id"]) for m in result["messages"]] == [10, 12]
    assert "wasn't deleted" in result["note"]
    assert "note" not in await message_context(db, SCOPE, 10, roster=ROSTER)


async def quoted(db, id):
    row = await (
        await db.execute("SELECT quoted_message_id(raw) FROM messages WHERE id = %s", (id,))
    ).fetchone()
    return row[0]


async def test_malformed_quote_posts_are_stored_and_never_raise(db):
    # Review P1: a link ID past BIGINT made the index expression raise, which
    # rejected the insert (and the ingester's whole history page with it).
    await ub3r_quote(db, 20, 1, None, ALEX, "huge", url=link("99999999999999999999"))
    await ub3r_quote(db, 21, 2, None, ALEX, "just past bigint", url=link("9223372036854775808"))
    await ub3r_quote(db, 22, 3, None, ALEX, "bigint max", url=link("9223372036854775807"))
    huge_guild = f"https://discord.com/channels/99999999999999999999/{GENERAL}/10"
    await ub3r_quote(db, 23, 4, None, ALEX, "huge guild", url=huge_guild)
    assert [await quoted(db, i) for i in (20, 21, 22, 23)] == [
        None,
        None,
        9223372036854775807,
        None,
    ]
    # Shapes Discord never sends, but nothing may make the insert fail.
    for id, raw in enumerate(
        [
            {"embeds": "x"},
            {"embeds": [{"author": "x", "description": 5}]},
            {"embeds": [{"author": {"name": "#1"}, "description": None}]},
            {"components": 5},
            {"components": [{"components": "x"}]},
            {"components": [{"components": [5, None, {"type": 10, "content": 7}]}]},
            {"components": [{"components": [{"type": 10, "content": "**Quote #1**"}]}]},
        ],
        start=30,
    ):
        await add(db, id, "", author_id=UB3R, bot=True, **raw)
        assert await quoted(db, id) is None
    for value in ("[]", "5", "null", '"text"'):
        assert (await (await db.execute(f"SELECT quoted_message_id('{value}'::jsonb)")).fetchone())[
            0
        ] is None


async def test_the_saved_message_is_the_attributions_not_a_link_in_the_quote(db):
    # Review P2: a link in the quote's text was taken for the saved message.
    await add(db, 10, "the real quote", days_ago=2)
    await add(db, 11, "an unrelated message by sam", author_id=SAM, days_ago=1)
    body = f"look at [(Jump)]({link(11)}) lol"
    await ub3r_quote(db, 20, 1, 10, ALEX, body)
    await pilz_quote(db, 21, 2, 10, ALEX, f"see [View Original Message]({link(11)})")
    assert await quoted(db, 20) == 10 and await quoted(db, 21) == 10
    result = await quotes(db)
    assert [(int(q["id"]), q["author"]) for q in result["messages"]] == [(10, "u1 Alex")]
    assert (await quotes(db, person="u2"))["total"] == 0


async def test_a_copy_by_someone_kiki_doesnt_read_is_left_out(db):
    # Review P2: a copy attributed to the ignored PilzBrots came back as saved_copy.
    await db.execute("INSERT INTO ignored_authors (user_id, note) VALUES (%s, 'bot')", (PILZ,))
    # PilzBrots has posted, so its username is known (the people trigger).
    await add(db, 5, "an answer", author_id=PILZ, bot=True)
    await ub3r_quote(db, 20, 1, 1000000000000000001, PILZ, "a chatbot's words")
    await ub3r_quote(db, 21, 2, 1000000000000000002, None, "named, not mentioned", who="PilzBrots")
    await pilz_quote(db, 22, 3, 1000000000000000003, PILZ, "its own card")
    await ub3r_quote(db, 23, 4, 1000000000000000004, SAM, "a person's words")
    result = await quotes(db)
    assert [q["text"] for q in result["messages"]] == ["a person's words"]


async def test_a_copy_must_link_this_guild(db):
    # Review P2: a card linking guild 999's channel 888 was cited as this guild's #888.
    other = "https://discord.com/channels/999/888/1000000000000000001"
    await ub3r_quote(db, 20, 1, None, SAM, "another server's words", url=other)
    await ub3r_quote(db, 21, 2, 1000000000000000002, SAM, "this server's words")
    result = await quotes(db)
    assert [q["text"] for q in result["messages"]] == ["this server's words"]
    assert result["messages"][0]["url"] == link(1000000000000000002)


async def test_a_query_matches_the_bots_copies_too(db):
    # Review P2: a copy was listed without a query but never matched one.
    await ub3r_quote(db, 20, 1, 1000000000000000001, SAM, "pizza for lunch")
    await ub3r_quote(db, 21, 2, 1000000000000000002, SAM, "the rig is sentient")
    result = await quotes(db, query="pizza")
    assert [q["text"] for q in result["messages"]] == ["pizza for lunch"]
    assert result["messages"][0]["saved_copy"] is True


async def test_older_formats_name_who_said_it(db):
    # Seen in prod: "- szajee [(Jump)]", "by 1837… at", and a "Quote of the Day" title.
    await db.execute(
        "INSERT INTO user_names (guild_id, user_id, kind, name, name_norm, first_seen, last_seen)"
        " VALUES (%s, %s, 'username', 'samwise', name_norm('samwise'), now(), now())",
        (GUILD, SAM),
    )
    await ub3r_quote(db, 20, 1, 1000000000000000001, None, "named", who="samwise")
    await pilz_quote(db, 21, 2, 1000000000000000002, None, "bare id", who=f"by {SAM}")
    await pilz_quote(
        db, 22, 3, 1000000000000000003, ALEX, "of the day", title="## :newspaper: Quote of the Day"
    )
    await ub3r_quote(db, 23, 4, 1000000000000000004, None, "nobody known", who="ghost")
    by_text = {q["text"]: q["author"] for q in (await quotes(db))["messages"]}
    assert by_text == {
        "named": "u2 Sam",
        "bare id": "u2 Sam",
        "of the day": "u1 Alex",
        "nobody known": "ghost",
    }
    assert sorted(q["text"] for q in (await quotes(db, person="u2"))["messages"]) == [
        "bare id",
        "named",
    ]


async def test_the_quote_index_finds_a_guilds_posts(db):
    await ub3r_quote(db, 20, 1, 10, ALEX, "pizza for lunch")
    await db.execute("SET enable_seqscan = off")
    plan = await (
        await db.execute(
            "EXPLAIN SELECT id FROM messages WHERE guild_id = %s AND author_bot"
            " AND quoted_message_id(raw) IS NOT NULL",
            (GUILD,),
        )
    ).fetchall()
    assert "messages_quote_posts_idx" in "\n".join(row[0] for row in plan)


async def name(db, user_id, kind, value):
    await db.execute(
        "INSERT INTO user_names (guild_id, user_id, kind, name, name_norm, first_seen, last_seen)"
        " VALUES (%s, %s, %s, %s, name_norm(%s), now(), now()) ON CONFLICT DO NOTHING",
        (GUILD, user_id, kind, value, value),
    )


def test_attributions_name_the_whole_name_by_each_formats_delimiters():
    # Review round 2: "- Pilz - beta [(Jump)]" was read as "Pilz".
    jump = "[(Jump)](https://discordapp.com/channels/1/2/3)"
    view = "[View Original Message](https://discord.com/channels/1/2/3)"
    assert attributed(f"- Pilz - beta {jump}") == "Pilz - beta"
    assert attributed(f"- <@5> [-{jump}") == "<@5>"
    assert attributed(f"- szajee {jump}") == "szajee"
    assert attributed(f"Authored by <@5> at 9/19/26, 7:01 AM. Nominated by <@6>. {view}") == "<@5>"
    assert attributed(f"by Ann at Home at 2/24/21, 6:02 PM - {view}") == "Ann at Home"
    assert attributed(f"by Ann - Lee - {view}") == "Ann - Lee"
    assert attributed("nothing like an attribution") == ""


async def test_a_named_copy_resolves_by_its_whole_name_only(db):
    # An ignored bot is "Pilz - beta"; a person goes by "Pilz". The bot's copy is
    # left out, not given to the person, and the person's own copy is theirs.
    await db.execute("INSERT INTO ignored_authors (user_id, note) VALUES (%s, 'bot')", (PILZ,))
    await add(db, 5, "an answer", author_id=PILZ, bot=True)
    await name(db, PILZ, "global_name", "Pilz - beta")
    await add(db, 6, "hi", author_id=SAM)
    await name(db, SAM, "nick", "Pilz")
    await ub3r_quote(db, 20, 1, 1000000000000000001, None, "a bot's words", who="Pilz - beta")
    await ub3r_quote(db, 21, 2, 1000000000000000002, None, "sam's words", who="Pilz")
    result = await quotes(db)
    assert [(q["text"], q["author"]) for q in result["messages"]] == [("sam's words", "u2 Sam")]


async def test_a_copy_by_a_bot_known_only_from_a_mention_is_left_out(db):
    # Review round 2: bots were known only from messages they wrote; a mention
    # records them in people with bot = true.
    other_bot = 777
    await db.execute(
        "INSERT INTO people (guild_id, user_id, username, bot, seen_at)"
        " VALUES (%s, %s, 'dankmemer', true, now())",
        (GUILD, other_bot),
    )
    await ub3r_quote(db, 20, 1, 1000000000000000001, other_bot, "a bot's words")
    await ub3r_quote(db, 21, 2, 1000000000000000002, SAM, "a person's words")
    assert [q["text"] for q in (await quotes(db))["messages"]] == ["a person's words"]


async def test_quotes_come_whole_not_as_snippets(db):
    # Review round 2: a 1,015-character quote lost its ending (search's 1,000 cap).
    long = "word " * 203  # 1,015 characters
    assert len(long) > SNIPPET_CHARS
    await add(db, 10, long, days_ago=1)
    await ub3r_quote(db, 20, 1, 10, ALEX, long)
    await ub3r_quote(db, 21, 2, 1000000000000000002, SAM, "copy " * 300)
    texts = {int(q["id"]): q["text"] for q in (await quotes(db))["messages"]}
    assert texts == {10: long, 1000000000000000002: "copy " * 300}  # verbatim


async def test_a_page_of_long_quotes_stops_at_its_text_budget(db):
    text = "x" * 3999
    for i in range(15):
        await add(db, 10 + i, f"{i} {text}", days_ago=i + 1)
        await ub3r_quote(db, 100 + i, i, 10 + i, ALEX, "the quote")
    pages, offset = [], 0
    while offset is not None:
        page = await quotes(db, limit=50, offset=offset)
        pages.append(len(page["messages"]))
        assert all(q["text"].endswith(text) for q in page["messages"])  # whole
        offset = page.get("next_offset")
    per_page = PAGE_CHARS // 4001
    assert sum(pages) == 15 and pages[0] == per_page and max(pages) == per_page


class HangingEmbeddings:
    async def create(self, model, input):
        await asyncio.sleep(30)


async def test_finding_quotes_keeps_to_its_deadline(db):
    # #63's deadline: each step gets what's left, inside the tool's 10 s.
    await add(db, 10, "pizza for lunch", days_ago=1)
    await ub3r_quote(db, 20, 1, 10, ALEX, "pizza for lunch")
    late = await find_quotes(db, embedder(), SCOPE, roster=ROSTER, deadline=Deadline(0))
    assert "took too long" in late["error"]
    # A hung embedding ranks by keywords instead, in time.
    hanging = Embedder(SimpleNamespace(embeddings=HangingEmbeddings()))
    started = time.monotonic()
    result = await find_quotes(
        db, hanging, SCOPE, query="pizza", roster=ROSTER, deadline=Deadline(3)
    )
    assert time.monotonic() - started < 3
    assert [int(q["id"]) for q in result["messages"]] == [10]


async def test_quotes_of_someone_with_no_stored_message_of_their_own(db):
    # Review round 3: person="@samwise" said "No one named..." (search's lookup
    # needs a stored message by them) while Sam's copies came back unfiltered.
    mention = {"id": str(SAM), "username": "samwise", "global_name": "Sam"}
    await add(db, 5, f"<@{SAM}> said the best thing", mentions=[mention])
    await ub3r_quote(db, 20, 1, 1000000000000000001, SAM, "sam's words")
    await ub3r_quote(db, 21, 2, 1000000000000000002, ALEX, "alex's words")
    for person in ("@samwise", "Sam", f"<@{SAM}>"):
        result = await quotes(db, person=person)
        assert "messages" in result, (person, result)
        assert [q["text"] for q in result["messages"]] == ["sam's words"], (
            person,
            result,
        )
    assert "error" in await quotes(db, person="@nobody")
