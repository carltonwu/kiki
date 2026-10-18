# Per-server settings

Status: implemented v1 after Opus 5.5 review, with the narrower scope below.

## Implemented v1

The single Rust registry in `guild_settings.rs` defines 22 typed server settings, bounds,
descriptions and defaults. Existing env variables supply startup defaults; the remaining
behavioral defaults are in the registry. Overrides live in `guild_settings`, keyed by guild and
setting, with actor and timestamp. Missing rows inherit current defaults.

The settings are `chat_model`, `keywords`, `pool_debounce`, `pool_max_wait`, `pool_max_messages`,
`active_window`, `max_active_conversations`, `decide_min_confidence`,
`decide_keyword_min_confidence`, `follow_up_window`, `follow_up_max_no_confidence`, `stale_after`,
`stale_min_confidence`, `decide_recent_messages`, `decide_conversation_preview`, `history_messages`,
`channel_context`, `channel_context_age`, `reply_max_tokens`, `topic_every_messages`,
`topic_sample_messages`, and `lingo_nudge`. Duration inputs accept whole `ms`, `s`, `m`, `h`, or a bare number in
the native unit displayed by `/kiki config` (pool timings in ms; other durations in seconds).
Keywords are comma-separated, lowercased alphanumeric words. An empty list disables keyword
triggers; `value:,` can be used to supply an empty list in Discord.

- `/kiki config` shows effective values, source (`server` or inherited `default`), bounds and
  global models/timeouts. `key:<choice>` inspects one value and its reset default.
- `/kiki config key:<choice> value:<text>` changes one setting.
- `/kiki config key:<choice> reset:true` removes one override.
- Chat models use `/kiki config key:chat_model` for inspection, changes and resets.
- Manage Server is checked at runtime for every write, before catalog requests or database work.
  Chat models retain the existing OpenRouter catalog check for text and tool support. Topic and
  Decide models remain global; the topic default derives from the env chat model, not its guild
  override.

`follow_up_window` (default 300 seconds, range 0–3600) controls two ways to
continue an open conversation in the same channel. First, a direct follow-up
bypasses Decide when Kiki's latest eligible reply answered this author within the
window, no other non-bot/non-ignored person spoke between that reply and this
pool, and a pooled message contains a question mark or second-person word such
as `you` or `u`. Mentioning another person prevents this shortcut. The normal
keyword or active-conversation admission check must still match. Such runs retain
`via: decide`, but have no Decide response or Decide provider cost.

Otherwise, keyword-triggered Decide calls use `decide_keyword_min_confidence`
(default 0.25, range 0–1); other Decide calls use `decide_min_confidence`
(default 0.5). An answer below its threshold, or `no` below
`follow_up_max_no_confidence` (default 0.55, range 0–1), can continue the author's
most recently answered open conversation within the window. A keyword at the
start or end of a question/second-person message also takes this path when
Decide says `no`, even at high confidence; if no recent conversation is found,
that explicitly addressed message starts a new one. Other low-confidence
answers become `no`. Provider errors/timeouts still mean `no` on the Decide path.

Setting `follow_up_window` to 0 disables both recent-conversation shortcuts; it
does not disable starting a new conversation for the explicitly addressed keyword
case. All three settings use registry defaults and are editable through
`/kiki config key:<choice> value:<text>`; no new subcommand is required.

`lingo_nudge` is an integer percentage (default 35, range 0–100), exposed by
`/kiki config key:lingo_nudge value:<percent>`. It controls the extra per-reply
suggestion to use one manually curated phrase from this guild's `guild_lingo`
rows. Selection derives from the trigger message ID and remains stable on retry.
At 0 no suggestion is added; the complete guild phrase list still appears in the
system prompt. An empty phrase list produces no suggestion at any percentage.
This does not enable automatic learning or provide phrase-editing commands.

The leader loads all configured guilds after acquiring leadership. Commands, router and agent
share one cache. A writer mutex covers transaction commit and cache publication, preserving write
order. There is no LISTEN listener: this process is the only supported writer; direct SQL edits
require a restart. Pool creation takes a cached immutable snapshot without database I/O. Replies
snapshot settings when building context and persist model input in `host_request`; retries resend
that input. Delivery finds the trigger independently of the current history window. Topic IDs
include sample size to avoid durable input conflicts when that setting changes.

Writes validate the complete effective config under the existing global pool advisory lock.
Cross-field rules are `pool_debounce + 2s <= ROUTE_MAX_MESSAGE_AGE_MS`,
`pool_max_wait >= pool_debounce`, and `stale_min_confidence >= decide_min_confidence`.
Invalid individual overrides and overrides involved in broken cross-field rules fall back to
validated defaults on load, with warnings and flags in the config display. Unaffected overrides
remain active. Invalid env defaults cause startup failure.

Eviction filters both its update and keep-set by guild. A limit change evicts excess conversations
in that transaction. Routing reads the effective limit from the database inside the locked tag
transaction, so a pool created before a shrink still obeys it. The existing global advisory lock
is deliberately retained; a two-int lock cannot represent a Discord snowflake directly.

Migration 0012 copies existing model overrides, actor and timestamp. It retains
`guild_chat_models` so a previous leader can keep serving during migration. Changes made through
an old binary after the copy are not synchronized; repeat those changes after the new leader
takes over. The old table can be dropped in a later release. Migration 0013 adds the guild index.

V1 uses hard code bounds from the inventory below. Operator-adjustable ceilings, change history,
autocomplete, reset-all, custom/pending emoji settings, host tool/model-request/search budgets,
and cross-process cache invalidation are deferred. This intentionally replaces the earlier
proposal's operator-ceiling decision. Host budgets need a backward-compatible canonical request
comparison and host-first rollout. Python host and retrieval files are unchanged by this work.
Infrastructure timeouts, credentials, Discord limits, prompt-shape constants and retrieval
ranking remain global or fixed.

## Earlier proposal (superseded; retained as future design notes)

The sections below describe the broader proposal before this implementation. Where they differ,
the implemented-v1 scope above is authoritative; their unimplemented features are future work.

## Goal

The numbers that decide how Kiki behaves (when she answers, how long she waits, how much context
she reads, how big the active pool is) are scattered across env vars and `const`s. Make the
behavioural ones visible and editable per Discord server with `/kiki config`, without a redeploy,
inside ceilings the operator controls. Infrastructure limits stay global.

Non-goals: per-channel settings, editing prompts, per-server models (v1), secrets in Discord, a web
UI.

## Inventory

Every tunable found today. **Scope**: *server* = settable per guild, *global* = env var only (shown
read-only in `/kiki config show`), *fixed* = stays a `const`. **Ceiling** marks cost-relevant
settings whose upper bound the operator can lower with an env var (see Bounds).

### Routing (`router/`)

| Key | Today | Default | Bounds | Scope |
|---|---|---|---|---|
| `keywords` | env `ROUTE_KEYWORDS` | `kiki` | 0–20 words; letters/digits only, ≤32 chars | server |
| `pending_emoji` | env `ROUTE_PENDING_EMOJI` | 👀 | unicode, or a custom emoji of this guild the bot can use; empty = off | server |
| `active_window` | env `ROUTE_ACTIVE_WINDOW_SECS` | 1 h | 0–24 h | server, ceiling |
| `pool_debounce` | env `POOL_DEBOUNCE_MS` | 500 ms | 0 ms to `max_message_age` − 2 s | server |
| `pool_max_wait` | env `POOL_MAX_WAIT_MS` | 8 s | ≥ debounce, ≤ 30 s | server |
| `pool_max_messages` | env `POOL_MAX_MESSAGES` | 10 | 1–50 | server |
| `max_active_conversations` | env `MAX_ACTIVE_CONVERSATIONS` | 8 | 1–8 | server (after pool isolation) |
| `decide_min_confidence` | env `JEV_MIN_CONFIDENCE` | 0.5 | 0–1 | server |
| `decide_keyword_min_confidence` | default | 0.25 | 0–1: the bar when a keyword named Kiki | server |
| `follow_up_window` | default | 5 min | 0–1 h: after Kiki replies, a participant's unsure Decide answer (below the bar, or "no" below the next setting) continues that conversation | server |
| `follow_up_max_no_confidence` | default | 0.55 | 0–1 | server |
| `stale_after` | const `STALE_AFTER_SECS` | 30 min | 1 min–24 h | server |
| `stale_min_confidence` | const `STALE_MIN_CONFIDENCE` | 0.9 | ≥ decide_min_confidence, ≤ 1 | server |
| `decide_recent_messages` | const `RECENT_MESSAGES` | 15 | 1–50 | server, ceiling |
| `decide_conversation_preview` | const `CONVERSATION_PREVIEW` | 3 | 0–10 | server, ceiling |
| `max_message_age` | env `ROUTE_MAX_MESSAGE_AGE_MS` | 5 s | | global |
| `jev_timeout` | env `JEV_TIMEOUT_MS` | 5 s | | global |
| `CONVERSATION_CHOICES` | const | 8 | | fixed: Jev's 10 choices minus `new`/`no` |

### Agent (`agent/`, Python host)

| Key | Today | Default | Bounds | Scope |
|---|---|---|---|---|
| `history_messages` | env `AGENT_HISTORY_MESSAGES` | 30 | 1–190 | server, ceiling |
| `channel_context` | const `CHANNEL_CONTEXT` | 8 | 0–30 | server, ceiling |
| `channel_context_age` | const `CHANNEL_CONTEXT_SECS` | 30 min | 0–24 h | server |
| `reply_max_tokens` | const `MAX_REPLY_TOKENS` | 2000 | 100–4000, **per model request** | server, ceiling |
| `max_tool_calls` | Python `MAX_TOOL_CALLS` | 4 | 0–8, **tool calls executed per run** | server, ceiling |
| `max_model_requests` | Python `MAX_MODEL_REQUESTS` | 6 | 1–10 | server, ceiling |
| `search_results` | Python `search(limit=10)` | 10 | 1–25 | server, ceiling |
| `topic_every_messages` | env `TOPIC_EVERY_MESSAGES` | 4 | 1–50 (lower = more topic calls) | server |
| `topic_sample_messages` | const `SAMPLE_MESSAGES` | 12 | 4–50 | server, ceiling |
| `run_max_age` | env `AGENT_RUN_MAX_AGE_SECS` | 120 s | | global |
| `host_timeout` | env `AGENT_HOST_TIMEOUT_MS` | 180 s | | global |
| host job timeout, concurrency | env `AGENT_HOST_JOB_TIMEOUT_SECS`, `AGENT_HOST_MAX_CONCURRENT_JOBS` | 120 s, 2 | | global |
| tool and embedding deadlines | Python `TOOL_TIMEOUT_SECONDS`, `EMBEDDING_TIMEOUT_SECONDS` | 10 s, 4 s | | global (promote to env) |
| `chat_model`, `topic_model`, `decide_model` | env | | | global |

### Kept fixed in v1

| Constant | Value | Why |
|---|---|---|
| Retrieval ranking (`MIN_SIMILARITY` 0.25, `RECENCY_WEIGHT` 0.15, `HALF_LIFE_DAYS` 30, `CANDIDATES` 50, `RRF_K` 60) | | Tuned together for the embedding model; change only with evals |
| Context neighbours (`retrieval.py`) | default 5, cap 10 | Model chooses within the cap; revisit with search evals |
| `SNIPPET_CHARS`, `MAX_TOPIC_TOKENS`, `MAX_TOPIC_CHARS` | 1000, 400, 200 | Product choices with little per-server value; promote later if asked |
| Roster (`MAX_PEOPLE`, `MAX_LABEL_CHARS`, `FORMER_USERNAMES`) | 200, 32, 2 | Prompt-shape choices, same reasoning |
| Discord and infra (`MAX_MESSAGE_CHARS` 1900, `PAGE_SIZE` 100, backfill concurrency, lock keys, intervals, REST retries, embedding batch sizes) | | Protocol or operational |

## Design

### Storage

```sql
CREATE TABLE guild_settings (
    guild_id   BIGINT NOT NULL,
    key        TEXT   NOT NULL,
    value      JSONB  NOT NULL,
    updated_by BIGINT NOT NULL,          -- Discord user id
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (guild_id, key)
);

CREATE TABLE guild_settings_log (        -- append-only, for /kiki config history
    id BIGSERIAL PRIMARY KEY, guild_id BIGINT NOT NULL, key TEXT NOT NULL,
    old_value JSONB, new_value JSONB,    -- NULL new_value = reset
    changed_by BIGINT NOT NULL, changed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

Only overrides are stored; a missing row means "use the default", so changing an env default still
reaches every server that hasn't overridden it.

### Registry, resolution and bounds

One Rust registry (`settings.rs`) lists each server setting once: key, type (int, float, duration,
word list, emoji), code bounds, description, env var for the default, and whether it has an
operator ceiling.

> effective value = server override → env default → built-in default

Ceilings: `SETTINGS_CEILING_<KEY>` (e.g. `SETTINGS_CEILING_REPLY_MAX_TOKENS=1000`) lowers a
setting's upper bound below the code bound. Env defaults must sit inside the ceiling or the
ingester refuses to start.

Validation always checks a **whole effective configuration**, never one row:

- **Write** (`set`, `reset`): in one transaction, take a per-guild advisory lock, load the guild's
  overrides, apply the change, resolve and validate the full config (bounds, ceilings, cross-field
  rules), then write the override and the log row and `NOTIFY`. Concurrent writes to one guild are
  serialized, so two individually valid changes can't jointly break a rule. The cache is updated
  after commit, in commit order (the cache swap happens under the same per-guild mutex as the
  write).
- **Load** (startup, or after an env change): if the resolved config breaks a rule, the overrides
  involved are ignored (falling back to defaults), logged, and flagged in `/kiki config show` so an
  admin can fix or reset them.

Cross-field rules: `pool_debounce + 2 s ≤ max_message_age` (a single-message pool is classified
about `debounce` after it arrives and is dropped if older than `max_message_age`);
`pool_max_wait ≥ pool_debounce`; `stale_min_confidence ≥ decide_min_confidence`. There is no
`max_model_requests > max_tool_calls` rule, since tool calls can run in parallel within one request.

`keywords` are validated against the router's tokenizer (`classify.rs` splits on
non-alphanumerics), so a keyword with punctuation, which could never match, is rejected.

### Reading settings at runtime

The ingester keeps a cache of each guild's resolved settings (`HashMap<guild_id,
Arc<GuildSettings>>`). It is loaded after leadership is acquired (only the leader routes and
answers) and updated by the writer. It also `LISTEN`s on `guild_settings_changed` and reloads after
a listener reconnect, for when there is more than one consumer.

Each unit of work takes one snapshot and uses it to the end:

| Unit | Snapshot taken | Used for |
|---|---|---|
| Pool | when the pool is **created** (first message) | debounce, max wait, max messages, then classification (keywords, windows, thresholds, Jev context) |
| Agent run | when the run is built | history, channel context, reply tokens, tool budget, search results; stored in `host_request` with the rest of the input |
| Topic job | when the job is built | sample size |

Changes apply to the next pool or run.

### Agent host contract

New `JobRequest` fields: `max_tool_calls`, `max_model_requests`, `search_results`. Durable retries
compare the stored request with the new one (`jobs.py`, `extra="forbid"`), so:

1. **Deploy the host first.** The new fields are optional, defaulting to today's constants, so it
   still accepts old requests.
2. **Compare canonically.** The host compares requests after dropping fields equal to their
   defaults, so a request stored before the upgrade still matches its retry afterwards.
3. Rust then sends the fields. Retries resend the persisted `host_request` unchanged, so a settings
   change never conflicts with an in-flight run.
4. **Topic job ids** include a short hash of the job's settings, so a changed sample size starts a
   new job instead of conflicting with a cached one under the same id.

`max_tool_calls` keeps today's meaning (tool calls executed per run; further calls get a "budget
spent" result). With `max_tool_calls = 0` the host also skips starting background indexing.

### Pending reaction lifecycle

Today the emoji is fixed at startup and the agent removes that same emoji after replying
(`agent/discord.rs`). Per server, the emoji can change between reacting and replying, so the
router records the emoji it actually used (`agent_runs.pending_emoji`), and clearing, including
after restart recovery, removes the recorded one. Custom emoji are sent as custom reactions
(`name:id`), not `Unicode`.

### Active pool isolation

`max_active_conversations` is one pool shared by every guild today, under one advisory lock;
`evict_beyond` can close conversations in any guild. This must be fixed **before** the setting is
exposed:

- `evict_beyond(tx, guild_id, max)`: scope both the outer `UPDATE` and the inner keep-set `SELECT`
  to the guild.
- Pool lock per guild: `pg_advisory_xact_lock(POOL_LOCK_CLASS, guild_id)` (two-key form, so it
  can't collide with the leader lock).
- The limit is read **inside** the locked transaction from `guild_settings` (not from the pool's
  snapshot), so a stale snapshot can't keep more conversations open after a shrink.
- Shrinking the limit evicts the excess immediately, in the same transaction as the `set`.
- The Jev candidate list keeps its channel filter; `/kiki conversations` shows this guild's slots.
- Index: `conversations (guild_id, last_active_at DESC) WHERE closed_at IS NULL`.

### Commands

`config` becomes a subcommand group next to the existing flat subcommands (Discord allows mixing
the two). Manage Server stays the default permission, and writes also **check it at runtime** from
the interaction's `member.permissions`, since admins can re-grant the command to other roles.

| Command | Does |
|---|---|
| `/kiki config show [section]` | Effective values as tables per section (Routing, Agent, Global): key, value, source (`server` / `env` / `default`), bounds. Invalid overrides flagged. One section per embed so it fits Discord's limits. |
| `/kiki config set <key> <value>` | Validates the whole config, stores, logs, replies with old → new. |
| `/kiki config reset <key\|all>` | Deletes overrides after validation. `all` asks for confirmation with a button. |
| `/kiki config history [key]` | Last 15 changes: when, who, key, old → new. |

Interaction handling:

- Dispatch on the full option path (`config` → `set`), not one level.
- **Autocomplete** (interaction type 4) for `key` is answered directly, without deferring, from the
  in-memory registry: at most 25 matches, labels ≤100 chars (current value truncated). Manually
  typed keys are still validated.
- **Buttons** (type 3): `custom_id` encodes action, guild, the invoking user, and an expiry; the
  handler rejects other users and expired buttons, and rechecks permissions.
- `value` is a string parsed by the key's type, with units (`500ms`, `8s`, `30m`, `1h`) and
  comma-separated word lists.

## Rollout

1. **Pool isolation**, per-guild eviction and lock, and index. No setting yet; behaviour identical
   with one guild.
2. **Settings core**: migration, registry, ceilings, cache, `show` / `set` / `reset`, runtime
   permission check, autocomplete. Routing settings including `max_active_conversations`, plus
   `pending_emoji` with its lifecycle column.
3. **Agent host** contract (host first), then the agent settings in Rust.
4. `history` and the `reset all` confirmation button.

## Tests

- Registry: bounds, ceilings, unit parsing, keyword tokenizer rule, cross-field rules.
- Resolution order, and load-time handling of overrides an env change made invalid.
- Concurrent `set`s on one guild that break a cross-field rule only together: one must fail.
- Cache publication order under concurrent writes.
- Postgres: same-guild concurrent eviction; cross-guild isolation (eviction in A never touches B);
  shrink with a revive in flight.
- Pool snapshot: a mid-pool settings change doesn't alter that pool.
- Emoji change between react and reply, including after restart recovery.
- Commands: unauthorized `set` and button presses rejected; autocomplete ≤25 choices; `show`
  fits embed limits with every key overridden.
- Host contract: a request persisted by the old version, retried against the upgraded host,
  doesn't conflict.

## Decisions (from review)

1. **Models stay global in v1.** Later: operator-approved aliases with budget ceilings.
2. **Cost**: operator ceilings separate from defaults (above).
3. **Permissions**: Manage Server, enforced at runtime, within the ceilings.
4. **Emoji**: custom emoji must belong to the guild and be usable by the bot.
