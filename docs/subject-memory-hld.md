# Subject memory — HLD

2026-10-18 · **Implemented** (issue #3): migration `0017_subject_memory.sql`,
`services/agent/src/kiki_agent_host/memory.py`, five agent tools, and a
memory line in the reply prompt.

## Decisions

- **A subject is anyone or anything Kiki learns about**: a person (keyed by
  Discord user ID, so renames don't matter) or a named thing: a dog, a game, a
  place, an inside joke. Each subject has exactly one **base profile** document,
  created with the subject, and any number of **sub docs** that Kiki creates and
  names herself.
- **A document is a list of entries, rendered as Markdown on read.** Each entry
  is one atomic claim with a kind, `fact` or `opinion`, its source messages and
  its dates. Storing entries rather than free text is what lets the temperature
  rule apply per claim and keeps every claim citable.
- **Temperature decides how hard an entry is to change.** Facts cool as they
  stand unchallenged; opinions never cool below a high floor. Changing an entry
  takes enough cited evidence for its temperature. Details below.
- **1,024 words per document.** A write that would exceed it fails, saves
  nothing, and returns step-by-step split instructions with the doc's entries
  and word counts. `split_doc` moves entries into a new sub doc.
- **Kiki chooses where a memory goes.** Without an explicit doc, `remember`
  ranks the subject's docs against the new text and places it in the best one
  with room; the result names the doc, and lists similar entries so Kiki can
  replace instead of duplicating.
- **Storage: Postgres**, next to the messages the entries cite. Object storage
  (R2 or any S3-compatible API) was considered and rejected for now; see
  [Storage](#storage).

## Prior art

| System | How memory works | What Kiki takes |
|---|---|---|
| [OpenClaw](https://docs.openclaw.ai/concepts/memory) | Plain Markdown in the agent's workspace: a curated `MEMORY.md` (durable facts, preferences, decisions) and dated daily notes. `memory_search` (semantic) and `memory_get` (file or line range). The agent distills daily notes into `MEMORY.md` and prunes; a silent turn flushes memory before compaction. An oversized `MEMORY.md` is truncated in the prompt, and the docs suggest moving detail elsewhere. | Curated documents written by the agent itself; a small always-relevant file plus detail files found by search; distillation instead of transcripts. Unlike OpenClaw, an oversized doc is refused rather than silently truncated. |
| [Muse](https://www.agent37.com/docs/agents-api/muse) (Agent37's example app) | Who the user is and who the agent is live in plain files (`SOUL.md`), ideas and goals in JSON files, with a memory editor in the app; background crons wake the instance. | A per-subject profile as the anchor; memory that people could read and correct. |
| MUSE ([arXiv 2510.08002](https://arxiv.org/abs/2510.08002), [2605.27366](https://arxiv.org/pdf/2605.27366)) | Hierarchical experience memory written after each sub-task; per-skill memory files where the agent appends lessons; long-term notes aren't compressed. | Several focused documents per subject, rather than one growing file. |
| [Letta / MemGPT](https://docs.letta.com/guides/agents/architectures/memgpt) | Core memory blocks with a label, a **description** and a **character limit**, pinned to the prompt and edited by tools (`core_memory_append`, `core_memory_replace`, …); archival memory is searched explicitly. | Hard per-document limits, a description per doc that tells the agent what belongs there, and self-editing tools. |
| [Graphiti / Zep](https://help.getzep.com/facts) | Bi-temporal facts: when a fact was true (`valid_at`/`invalid_at`) and when the system learned it (`created_at`/`expired_at`). Contradictions invalidate rather than delete. | Two clocks per entry (observed and recorded); superseded entries stay as history. |
| Mem0 ([arXiv 2504.19413](https://arxiv.org/pdf/2504.19413)) | For each extracted memory, retrieve similar ones and let the LLM choose ADD, UPDATE, DELETE or NOOP. | `remember` returns similar entries so the model can choose `replaces` (update), `forget` (delete), or nothing. |

None of these protect a long-established fact from one joking message, and none
distinguish facts from opinions in how readily they change. That is the gap
the temperature rule fills.

## Data model

```
memory_subjects   id, guild_id, kind person|thing, user_id (person), name, name_norm (thing)
memory_docs       id, subject_id, slug, title, summary, parent_id, created_at, updated_at
memory_entries    id, doc_id, kind fact|opinion, text, sources bigint[],
                  first_observed_at, last_observed_at, recorded_at, confirmations,
                  superseded_at, superseded_by, job_id
```

- One person subject per `(guild, user)`; one thing subject per
  `(guild, name_norm(name))`, the same normalization as people's names.
- The base profile is the doc with slug `profile`. A trigger creates it with
  every subject, and no tool can delete it or move its last entry away.
- Sub docs have a `parent_id` (the doc they were split from) and a one-line
  `summary` saying what belongs there. The summary is how Kiki finds a doc
  again, so `split_doc` requires it.
- Entries are never deleted by tools: replacing or forgetting sets
  `superseded_at` (and `superseded_by` for a replacement). Live entries are those
  with `superseded_at IS NULL`. History stays for audit and repair.
- Each entry is at most 60 words. Small claims keep supersession precise.

### Two clocks per entry

- **Observed** (`first_observed_at`, `last_observed_at`): the `created_at` of the
  cited source messages, read from the database, never from model arguments.
  When the claim was evidenced.
- **Recorded** (`recorded_at`): when Kiki wrote it into the doc.

A backfill reads years of history in one sitting: its entries have old observed
dates and a recorded date of today. The temperature rule below uses both, so
that a backfilled fact doesn't arrive already hardened.

## Temperature

An entry's temperature `T` is in (0, 1]. Hot entries change easily; cold ones
resist.

```
standing = min(evidence_time − first_observed_at, now − recorded_at)
T        = floor + (1 − floor) · 2^(−standing / half_life)
```

| Kind | floor | half_life | Meaning |
|---|---|---|---|
| fact | 0.05 | 90 days | Hardens over months of standing unchallenged |
| opinion | 0.60 | 30 days | Always fluid |

`standing` is how long the entry has stood unchallenged, counting only time
that is both **in the evidence timeline** and **since Kiki recorded it**:

- `evidence_time − first_observed_at` measures age in the evidence timeline,
  ending at the newest source of the change. When history is replayed in order,
  temperatures are what they would have been live: a 2024 fact challenged by a
  message a week later is a week old, not two years.
- `now − recorded_at` is the issue's "date added to the doc". A fact backfilled
  today from a 2024 message has stood for zero days and is fully hot; it cools
  only as it survives in the doc. Backfilling can't lock anything in.
- Confirming an entry adds sources and moves `last_observed_at`, but doesn't
  reset `first_observed_at` or `recorded_at`: corroboration never makes a fact
  easier to change.

### Changing an entry

`remember(..., replaces=e12)` and `forget(e12)` succeed only with enough
evidence:

```
n        = distinct cited source messages, a message by the subject themself counting twice
needed   = max(1, ⌈log₂(1/T)⌉)
accepted = n ≥ needed        (equivalently 1 − 2^−n ≥ 1 − T)
```

| Entry | T | Sources needed |
|---|---|---|
| any opinion | ≥ 0.60 | 1 |
| fact, recorded today (including backfill) | 1.00 | 1 |
| fact, 3 months standing | 0.53 | 1 |
| fact, 6 months | 0.29 | 2 |
| fact, 1 year | 0.11 | 4 |
| fact, ≥ 2 years | → 0.05 | 5 |

So a year-old fact survives one joking message, but the subject saying it
twice, or four people's messages, will change it. Opinions always yield to one
new message.

**Stale evidence never overrides newer knowledge.** If the newest cited source
predates the entry's `last_observed_at`, the change is refused: replaying an
old message must not overwrite what was learned later. The error suggests
recording the older claim as dated history if it matters.

**Sources.** Kiki cites message IDs from search results. Without citations, the
source is the current conversation's latest message by the subject (or its
latest human message if the subject hasn't spoken), because the prompt doesn't
show IDs for the current conversation. Only non-deleted human messages in the
guild count. The same message cited twice counts once.

**A person's own request to forget** skips the temperature check (not the
stale-evidence one): if the evidence includes a message by the subject in the
current conversation (cited, or, uncited, their message being the
conversation's latest human one), `forget` retires the entry however settled
it is. People control what Kiki keeps about them; a third party asking still
needs the full evidence.

Adding a new entry isn't temperature-checked: it changes nothing already known.
Confirming (`replaces` with the same text) is always accepted and adds sources.

`recall` shows each entry's kind, observed date and current number of sources
needed to change it, so Kiki knows before trying.

## Size limit and splitting

- Words are whitespace-separated tokens of live entry texts. Titles and
  summaries don't count. The limit is 1,024 per doc; the agent's prompt only
  ever sees one doc at a time, so reading one costs at most about 1,500 tokens.
- A write that would push a doc past the limit fails before saving anything.
  The error contains:
  - the doc, its current words, the write's words and the limit;
  - the doc's live entries with IDs and word counts, so Kiki can choose a theme;
  - the subject's existing sub docs with their summaries and room left, since
    the right answer may be to write there instead;
  - exact steps: call `split_doc(subject, from_doc, new_doc, title, summary,
    move_entries)` moving at least the overflow, then retry `remember` (with
    `doc=new_doc`, or without a doc to auto-place); the base profile keeps who
    the subject is; slugs are short lowercase words with hyphens; the summary
    must say what belongs in the new doc.
- `split_doc` with no entries creates an empty sub doc. It refuses to move every
  entry out of the base profile, to move entries from another subject, or to
  overflow the target.

## Discovery and placement

- `recall(subject, doc?)` returns the base profile (or the named doc) rendered
  as Markdown, plus an index of every doc of the subject: slug, title, summary,
  words. That index is how a person's docs besides the main one are found.
- `search_memory(query, subject?)` searches live entries across all subjects in
  the guild (or one subject), lexically, like message search: an OR of the
  query's English lexemes ranked by `ts_rank_cd`, with subject names matched
  too. Each hit says which subject and doc it's in.
- **Placement**: `remember` without `doc` ranks the subject's docs by how well
  the new text matches each doc's title, summary and entries, and uses the best
  match with room. The base profile wins ties and empty matches. A doc that
  doesn't match at all is never a fallback; if neither the best match nor any
  other matching doc has room, it fails with the best match's split
  instructions. The result names the chosen doc
  and the closest existing entries in the subject's docs, with a hint to use
  `replaces` if one of them is the same claim.
- Semantic (embedding) ranking for placement and search is a follow-up: the
  Embedder exists, but lexical ranking over a few small docs is adequate to
  start, and it costs nothing.

## Agent tools

| Tool | Does |
|---|---|
| `recall(subject, doc?)` | Read a subject's base profile or a named doc, plus the doc index |
| `search_memory(query, subject?)` | Find entries across subjects and docs |
| `remember(subject, text, kind, doc?, replaces?, sources?)` | Add, confirm or replace an entry, placed automatically without `doc` |
| `forget(entry, sources?)` | Retire an entry; same evidence rule as replacing |
| `split_doc(subject, new_doc, title, summary, move_entries, from_doc?)` | Create a sub doc, moving entries into it |

- `subject` is a roster ref (best), an @username, a name someone has had, or the
  name of a thing. A name that matches a person is that person; a name matching
  several people is an error that lists them, and so is an `@username` that
  matches no one. Anything else is a thing.
- Scope is the job's guild, from trusted run context, like message search.
- Budget: memory tools share a separate budget of 6 calls per run, so filing a
  memory doesn't use up searches. The model request limit rises from 6 to 10 so
  a split and retry fit.
- Writes run in one transaction that locks the subject row: concurrent replies
  can't both squeeze into the last words of a doc.
- **Replays.** A retried job repeats inference, so writes record the job ID; a
  write whose live text already exists in the subject's docs is a confirmation,
  and a confirmation from the same job is a no-op. Replaying a replacement
  (`replaces` an entry this job already superseded, with the same text) returns
  the saved replacement instead of an error.

### Prompt

The reply prompt's tool section explains what to remember (durable things people
reveal about themselves, each other or the group's world; facts versus
opinions), what not to (jokes taken as facts, things people want kept quiet,
sensitive details), and to honor "forget that". The run context lists which
people present have memory, so Kiki knows when `recall` is worthwhile without
paying for every profile in every prompt.

## Storage

Memory is small: at most 1,024 words per doc, perhaps a few hundred docs per
guild. The choice is about correctness and operations, not volume.

| | Postgres (chosen) | R2 / S3-compatible objects | Files on a volume (OpenClaw) | Git repo of Markdown (Letta MemFS) |
|---|---|---|---|---|
| Atomic size check + write | One transaction with a row lock | Conditional PUT (`If-Match` ETag) per object; multi-doc split isn't atomic | File locks on one host only | Commit per change; conflicts need merging |
| Per-entry metadata (dates, sources, temperature) | Columns | Frontmatter parsed and rewritten on every write | Same as R2 | Same as R2 |
| Search | Full-text and pgvector in the same database, joined to messages and people | Needs a separate index, i.e. Postgres anyway, plus keeping it in sync | Needs an index | Needs an index |
| Guild scoping and citations | Joins with `messages`, `people` | Application-side | Application-side | Application-side |
| Operations | Already deployed, backed up, migrated by Rust | New bucket, credentials, client; ~50–100 ms per call | Railway volumes attach to one service, which sleeps | Extra service or volume |
| Human browsing and editing | SQL, or an export | Any S3 browser; R2 has no egress fees | Direct | Direct, with history |
| Cost | Kilobytes in the existing database | R2 free tier would cover it | Volume | Volume |

**Decision: Postgres.** Every write needs a read-check-write across entries
(temperature, size, placement) and sometimes two docs (split). That is a
transaction, which Postgres has and object storage doesn't. Search would need a
database index regardless, so objects would become a second source of truth.

Object storage remains the right tool for **exporting** memory for people to
read, or for large artifacts (images, files) if Kiki ever keeps them. An export
command writing each doc's rendered Markdown to an S3-compatible bucket (R2 is
the recommendation: no egress fees, S3 API) can be added without changing the
model; the database stays authoritative.

## Not done / next

- Automatic consolidation: a background job that reads conversations and files
  memories (as OpenClaw's heartbeat does), with history backfill. The two-clock
  rule is designed for it; the job and its scheduling are not built.
- Embedding-based placement and search over entries.
- An admin view: `/kiki memory` to list or delete a subject's docs.
- Pinning base profiles of active participants into the prompt (Letta-style
  core memory) if `recall` proves too often skipped. Costs up to ~1,500 tokens
  per person per reply.
- Per-member channel permissions: memory, like search, is guild-scoped.
