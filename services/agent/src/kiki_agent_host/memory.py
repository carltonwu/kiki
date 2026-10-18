"""Subject memory: documents Kiki keeps about people and things (docs/subject-memory-hld.md).

A subject has a base profile and any number of sub docs; a doc is a list of
entries (one fact or opinion each), rendered as Markdown on read. Changing an
entry takes enough cited evidence for its temperature: facts harden as they
stand unchallenged, opinions stay fluid. A doc holds at most `MAX_DOC_WORDS`;
a write past that fails with instructions to split.
"""

import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from .retrieval import HEARD, Roster, Scope, resolve_person, speakers

MAX_DOC_WORDS = 1024
MAX_ENTRY_WORDS = 60
MAX_SOURCES = 10
BASE = "profile"
SLUG = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
ENTRY_ID = re.compile(r"^e?([0-9]+)$")


@dataclass(frozen=True)
class Cooling:
    floor: float
    half_life_days: float


# Facts harden over months; opinions never drop below a level one message moves.
COOLING = {"fact": Cooling(0.05, 90), "opinion": Cooling(0.60, 30)}


def temperature(
    kind: str,
    first_observed_at: datetime,
    recorded_at: datetime,
    evidence_time: datetime,
    now: datetime,
) -> float:
    """How readily an entry changes, in (0, 1].

    It has stood unchallenged for the shorter of: its age in the evidence
    timeline (so replaying history in order gives live temperatures), and its
    time in the doc (so a backfill of old messages starts hot).
    """
    cooling = COOLING[kind]
    standing = min(evidence_time - first_observed_at, now - recorded_at)
    days = max(0.0, standing.total_seconds() / 86400)
    return cooling.floor + (1 - cooling.floor) * 2 ** (-days / cooling.half_life_days)


def sources_needed(t: float) -> int:
    """Evidence n changes an entry when 1 − 2^−n ≥ 1 − T, i.e. n ≥ log₂(1/T)."""
    return max(1, math.ceil(math.log2(1 / t) - 1e-9))


def words(text: str) -> int:
    return len(text.split())


def norm(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", "", text.lower()).split())


def entry_id(value: str) -> int:
    match = ENTRY_ID.match(str(value).strip())
    if not match:
        raise ValueError(f"{value!r} is not an entry ID like e12")
    return int(match[1])


# --- subjects ---------------------------------------------------------------


@dataclass(frozen=True)
class Subject:
    id: int | None  # None: known how to address, but nothing remembered yet
    kind: str
    user_id: int | None
    name: str | None
    label: str


async def resolve_subject(
    connection: AsyncConnection, scope: Scope, roster: Roster, subject: str, *, create: bool
) -> Subject:
    """A person if `subject` names one (several is an error), otherwise a thing."""
    subject = subject.strip()
    if not subject:
        raise ValueError("subject must not be empty")
    users = await resolve_person(connection, scope, roster, subject)
    if len(users) > 1:
        names = await speakers(connection, scope, roster, set(users))
        raise ValueError(
            f"{subject!r} matches {len(users)} people: "
            + "; ".join(names.get(u, str(u)) for u in users)
            + ". Use their ref (like u2) or @username."
        )
    if users:
        [user_id] = users
        label = (await speakers(connection, scope, roster, {user_id})).get(user_id, str(user_id))
        if create:
            await connection.execute(
                "INSERT INTO memory_subjects (guild_id, kind, user_id) VALUES (%s, 'person', %s)"
                " ON CONFLICT (guild_id, user_id) WHERE kind = 'person' DO NOTHING",
                (scope.guild_id, user_id),
            )
        cursor = await connection.execute(
            "SELECT id FROM memory_subjects WHERE guild_id = %s AND kind = 'person'"
            " AND user_id = %s",
            (scope.guild_id, user_id),
        )
        row = await cursor.fetchone()
        return Subject(row and row[0], "person", user_id, None, label)

    if subject.startswith("@"):
        raise ValueError(
            f"no one here goes by {subject!r}. Use a ref like u2, or leave out @ for a thing."
        )
    name = subject.strip()
    if len(name) > 80:
        raise ValueError("subject names are at most 80 characters")
    if create:
        await connection.execute(
            "INSERT INTO memory_subjects (guild_id, kind, name, name_norm)"
            " SELECT %(guild)s, 'thing', %(name)s, name_norm(%(name)s)"
            " WHERE name_norm(%(name)s) <> ''"
            " ON CONFLICT (guild_id, name_norm) WHERE kind = 'thing' DO NOTHING",
            {"guild": scope.guild_id, "name": name},
        )
    cursor = await connection.execute(
        "SELECT id, name FROM memory_subjects WHERE guild_id = %s AND kind = 'thing'"
        " AND name_norm = name_norm(%s)",
        (scope.guild_id, name),
    )
    row = await cursor.fetchone()
    if row is None and create:
        raise ValueError(f"{subject!r} isn't usable as a subject name")
    return Subject(row and row[0], "thing", None, row[1] if row else name, row[1] if row else name)


async def lock(connection: AsyncConnection, subject: Subject) -> None:
    """Serializes writes to one subject: size checks and placement see a stable doc set."""
    await connection.execute(
        "SELECT 1 FROM memory_subjects WHERE id = %s FOR UPDATE", (subject.id,)
    )


# --- reading ----------------------------------------------------------------


async def load(connection: AsyncConnection, subject_id: int) -> tuple[list[dict], list[dict]]:
    """The subject's docs (base profile first) and live entries."""
    cursor = connection.cursor(row_factory=dict_row)
    await cursor.execute(
        "SELECT id, slug, title, summary, parent_id FROM memory_docs WHERE subject_id = %s"
        " ORDER BY slug <> 'profile', created_at, id",
        (subject_id,),
    )
    docs = await cursor.fetchall()
    await cursor.execute(
        "SELECT e.* FROM memory_entries e JOIN memory_docs d ON d.id = e.doc_id"
        " WHERE d.subject_id = %s AND e.superseded_at IS NULL ORDER BY e.id",
        (subject_id,),
    )
    entries = await cursor.fetchall()
    for doc in docs:
        doc["entries"] = [e for e in entries if e["doc_id"] == doc["id"]]
        doc["words"] = sum(words(e["text"]) for e in doc["entries"])
    return docs, entries


def index(docs: list[dict]) -> list[dict]:
    return [
        {
            "doc": d["slug"],
            "title": d["title"],
            "summary": d["summary"],
            "words": d["words"],
            "room": MAX_DOC_WORDS - d["words"],
        }
        for d in docs
    ]


def render(subject: Subject, doc: dict, now: datetime) -> str:
    lines = [
        f"# {subject.label}: {doc['title']} ({doc['slug']}, {doc['words']}/{MAX_DOC_WORDS} words)",
        doc["summary"],
    ]
    for kind, heading in (("fact", "Facts"), ("opinion", "Opinions")):
        entries = [e for e in doc["entries"] if e["kind"] == kind]
        if entries:
            lines += ["", f"{heading}:"]
            for e in entries:
                t = temperature(kind, e["first_observed_at"], e["recorded_at"], now, now)
                confirmed = f", confirmed {e['confirmations']}x" if e["confirmations"] else ""
                lines.append(
                    f"- e{e['id']}: {e['text']} (seen {e['first_observed_at']:%Y-%m-%d}"
                    f"{confirmed}; changing it takes {sources_needed(t)} source(s))"
                )
    if not doc["entries"]:
        lines += ["", "Nothing recorded yet."]
    return "\n".join(lines)


async def recall(
    connection: AsyncConnection,
    scope: Scope,
    roster: Roster,
    subject: str,
    doc: str | None = None,
    now: datetime | None = None,
) -> dict:
    now = now or datetime.now(UTC)
    who = await resolve_subject(connection, scope, roster, subject, create=False)
    if who.id is None:
        return {"subject": who.label, "note": f"Nothing remembered about {who.label} yet."}
    docs, _ = await load(connection, who.id)
    slug = (doc or BASE).strip().lower()
    chosen = next((d for d in docs if d["slug"] == slug), None)
    if chosen is None:
        return {
            "error": f"{who.label} has no doc {slug!r}.",
            "docs": index(docs),
        }
    return {
        "subject": who.label,
        "doc": render(who, chosen, now),
        "docs": index(docs),
    }


ORQUERY = "replace(plainto_tsquery('english', %(query)s)::text, ' & ', ' | ')::tsquery"

SEARCH = f"""
WITH q AS (SELECT {ORQUERY} AS query)
SELECT e.id, e.kind, e.text, e.first_observed_at, d.slug, d.title,
       s.kind AS subject_kind, s.user_id, s.name,
       ts_rank_cd(to_tsvector('english', e.text), q.query)
       + 0.5 * ts_rank_cd(to_tsvector('english', d.title || ' ' || d.summary), q.query)
       + CASE WHEN s.name_norm = name_norm(%(query)s) THEN 1 ELSE 0 END AS rank
FROM memory_entries e JOIN memory_docs d ON d.id = e.doc_id
JOIN memory_subjects s ON s.id = d.subject_id, q
WHERE s.guild_id = %(guild)s AND e.superseded_at IS NULL
  AND (%(subject)s::bigint IS NULL OR s.id = %(subject)s)
  AND (to_tsvector('english', e.text) @@ q.query
       OR to_tsvector('english', d.title || ' ' || d.summary) @@ q.query
       OR s.name_norm = name_norm(%(query)s))
ORDER BY rank DESC, e.id DESC
LIMIT %(limit)s
"""


async def search(
    connection: AsyncConnection,
    scope: Scope,
    roster: Roster,
    query: str,
    subject: str | None = None,
    limit: int = 10,
) -> dict:
    if not query.strip():
        raise ValueError("query must not be empty")
    subject_id = None
    if subject and subject.strip():
        who = await resolve_subject(connection, scope, roster, subject, create=False)
        if who.id is None:
            return {"entries": [], "note": f"Nothing remembered about {who.label} yet."}
        subject_id = who.id
    cursor = connection.cursor(row_factory=dict_row)
    await cursor.execute(
        SEARCH,
        {
            "query": query,
            "guild": scope.guild_id,
            "subject": subject_id,
            "limit": max(1, min(limit, 10)),
        },
    )
    rows = await cursor.fetchall()
    people = await speakers(
        connection, scope, roster, {r["user_id"] for r in rows if r["user_id"] is not None}
    )
    return {
        "entries": [
            {
                "id": f"e{r['id']}",
                "subject": people.get(r["user_id"], str(r["user_id"]))
                if r["subject_kind"] == "person"
                else r["name"],
                "doc": r["slug"],
                "kind": r["kind"],
                "text": r["text"],
                "seen": f"{r['first_observed_at']:%Y-%m-%d}",
            }
            for r in rows
        ]
    }


# --- evidence ---------------------------------------------------------------


@dataclass(frozen=True)
class Evidence:
    ids: list[int]
    strength: int  # distinct messages, the subject's own counting twice
    first: datetime
    last: datetime


CITED = f"""
SELECT m.id, m.author_id, m.created_at FROM messages m
WHERE m.id = ANY(%(ids)s) AND m.guild_id = %(guild)s
  AND m.deleted_at IS NULL AND NOT m.author_bot AND {HEARD}
"""

# Without citations: the subject's latest message in this conversation, else its
# latest human one. The prompt doesn't show the conversation's message IDs.
CONVERSATION = f"""
SELECT m.id, m.author_id, m.created_at FROM conversation_messages cm
JOIN messages m ON m.id = cm.message_id
WHERE cm.conversation_id = %(conversation)s AND m.guild_id = %(guild)s
  AND m.deleted_at IS NULL AND NOT m.author_bot AND {HEARD}
ORDER BY m.author_id = %(user)s DESC NULLS LAST, m.id DESC LIMIT 1
"""


async def evidence(
    connection: AsyncConnection, scope: Scope, subject: Subject, sources: list[str] | None
) -> Evidence:
    if sources:
        if len(sources) > MAX_SOURCES:
            raise ValueError(f"cite at most {MAX_SOURCES} source messages")
        ids = []
        for s in sources:
            if not str(s).strip().isdigit():
                raise ValueError(f"{s!r} isn't a message ID; cite `id`s from search results")
            ids.append(int(str(s).strip()))
        cursor = await connection.execute(CITED, {"ids": ids, "guild": scope.guild_id})
        rows = await cursor.fetchall()
        missing = set(ids) - {r[0] for r in rows}
        if missing:
            raise ValueError(
                "not messages from people in this server: " + ", ".join(map(str, sorted(missing)))
            )
    else:
        cursor = await connection.execute(
            CONVERSATION,
            {
                "conversation": scope.conversation_id,
                "guild": scope.guild_id,
                "user": subject.user_id,
            },
        )
        rows = await cursor.fetchall()
        if not rows:
            raise ValueError("no source: cite message IDs from search results in `sources`")
    return Evidence(
        ids=sorted(r[0] for r in rows),
        strength=sum(2 if r[1] == subject.user_id else 1 for r in rows),
        first=min(r[2] for r in rows),
        last=max(r[2] for r in rows),
    )


def resists(
    entry: dict, proof: Evidence, now: datetime, *, own_request: bool = False
) -> str | None:
    """Why `proof` can't change `entry`, or None if it can.

    A person's own request (to forget something about them) skips the temperature
    check, but not the stale-evidence one.
    """
    seen = entry["last_observed_at"]
    if proof.last < seen:
        return (
            f"e{entry['id']} was last observed {seen:%Y-%m-%d}, after your newest source"
            f" ({proof.last:%Y-%m-%d}). Older evidence doesn't override newer knowledge;"
            " if the earlier state matters, remember it as a separate dated entry."
        )
    if own_request:
        return None
    t = temperature(
        entry["kind"], entry["first_observed_at"], entry["recorded_at"], proof.last, now
    )
    needed = sources_needed(t)
    if proof.strength < needed:
        return (
            f"e{entry['id']} is a settled {entry['kind']} (temperature {t:.2f}): changing it"
            f" takes {needed} source message(s) (the subject's own count double), you gave"
            f" {proof.strength}. Search for more evidence and cite it in `sources`, or leave"
            " it. A joke or a single offhand remark shouldn't overturn it."
        )
    return None


# --- writing ----------------------------------------------------------------


def split_instructions(
    subject: Subject, doc: dict, docs: list[dict], adding: int, overflow: int
) -> dict:
    entries = [
        f"e{e['id']} ({words(e['text'])}w, {e['kind']}): {e['text']}" for e in doc["entries"]
    ]
    return {
        "error": (
            f"Not saved: doc {doc['slug']!r} of {subject.label} has {doc['words']} words, adding"
            f" {adding} would make {doc['words'] + adding}; the limit is {MAX_DOC_WORDS}"
            f" per doc."
        ),
        "how_to_fix": (
            "1. If one of `other_docs` is the right home and has room, retry `remember` with"
            " doc set to it.\n"
            "2. Otherwise split this doc: pick a theme that groups several entries below"
            " (work, music, relationships, history, a running joke...) moving at least"
            f" {overflow} words out.\n"
            f"3. Call split_doc(subject=<the same subject>, from_doc={doc['slug']!r},"
            " new_doc=<short-lowercase-slug>, title=<short title>,"
            " summary=<one line saying what belongs in it; you'll find the doc by this later>,"
            " move_entries=[<entry ids>]).\n"
            "4. Retry `remember` with doc=<new slug>, or without doc to place it automatically.\n"
            f"The base profile ({BASE}) keeps who the subject is: their most identifying"
            " facts. Move detail out of it, not the essentials."
        ),
        "entries": entries,
        "other_docs": [d for d in index(docs) if d["doc"] != doc["slug"]],
    }


PLACE = f"""
WITH q AS (SELECT {ORQUERY} AS query)
SELECT d.id, ts_rank_cd(to_tsvector('english', d.title || ' ' || d.summary || ' '
                                    || coalesce(string_agg(e.text, ' '), '')), q.query, 1) AS score
FROM memory_docs d
LEFT JOIN memory_entries e ON e.doc_id = d.id AND e.superseded_at IS NULL, q
WHERE d.subject_id = %(subject)s
GROUP BY d.id, q.query
"""

SIMILAR = f"""
WITH q AS (SELECT {ORQUERY} AS query)
SELECT e.id, e.kind, e.text, d.slug FROM memory_entries e
JOIN memory_docs d ON d.id = e.doc_id, q
WHERE d.subject_id = %(subject)s AND e.superseded_at IS NULL AND e.id <> %(saved)s
  AND to_tsvector('english', e.text) @@ q.query
ORDER BY ts_rank_cd(to_tsvector('english', e.text), q.query) DESC, e.id DESC
LIMIT 3
"""


async def place(connection: AsyncConnection, subject: Subject, text: str, docs: list[dict]) -> dict:
    """The doc whose title, summary and entries best match `text` and that has room
    for it; the profile on ties. If none that matches at all has room, the best
    match (whose split instructions the caller returns)."""
    cursor = await connection.execute(PLACE, {"query": text, "subject": subject.id})
    scores = dict(await cursor.fetchall())
    # docs is profile-first, and sorted() keeps equal scores in order.
    ranked = sorted(docs, key=lambda d: -(scores.get(d["id"]) or 0))
    best = ranked[0]
    fits = [
        d
        for d in ranked
        if (d is best or (scores.get(d["id"]) or 0) > 0)
        and d["words"] + words(text) <= MAX_DOC_WORDS
    ]
    return fits[0] if fits else best


async def remember(
    connection: AsyncConnection,
    scope: Scope,
    roster: Roster,
    subject: str,
    text: str,
    kind: str,
    doc: str | None = None,
    replaces: str | None = None,
    sources: list[str] | None = None,
    job_id: str = "",
    now: datetime | None = None,
) -> dict:
    now = now or datetime.now(UTC)
    text = " ".join(text.split())
    if not text:
        raise ValueError("text must not be empty")
    if kind not in COOLING:
        raise ValueError("kind must be 'fact' or 'opinion'")
    if words(text) > MAX_ENTRY_WORDS:
        raise ValueError(
            f"an entry is one claim of at most {MAX_ENTRY_WORDS} words; this has {words(text)}."
            " Split it into separate remember calls."
        )
    old_id = entry_id(replaces) if replaces else None
    async with connection.transaction():
        who = await resolve_subject(connection, scope, roster, subject, create=True)
        await lock(connection, who)
        proof = await evidence(connection, scope, who, sources)
        docs, entries = await load(connection, who.id)
        by_id = {e["id"]: e for e in entries}

        old = None
        if old_id is not None:
            old = by_id.get(old_id)
            if old is None:
                if replayed := await replayed_replacement(connection, old_id, text, job_id):
                    replayed["subject"] = who.label
                    return replayed
                return {"error": await missing_entry(connection, who, old_id)}
        else:
            same = [e for e in entries if norm(e["text"]) == norm(text)]
            old = same[0] if same else None

        if old is not None and norm(old["text"]) == norm(text) and old["kind"] == kind:
            return await confirm(connection, old, proof, job_id, docs)

        if old is not None:
            if reason := resists(old, proof, now):
                return {"error": "Not changed: " + reason}
            target = next(d for d in docs if d["id"] == old["doc_id"])
            if doc and doc.strip().lower() != target["slug"]:
                return {
                    "error": f"e{old['id']} is in {target['slug']!r}; replace it there"
                    " (leave out doc), then move it with split_doc if needed."
                }
            freed = words(old["text"])
        else:
            if doc:
                slug = doc.strip().lower()
                target = next((d for d in docs if d["slug"] == slug), None)
                if target is None:
                    return {
                        "error": f"{who.label} has no doc {slug!r}. Use one of `docs`, or create"
                        " it first with split_doc (move_entries can be empty).",
                        "docs": index(docs),
                    }
            else:
                target = await place(connection, who, text, docs)
            freed = 0

        adding = words(text) - freed
        if target["words"] + adding > MAX_DOC_WORDS:
            overflow = target["words"] + adding - MAX_DOC_WORDS
            return split_instructions(who, target, docs, adding, overflow)

        cursor = await connection.execute(
            "INSERT INTO memory_entries (doc_id, kind, text, sources, first_observed_at,"
            " last_observed_at, recorded_at, job_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
            " RETURNING id",
            (target["id"], kind, text, proof.ids, proof.first, proof.last, now, job_id or None),
        )
        [saved] = await cursor.fetchone()
        if old is not None:
            await connection.execute(
                "UPDATE memory_entries SET superseded_at = %s, superseded_by = %s WHERE id = %s",
                (now, saved, old["id"]),
            )
        await connection.execute(
            "UPDATE memory_docs SET updated_at = %s WHERE id = %s", (now, target["id"])
        )
        cursor = await connection.execute(
            SIMILAR, {"query": text, "subject": who.id, "saved": saved}
        )
        similar = await cursor.fetchall()

    result = {
        "saved": f"e{saved}",
        "subject": who.label,
        "doc": target["slug"],
        "doc_words": f"{target['words'] + adding}/{MAX_DOC_WORDS}",
    }
    if old is not None:
        result["replaced"] = f"e{old['id']}"
    elif similar:
        result["similar"] = [f"e{i} ({k}, {s}): {t}" for i, k, t, s in similar]
        result["hint"] = (
            "If one of `similar` is the same claim, or what you saved contradicts it, call"
            " forget on the outdated one (or next time use `replaces`)."
        )
    return result


async def confirm(
    connection: AsyncConnection, entry: dict, proof: Evidence, job_id: str, docs: list[dict]
) -> dict:
    slug = next(d["slug"] for d in docs if d["id"] == entry["doc_id"])
    if job_id and entry["job_id"] == job_id:
        return {"confirmed": f"e{entry['id']}", "doc": slug, "note": "already saved by this reply"}
    await connection.execute(
        "UPDATE memory_entries SET sources = ARRAY(SELECT DISTINCT unnest(sources || %s::bigint[])"
        " ORDER BY 1), last_observed_at = greatest(last_observed_at, %s),"
        " confirmations = confirmations + 1, job_id = %s WHERE id = %s",
        (proof.ids, proof.last, job_id or None, entry["id"]),
    )
    return {"confirmed": f"e{entry['id']}", "doc": slug}


async def replayed_replacement(
    connection: AsyncConnection, old_id: int, text: str, job_id: str
) -> dict | None:
    """A retried job replacing an entry it already replaced: the replacement it saved."""
    if not job_id:
        return None
    cursor = await connection.execute(
        "SELECT n.id, n.text, d.slug FROM memory_entries o"
        " JOIN memory_entries n ON n.id = o.superseded_by JOIN memory_docs d ON d.id = n.doc_id"
        " WHERE o.id = %s AND n.job_id = %s AND n.superseded_at IS NULL",
        (old_id, job_id),
    )
    row = await cursor.fetchone()
    if row is None or norm(row[1]) != norm(text):
        return None
    return {
        "saved": f"e{row[0]}",
        "doc": row[2],
        "replaced": f"e{old_id}",
        "note": "already saved by this reply",
    }


async def missing_entry(connection: AsyncConnection, subject: Subject, id: int) -> str:
    cursor = await connection.execute(
        "SELECT e.superseded_by, d.subject_id FROM memory_entries e"
        " JOIN memory_docs d ON d.id = e.doc_id WHERE e.id = %s",
        (id,),
    )
    row = await cursor.fetchone()
    if row is None or row[1] != subject.id:
        return f"e{id} isn't an entry of {subject.label}; recall them to see their entries."
    if row[0]:
        return f"e{id} was already replaced by e{row[0]}."
    return f"e{id} was already forgotten."


# The subject asking, in this conversation: a cited message of theirs in it, or
# (uncited) their message being its latest human one.
OWN_REQUEST = f"""
SELECT EXISTS (
    SELECT 1 FROM conversation_messages cm JOIN messages m ON m.id = cm.message_id
    WHERE cm.conversation_id = %(conversation)s AND m.id = ANY(%(ids)s)
      AND m.author_id = %(user)s
      AND (%(cited)s OR m.id = (
          SELECT max(m2.id) FROM conversation_messages c2 JOIN messages m2 ON m2.id = c2.message_id
          WHERE c2.conversation_id = %(conversation)s AND m2.deleted_at IS NULL
            AND NOT m2.author_bot AND {HEARD.replace("m.", "m2.")}))
)
"""


async def asked_by_subject(
    connection: AsyncConnection, scope: Scope, subject: Subject, proof: Evidence, *, cited: bool
) -> bool:
    if subject.user_id is None:
        return False
    cursor = await connection.execute(
        OWN_REQUEST,
        {
            "conversation": scope.conversation_id,
            "ids": proof.ids,
            "user": subject.user_id,
            "cited": cited,
        },
    )
    return (await cursor.fetchone())[0]


async def forget(
    connection: AsyncConnection,
    scope: Scope,
    roster: Roster,
    entry: str,
    sources: list[str] | None = None,
    now: datetime | None = None,
) -> dict:
    now = now or datetime.now(UTC)
    id = entry_id(entry)
    async with connection.transaction():
        cursor = connection.cursor(row_factory=dict_row)
        await cursor.execute(
            "SELECT e.*, s.id AS subject_id, s.kind AS subject_kind, s.user_id, s.name"
            " FROM memory_entries e JOIN memory_docs d ON d.id = e.doc_id"
            " JOIN memory_subjects s ON s.id = d.subject_id"
            " WHERE e.id = %s AND s.guild_id = %s FOR UPDATE OF s",
            (id, scope.guild_id),
        )
        row = await cursor.fetchone()
        if row is None:
            return {"error": f"e{id} isn't an entry in this server's memory."}
        who = Subject(row["subject_id"], row["subject_kind"], row["user_id"], row["name"], "")
        if row["superseded_at"] is not None:
            return {"error": await missing_entry(connection, who, id)}
        proof = await evidence(connection, scope, who, sources)
        own = await asked_by_subject(connection, scope, who, proof, cited=bool(sources))
        if reason := resists(row, proof, now, own_request=own):
            return {"error": "Not forgotten: " + reason}
        await connection.execute(
            "UPDATE memory_entries SET superseded_at = %s WHERE id = %s", (now, id)
        )
    return {"forgot": f"e{id}", "text": row["text"]}


async def split_doc(
    connection: AsyncConnection,
    scope: Scope,
    roster: Roster,
    subject: str,
    new_doc: str,
    title: str,
    summary: str,
    move_entries: list[str],
    from_doc: str = BASE,
    now: datetime | None = None,
) -> dict:
    now = now or datetime.now(UTC)
    slug, source_slug = new_doc.strip().lower(), (from_doc or BASE).strip().lower()
    title, summary = " ".join(title.split()), " ".join(summary.split())
    if not SLUG.match(slug) or len(slug) > 40:
        raise ValueError("new_doc must be a short lowercase slug like 'music' or 'work-history'")
    if not title or len(title) > 80:
        raise ValueError("title must be 1 to 80 characters")
    if not summary or len(summary) > 240:
        raise ValueError("summary must be one line (at most 240 characters) of what belongs here")
    if slug == source_slug:
        raise ValueError("new_doc must differ from from_doc")
    ids = {entry_id(e) for e in move_entries}
    async with connection.transaction():
        who = await resolve_subject(connection, scope, roster, subject, create=True)
        await lock(connection, who)
        docs, _ = await load(connection, who.id)
        source = next((d for d in docs if d["slug"] == source_slug), None)
        if source is None:
            return {"error": f"{who.label} has no doc {source_slug!r}.", "docs": index(docs)}
        moving = [e for e in source["entries"] if e["id"] in ids]
        if strays := ids - {e["id"] for e in moving}:
            return {
                "error": "not live entries of "
                + f"{who.label}'s {source_slug!r}: "
                + ", ".join(f"e{i}" for i in sorted(strays))
            }
        if source_slug == BASE and moving and len(moving) == len(source["entries"]):
            return {"error": "Keep the essentials in the base profile: don't move every entry out."}
        moved = sum(words(e["text"]) for e in moving)
        target = next((d for d in docs if d["slug"] == slug), None)
        if target is None:
            cursor = await connection.execute(
                "INSERT INTO memory_docs (subject_id, slug, title, summary, parent_id, created_at,"
                " updated_at) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
                (who.id, slug, title, summary, source["id"], now, now),
            )
            [target_id] = await cursor.fetchone()
            target_words = 0
        else:
            target_id, target_words = target["id"], target["words"]
            if target_words + moved > MAX_DOC_WORDS:
                return {
                    "error": f"{slug!r} has {target_words} words; moving {moved} more would"
                    f" pass {MAX_DOC_WORDS}. Choose fewer entries or a new doc."
                }
        if moving:
            await connection.execute(
                "UPDATE memory_entries SET doc_id = %s WHERE id = ANY(%s)",
                (target_id, [e["id"] for e in moving]),
            )
            await connection.execute(
                "UPDATE memory_docs SET updated_at = %s WHERE id = ANY(%s)",
                (now, [source["id"], target_id]),
            )
    return {
        "doc": slug,
        "created": target is None,
        "moved": [f"e{e['id']}" for e in moving],
        "words": {source_slug: source["words"] - moved, slug: target_words + moved},
    }
