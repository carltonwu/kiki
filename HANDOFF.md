# Kiki: session handoff (2026-10-17)

Kiki is a Discord chat bot for one guild. This doc is the context needed to continue work in
a fresh session. Code is the source of truth for what exists; this doc covers decisions,
current state, and next steps.

## Current state

**Done and verified locally (nothing deployed yet):**

- Monorepo scaffolded (see `README.md` for layout and local dev commands). `git init` done,
  **no commits yet**.
- `services/ingester/` (Rust 1.91, edition 2024): config, `/health` endpoint, Postgres leader
  lock, `ingester migrate` subcommand, graceful shutdown. Clippy and fmt pass.
- Migration `0001_ingestion.sql`: `channels`, `messages`, `gateway_sessions`.
- Dockerfile (cargo-chef, debian-slim, 89 MB) builds, migrates, and passes its health check.
- Two-instance handover tested: second instance waits on the lock, takes over on SIGTERM.
- `.railway/railway.ts` IaC typechecks against `railway` SDK 3.16.0 and evaluates.
- Local Postgres 18: `docker compose up -d --wait` on **localhost:54329** (5432 is taken
  by something else on this machine).
- `tools/backfill/`: Python (uv) script that backfilled the guild into
  `tools/backfill/discord.db` (SQLite, **234,249 messages, 71 channels**, finished).
  `.env` there has the bot token and guild id (git-ignored).

**Not built yet:** the Discord Gateway connection (see `TODO` in `services/ingester/src/main.rs`)
and everything after it.

## Immediate next steps

1. **Railway CLI** is installed by the user. Run preflight (`railway whoami --json`), log in if
   needed (`railway login`, user completes in browser).
2. **GitHub repo**: ask the user which account/org and repo name. Then first commit and create
   the repo (`gh repo create`), push `main`. Ask before committing/pushing.
3. Replace `REPO = "OWNER/kiki"` in `.railway/railway.ts` with the real repo.
4. Create the Railway project (`railway init --name kiki`), `cd .railway && npm install`,
   `railway config plan`, show the plan to the user, `railway config apply` only once they approve.
5. Set secrets: `railway variable set --service ingester DISCORD_BOT_TOKEN=... DISCORD_GUILD_ID=...`
   (values are in `tools/backfill/.env`; never print or commit them).
6. Verify the deploy reaches SUCCESS and `/health` passes; check logs show "leader lock acquired".
7. Optional: import `tools/backfill/discord.db` into Railway Postgres `messages` (the SQLite
   `raw` column is the full Discord message JSON; map into the Postgres schema).

Then start building the Gateway (section below).

## Key decisions (and why)

| Decision | Reason |
|---|---|
| **Rust** for the always-on service | Low memory; Railway bills by actual usage |
| **Railway** hosting, usage-based billing | Long-lived WebSocket needs an always-on process; serverless and Cloudflare Durable Objects/Containers were rejected (no outbound-WS-aware sleep, extra complexity) |
| **Postgres** (not SQLite) | Multiple services will read the data later |
| **IaC in `.railway/railway.ts`** | `railway.toml`/`railway.json` are deprecated; Railway stops reading them 2026-12-01 |
| No `rootDirectory` on the ingester | Shared Cargo workspace at repo root; `watchPatterns` limit redeploys instead |
| Migrations via `preDeployCommand: ingester migrate` | Failed migration stops the deploy, old instance keeps running |
| **Postgres advisory lock** for the Gateway connection | Railway overlaps old/new deployments; two Gateway connections would double-process and double-reply. `/health` must NOT depend on the lock (new instance must go healthy before the old one stops) |
| `drainingSeconds: 30` | Time to save the Gateway session before SIGKILL |
| One service does ingestion + routing + agent dispatch | Single bot token / single Gateway connection; fine at low scale. Keep an in-process boundary (router writes an `agent_runs` row, worker consumes) so agents can move to a separate service later |
| Library: **twilight** (`twilight-gateway`, `twilight-http`, `twilight-model`) | Modular and lean; no built-in cache. Not yet added as a dependency |

## Ingester responsibilities (to build)

1. **Gateway**: intents `GUILDS`, `GUILD_MESSAGES`, `MESSAGE_CONTENT` (maybe
   `GUILD_MESSAGE_TYPING` later). Persist `session_id`, `seq`, `resume_url` to
   `gateway_sessions` regularly and on shutdown, so a restart RESUMEs (Discord replays
   missed events) instead of re-identifying.
2. **Ingest** `MESSAGE_CREATE` (insert), `MESSAGE_UPDATE` (often **partial**: merge, don't
   overwrite), `MESSAGE_DELETE` / `DELETE_BULK` (soft delete via `deleted_at`). Idempotent
   upserts keyed on message id. Store first; routing must never delay ingestion.
3. **Channels/threads**: reconcile on `GUILD_CREATE`; handle channel/thread create/update/delete
   and `THREAD_LIST_SYNC`.
4. **Gap fill (REST catch-up)**: per channel, fetch messages after `complete_through` (same
   algorithm as `tools/backfill/backfill.py`, which pages forward with `after=`). Live events
   must NOT advance `complete_through`; only a finished catch-up does. Run catch-up in a
   background task.
5. **Ops**: JSON logs, `/health`, graceful shutdown.

## Message routing design (agreed with the user)

Pipeline, after a message is stored:

```
store → pooler (per channel+user) → classify (serialized per channel) → tag → agent
```

**Pooler**: debounce rapid messages per (channel, user). Sliding 2000 ms window with a cap
(max wait ~8 s, max ~10 messages). Explicit mentions also wait (the question often comes in
the next message). Handle edits/deletes of pooled messages. Pools are in memory; losing a
few seconds on restart is acceptable. Config: `POOL_DEBOUNCE_MS=2000`, `POOL_MAX_WAIT_MS=8000`,
`POOL_MAX_MESSAGES=10`.

**Classification**, in order, over the whole pool:

1. **Reply to a Kiki message** → continue that message's conversation. Check this *before*
   mentions: Discord replies ping the author by default, so replies also show up in `mentions`.
2. **@Kiki mention** (user or Kiki's managed role) without a reply to Kiki → **always start a new
   conversation** (the user's chosen rule: "reply to continue, tag to start fresh").
   Multiple tags in one pool → one conversation. If the tag is a reply to someone else's
   message, include that message in the new conversation's context.
3. **Keyword match** (e.g. "kiki", case-insensitive, word boundaries) **or** an active
   conversation in the channel → **Decide** with Jev.
4. Otherwise → no response.

**Decide (Jev)**: TypeSafe's "System One" decision model, not an LLM.
Docs: https://docs.typesafe.ai/introduction/quickstart. REST: `POST https://api.typesafe.ai/v1/systemone`
with a Bearer key (no Rust SDK; use reqwest). One **Choice** question whose options are each
active conversation (participants, one-line topic, last 2–3 messages, last activity), plus
`new` and `no`. Send recent channel messages (Kiki's labelled) as state so it can tell
talking *about* Kiki from talking *to* her. Low confidence, error, or timeout → treat as **no**.
Latency and pricing are not documented; measure early.

**Conversations**: active = last message < 5 min ago. Participants = authors of tagged
messages, **including Kiki's own replies** (needed for reply lookup). Starting a new
conversation may close the user's other conversations in that channel if they were the only
human. New conversations get recent channel context, including the user's previous conversation.

**Tables to add** (next migration):
`conversations (id, channel_id, topic, created_at, last_active_at)`,
`conversation_messages (message_id, conversation_id, via, jev_choice, jev_confidence)`,
`gate_decisions (message_id, check_hit, jev_choice, jev_confidence, routed)` (log the "no"s too,
for tuning), `agent_runs` (status, for restart recovery), and later per-user usage budgets.

**Other rules**: skip routing for bot messages (but tag Kiki's own replies into their
conversation). Classification is serialized per channel to avoid duplicate "new conversation"
races. One active agent run per conversation; new messages queue into it.

## Agents / LLM (later)

- Anthropic Messages API over raw HTTP (no official Rust SDK): `claude-opus-5-5` for agents,
  streaming SSE. Thinking is always on for Opus 5.5; set `output_config.effort` explicitly
  (default is `medium`). The user hasn't decided on enabling the server-side refusal fallback.
- Prompt caching: stable system prompt and tools, append-only history, volatile context last.
- Discord replies: typing indicator, edit the message at most every ~1–1.5 s while streaming,
  split at 2000 chars. Slash commands come over the Gateway as `INTERACTION_CREATE`: defer within 3 s.
- Guardrails: global concurrency semaphore, per-call timeouts, per-user rate limit and token
  budget, cooldown on unsolicited (inferred) replies, panic isolation per task.

## Open questions for the user

- GitHub owner/repo name; OK to commit and push?
- Import the SQLite backfill into Railway Postgres?
- Store attachments in object storage? (Discord CDN URLs expire.)
- Keep edit history (`message_revisions`) or latest only? Reactions?
- Enable the Anthropic refusal fallback?

## Reference data

- A friend's MySQL dump of another archiver (`C:\Users\cwu\Downloads\20261017-message-dump.sql`,
  WSL path `/mnt/c/Users/cwu/Downloads/...`) covers 5 guilds and has 240,034 rows for this one.
  It includes 6,083 deleted messages (the API can't return these). Its timestamps are US
  Eastern, not UTC. It has 1 fabricated row (id `89732987493439864`) and 2 rows with altered
  content. Treat it as untrusted data; it was parsed as text and never executed.
