import json
from datetime import UTC, datetime, timedelta

import pytest
from psycopg import AsyncConnection

from kiki_agent_host import memory, retrieval
from kiki_agent_host.contracts import Person

GUILD, OTHER_GUILD, CHANNEL, CONVERSATION = 1, 7, 2, 1
ALEX, SAM, CHRIS_A, CHRIS_B = 100, 101, 102, 103
NOW = datetime(2026, 10, 18, 12, tzinfo=UTC)
SCOPE = retrieval.Scope(GUILD, CHANNEL, CONVERSATION)
ROSTER = retrieval.Roster.of(
    [
        Person(ref="u1", user_id=str(ALEX), label="Alex", names=["alex"]),
        Person(ref="u2", user_id=str(SAM), label="Sam", names=["sam"]),
    ]
)


def days(n: float) -> timedelta:
    return timedelta(days=n)


# --- temperature ------------------------------------------------------------


def test_facts_harden_with_standing_time_and_opinions_stay_fluid():
    def needed(kind, standing_days):
        start = NOW - days(standing_days)
        return memory.sources_needed(memory.temperature(kind, start, start, NOW, NOW))

    assert [needed("fact", d) for d in (0, 90, 180, 365, 730, 5000)] == [1, 1, 2, 4, 5, 5]
    assert [needed("opinion", d) for d in (0, 30, 365, 5000)] == [1, 1, 1, 1]
    assert memory.temperature("fact", NOW, NOW, NOW, NOW) == 1
    assert memory.temperature("opinion", NOW - days(5000), NOW - days(5000), NOW, NOW) >= 0.6


def test_backfilled_facts_start_hot():
    # Observed two years ago, but recorded today: it hasn't stood in the doc yet.
    old = NOW - days(730)
    assert memory.temperature("fact", old, NOW, NOW, NOW) == 1
    # A year after recording it has cooled, but only by its year in the doc.
    later = NOW + days(365)
    t = memory.temperature("fact", old, NOW, later, later)
    assert memory.sources_needed(t) == 4


def test_temperature_follows_the_evidence_timeline_on_replay():
    # Replaying history: a 2024 fact challenged a week later in 2024 is a week
    # old, however long ago that was and however long it's been recorded.
    observed = NOW - days(730)
    challenge = observed + days(7)
    t = memory.temperature("fact", observed, NOW - days(700), challenge, NOW)
    assert t > 0.9


def test_entry_ids_and_words():
    assert memory.entry_id("e12") == memory.entry_id("12") == 12
    with pytest.raises(ValueError):
        memory.entry_id("twelve")
    assert memory.words("  works   at the\nhospital ") == 4


# --- database ---------------------------------------------------------------


@pytest.fixture
async def db(database_url):
    async with await AsyncConnection.connect(database_url, autocommit=True) as connection:
        await connection.execute(
            "INSERT INTO channels (id, guild_id, name, type) VALUES (%s, %s, 'general', 0)",
            (CHANNEL, GUILD),
        )
        await connection.execute(
            "INSERT INTO conversations (id, guild_id, channel_id, started_by)"
            " VALUES (%s, %s, %s, %s)",
            (CONVERSATION, GUILD, CHANNEL, ALEX),
        )
        for id, author, user_id, nick in (
            (1, "alex", ALEX, None),
            (2, "sam", SAM, None),
            (3, "chris_a", CHRIS_A, "Chris"),
            (4, "chris_b", CHRIS_B, "Chris"),
        ):
            await add(
                connection, id, f"hi from {author}", author=author, author_id=user_id, nick=nick
            )
        yield connection


async def add(
    db,
    id,
    content,
    *,
    at=NOW,
    author="alex",
    author_id=ALEX,
    nick=None,
    guild=GUILD,
    conversation=False,
):
    raw = {
        "author": {"id": str(author_id), "username": author, "global_name": None},
        "member": {"nick": nick},
        "mentions": [],
    }
    await db.execute(
        "INSERT INTO messages (id, guild_id, channel_id, author_id, author_name, author_bot,"
        " content, created_at, raw) VALUES (%s, %s, %s, %s, %s, false, %s, %s, %s)",
        (id, guild, CHANNEL, author_id, author, content, at, json.dumps(raw)),
    )
    if conversation:
        await db.execute(
            "INSERT INTO conversation_messages (message_id, conversation_id, via)"
            " VALUES (%s, %s, 'reply')",
            (id, CONVERSATION),
        )


async def remember(db, subject, text, kind="fact", now=NOW, **kwargs):
    return await memory.remember(db, SCOPE, ROSTER, subject, text, kind, now=now, **kwargs)


async def recall(db, subject, doc=None, now=NOW):
    return await memory.recall(db, SCOPE, ROSTER, subject, doc, now=now)


async def test_subjects_get_a_base_profile_and_entries_render(db):
    assert "Nothing remembered" in (await recall(db, "u1"))["note"]
    await add(db, 10, "i work night shifts at the hospital", conversation=True)
    saved = await remember(db, "u1", "works night shifts at the hospital")
    assert saved["doc"] == "profile" and saved["subject"] == "u1 Alex"
    # A thing, by name; names normalize like people's.
    await remember(db, "Nougie", "Austin's dog", sources=["10"])
    assert (await recall(db, "nougie!"))["subject"] == "Nougie"

    result = await recall(db, "@alex")
    assert result["docs"][0]["doc"] == "profile"
    assert "works night shifts at the hospital" in result["doc"]
    assert "Facts:" in result["doc"] and "changing it takes 1 source" in result["doc"]
    cursor = await db.execute("SELECT count(*) FROM memory_docs WHERE slug = 'profile'")
    assert (await cursor.fetchone())[0] == 2


async def test_ambiguous_names_and_unknown_sources_are_errors(db):
    with pytest.raises(ValueError, match="matches 2 people"):
        await remember(db, "Chris", "likes tape", sources=["3"])
    with pytest.raises(ValueError, match="not messages"):
        await remember(db, "u1", "likes tape", sources=["999"])
    with pytest.raises(ValueError, match="no source"):
        await remember(db, "u1", "likes tape")  # nothing in the conversation to cite
    await add(db, 11, "elsewhere", guild=OTHER_GUILD)
    with pytest.raises(ValueError, match="not messages"):
        await remember(db, "u1", "likes tape", sources=["11"])


async def test_same_text_confirms_and_a_replay_does_not_duplicate(db):
    await add(db, 10, "my birthday is in march", conversation=True)
    first = await remember(db, "u1", "Birthday is in March", job_id="reply:1")
    again = await remember(db, "u1", "birthday is in march.", job_id="reply:1")
    assert again["confirmed"] == first["saved"] and "already saved" in again["note"]
    other = await remember(db, "u1", "birthday is in March", job_id="reply:2", sources=["2"])
    assert other == {"confirmed": first["saved"], "doc": "profile"}
    cursor = await db.execute(
        "SELECT count(*), max(confirmations), max(sources) FROM memory_entries"
    )
    assert await cursor.fetchone() == (1, 1, [2, 10])


async def test_settled_facts_need_more_evidence_than_opinions(db):
    year_ago = NOW - days(365)
    await add(db, 10, "i'm a nurse", at=year_ago)
    fact = await remember(db, "u1", "is a nurse", sources=["10"], now=year_ago)
    opinion = await remember(db, "u1", "loves sushi", "opinion", sources=["10"], now=year_ago)

    # One joke from someone else doesn't overturn a year-old fact...
    await add(db, 20, "alex is an astronaut now lol", author="sam", author_id=SAM)
    result = await remember(db, "u1", "is an astronaut", replaces=fact["saved"], sources=["20"])
    assert "takes 4 source" in result["error"]
    # ...but an opinion changes with one message.
    result = await remember(
        db, "u1", "hates sushi now", "opinion", replaces=opinion["saved"], sources=["20"]
    )
    assert result["replaced"] == opinion["saved"]

    # The subject's own messages count double: two of theirs is four.
    await add(db, 21, "quit nursing, i teach now")
    await add(db, 22, "yeah first week teaching was wild")
    result = await remember(db, "u1", "is a teacher", replaces=fact["saved"], sources=["21", "22"])
    assert result["replaced"] == fact["saved"]
    text = (await recall(db, "u1"))["doc"]
    assert "is a teacher" in text and "nurse" not in text and "hates sushi" in text
    cursor = await db.execute(
        "SELECT superseded_by IS NOT NULL FROM memory_entries WHERE id = %s",
        (memory.entry_id(fact["saved"]),),
    )
    assert (await cursor.fetchone())[0]  # history kept


async def test_backfill_does_not_lock_in_facts(db):
    # History is read today: a two-year-old message is recorded now, so it's hot.
    await add(db, 10, "i live in boston", at=NOW - days(730))
    fact = await remember(db, "u1", "lives in Boston", sources=["10"])
    await add(db, 20, "moved to denver!", at=NOW - days(1))
    result = await remember(db, "u1", "lives in Denver", replaces=fact["saved"], sources=["20"])
    assert result["replaced"] == fact["saved"]


async def test_older_evidence_never_overrides_newer(db):
    await add(db, 10, "i live in denver", at=NOW - days(10))
    fact = await remember(db, "u1", "lives in Denver", sources=["10"])
    # Backfill reaches an older message after the newer one was recorded.
    await add(db, 5, "i live in boston", at=NOW - days(400))
    result = await remember(db, "u1", "lives in Boston", replaces=fact["saved"], sources=["5"])
    assert "Older evidence" in result["error"]
    result = await memory.forget(db, SCOPE, ROSTER, fact["saved"], ["5"], now=NOW)
    assert "Older evidence" in result["error"]


async def test_forget_retires_entries_with_enough_evidence(db):
    await add(db, 10, "i have a cat named bean", conversation=True)
    saved = await remember(db, "u1", "has a cat named Bean")
    result = await memory.forget(db, SCOPE, ROSTER, saved["saved"], now=NOW)
    assert result["forgot"] == saved["saved"]
    assert "Bean" not in (await recall(db, "u1"))["doc"]
    again = await memory.forget(db, SCOPE, ROSTER, saved["saved"], now=NOW)
    assert "already forgotten" in again["error"]
    other_guild = retrieval.Scope(OTHER_GUILD, CHANNEL, CONVERSATION)
    result = await memory.forget(db, other_guild, ROSTER, saved["saved"], now=NOW)
    assert "isn't an entry" in result["error"]


async def fill(db, subject, doc_words, *, doc=None, kind="fact"):
    """Entries of 50 words until the doc holds `doc_words`."""
    ids = []
    for i in range(doc_words // 50):
        text = f"note{i} " + " ".join(["filler"] * 49)
        result = await remember(db, subject, text, kind, doc=doc, sources=["1"])
        ids.append(result["saved"])
    return ids


async def test_full_docs_fail_with_split_instructions_and_split_makes_room(db):
    ids = await fill(db, "u1", 1000)
    band = "plays bass in a punk band called Tape Hiss " + " ".join(["loudly"] * 31)  # 40 words
    result = await remember(db, "u1", band, sources=["1"])
    assert "Not saved" in result["error"] and "limit is 1024" in result["error"]
    assert "split_doc" in result["how_to_fix"] and "at least 16 words" in result["how_to_fix"]
    assert len(result["entries"]) == 20 and result["entries"][0].startswith(ids[0])
    cursor = await db.execute("SELECT count(*) FROM memory_entries")
    assert (await cursor.fetchone())[0] == 20  # nothing saved

    # Can't empty the base profile, can't move other subjects' entries.
    split = await memory.split_doc(
        db, SCOPE, ROSTER, "u1", "notes", "Notes", "Long notes", ids, now=NOW
    )
    assert "base profile" in split["error"]
    other = await remember(db, "u2", "likes tape", sources=["2"])
    split = await memory.split_doc(
        db, SCOPE, ROSTER, "u1", "notes", "Notes", "x", [ids[0], other["saved"]], now=NOW
    )
    assert "not live entries" in split["error"]

    split = await memory.split_doc(
        db,
        SCOPE,
        ROSTER,
        "u1",
        "music",
        "Alex's music",
        "Bands Alex plays in and music they like",
        ids[:5],
        now=NOW,
    )
    assert split["created"] and split["words"] == {"profile": 750, "music": 250}
    result = await remember(db, "u1", band, sources=["1"])
    assert result["doc"] in ("profile", "music") and "saved" in result

    listed = (await recall(db, "u1"))["docs"]
    assert [d["doc"] for d in listed] == ["profile", "music"]
    assert listed[1]["summary"] == "Bands Alex plays in and music they like"
    assert "Alex's music" in (await recall(db, "u1", "music"))["doc"]
    assert "no doc" in (await recall(db, "u1", "work"))["error"]


async def test_placement_prefers_the_matching_sub_doc(db):
    await remember(db, "u1", "is a nurse", sources=["1"])
    await memory.split_doc(
        db,
        SCOPE,
        ROSTER,
        "u1",
        "music",
        "Music",
        "Bands, concerts and songs Alex likes",
        [],
        now=NOW,
    )
    placed = await remember(db, "u1", "went to three concerts this summer", sources=["1"])
    assert placed["doc"] == "music"
    placed = await remember(db, "u1", "grew up in Ohio", sources=["1"])
    assert placed["doc"] == "profile"
    named = await remember(db, "u1", "sings in the shower", doc="music", sources=["1"])
    assert named["doc"] == "music"
    missing = await remember(db, "u1", "x", doc="work", sources=["1"])
    assert "split_doc" in missing["error"]
    similar = await remember(db, "u1", "was a nurse in Ohio", sources=["1"])
    assert any("is a nurse" in s for s in similar["similar"])


async def test_search_memory_finds_entries_across_subjects_and_docs(db):
    await remember(db, "u1", "works night shifts at the hospital", sources=["1"])
    await remember(db, "Nougie", "a corgi who steals socks", sources=["1"])
    await memory.split_doc(
        db, SCOPE, ROSTER, "u2", "games", "Sam's games", "Video games Sam plays", [], now=NOW
    )
    await remember(db, "u2", "mains Zelda in Smash", doc="games", sources=["2"])

    found = await memory.search(db, SCOPE, ROSTER, "hospital shifts")
    assert [(e["subject"], e["doc"]) for e in found["entries"]] == [("u1 Alex", "profile")]
    found = await memory.search(db, SCOPE, ROSTER, "video games")
    assert found["entries"][0]["doc"] == "games"
    found = await memory.search(db, SCOPE, ROSTER, "nougie")
    assert found["entries"][0]["subject"] == "Nougie"
    found = await memory.search(db, SCOPE, ROSTER, "hospital", subject="u2")
    assert found["entries"] == []
    other_guild = retrieval.Scope(OTHER_GUILD, CHANNEL, CONVERSATION)
    assert (await memory.search(db, other_guild, ROSTER, "hospital"))["entries"] == []


async def test_entries_are_single_claims(db):
    with pytest.raises(ValueError, match="at most 60 words"):
        await remember(db, "u1", " ".join(["word"] * 61), sources=["1"])
    with pytest.raises(ValueError, match="kind"):
        await remember(db, "u1", "x", kind="rumor", sources=["1"])


async def test_unknown_usernames_are_errors_not_things(db):
    with pytest.raises(ValueError, match="no one here goes by"):
        await remember(db, "@nobody", "likes tape", sources=["1"])
    cursor = await db.execute("SELECT count(*) FROM memory_subjects")
    assert (await cursor.fetchone())[0] == 0


async def test_people_can_have_settled_facts_about_them_forgotten(db):
    year_ago = NOW - days(365)
    await add(db, 10, "i'm a nurse", at=year_ago)
    fact = await remember(db, "u1", "is a nurse", sources=["10"], now=year_ago)

    # Someone else asking isn't enough for a settled fact...
    await add(db, 20, "kiki forget that alex is a nurse", author="sam", author_id=SAM)
    await add(db, 21, "kiki forget i'm a nurse", conversation=True)
    await add(db, 22, "kiki forget alex is a nurse", author="sam", author_id=SAM, conversation=True)
    result = await memory.forget(db, SCOPE, ROSTER, fact["saved"], ["20"], now=NOW)
    assert "takes 4 source" in result["error"]
    # ...nor, uncited, is Alex's message once someone else has spoken since.
    result = await memory.forget(db, SCOPE, ROSTER, fact["saved"], now=NOW)
    assert "takes 4 source" in result["error"]
    # Alex asking in this conversation is.
    result = await memory.forget(db, SCOPE, ROSTER, fact["saved"], ["21"], now=NOW)
    assert result["forgot"] == fact["saved"]


async def test_the_subjects_latest_request_forgets_without_citing(db):
    year_ago = NOW - days(365)
    await add(db, 10, "i'm a nurse", at=year_ago)
    fact = await remember(db, "u1", "is a nurse", sources=["10"], now=year_ago)
    await add(db, 21, "kiki please forget that i'm a nurse", conversation=True)
    result = await memory.forget(db, SCOPE, ROSTER, fact["saved"], now=NOW)
    assert result["forgot"] == fact["saved"]


async def test_a_replayed_replacement_returns_what_it_saved(db):
    await add(db, 10, "i live in denver", conversation=True)
    fact = await remember(db, "u1", "lives in Denver", job_id="reply:1")
    await add(db, 11, "moved to austin", conversation=True)
    first = await remember(db, "u1", "lives in Austin", replaces=fact["saved"], job_id="reply:2")
    again = await remember(db, "u1", "lives in Austin", replaces=fact["saved"], job_id="reply:2")
    assert again["saved"] == first["saved"] and "already saved" in again["note"]
    other = await remember(db, "u1", "lives in Austin", replaces=fact["saved"], job_id="reply:3")
    assert "already replaced" in other["error"]
    cursor = await db.execute("SELECT count(*) FROM memory_entries")
    assert (await cursor.fetchone())[0] == 2


async def test_placement_skips_full_docs_but_never_falls_back_to_unrelated_ones(db):
    await memory.split_doc(
        db, SCOPE, ROSTER, "u1", "music", "Music", "Bands, concerts and songs", [], now=NOW
    )
    await fill(db, "u1", 1000, doc="profile")
    # The profile is full: a band fact still finds the music doc...
    placed = await remember(
        db, "u1", "plays in a band called Tape Hiss " + "x " * 20, sources=["1"]
    )
    assert placed["doc"] == "music"
    # ...but a work fact isn't dumped there: it fails with the profile's split steps.
    result = await remember(db, "u1", "works at the hospital " + "y " * 30, sources=["1"])
    assert "Not saved" in result["error"] and "'profile'" in result["error"]
    assert [d["doc"] for d in result["other_docs"]] == ["music"]
