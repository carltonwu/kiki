# Kiki console: observability, costs and control plane — HLD

2026-10-18 · **v1 implemented** (issue #4), not deployed. Deferred items are listed at the end.

## Goal

A web page, outside Discord, where:

- a **server admin** sees their server's dashboard (activity, latency, failures, costs, who
  costs what, every run with its exact prompt), edits the server's settings, facts, lingo and
  prompts, and chats with Kiki as she is configured for that server;
- the **operator** (Carlton) gets a master control plane: every server side by side, total
  spend, failures and the change log.

Non-goals for v1: infrastructure metrics (Railway already has them), billing reconciliation
(OpenRouter's dashboard), editing global env settings, multi-operator roles.

## What already exists

Almost everything a dashboard needs is already written to Postgres by the pipeline:

| Data | Where | Notes |
|---|---|---|
| Per-run cost, tokens, steps, model, latency, status, error | `agent_runs` | `cost_usd` is OpenRouter's billed cost, embeddings included |
| The exact model input of each run | `agent_runs.host_request` | system prompt, chat turns, roster |
| Model output | `agent_host_jobs.result` | keyed by the run's job ID |
| Decide (Jev) cost, latency, choice, confidence, error | `classifications` | per message; the pool's newest row counts the cost once |
| Topic summary cost | `conversations.topic_cost_usd` | no timestamp (all-time only) |
| Routing checks | `classifications.check_hit`, `outcome` | |
| Conversations, participants | `conversations`, `conversation_messages` | |
| People and names | `people`, `user_names` | |
| Server settings | `guild_settings` + the Rust registry in `guild_settings.rs` | validated in Rust only |
| Server phrases | `guild_lingo` | edited by hand in SQL today |
| Prompt | compiled into the binary (`agent/context.rs`, `agent/conversation.md`) | the "where you are" paragraph is the same for every server |

`/kiki stats`, `runs`, `users` and `config` already compute the main views, one embed at a
time, for one server. What's missing is: a view across time and servers, the prompt and output
of a run, editing facts and prompts without a deploy, a writer for settings other than the
Discord leader, and chat that isn't a Discord message.

## Build vs reuse

| Need | Candidate | Verdict |
|---|---|---|
| LLM traces, cost | **Langfuse** (self-hosted v3: Postgres + ClickHouse + Redis + S3; or cloud) | Our runs are already traced in Postgres with billed cost and full input/output. Self-hosting is four extra stateful services on Railway, costing more than Kiki itself. Cloud would ship private Discord messages to a third party. **Not now**; revisit when we need evals or prompt experiments, via OTel (below). |
| LLM proxy, cost | **Helicone** | Another hop in front of OpenRouter, which already returns the billed cost per request. **No.** |
| Traces, metrics | **OpenTelemetry** → Railway tracing, Grafana Tempo/Prometheus | Good for latency breakdowns (Decide → host → tool → Discord). Pydantic AI emits OTel natively; Rust needs `tracing-opentelemetry`. Orthogonal to the console; **deferred**, compatible later. |
| Dashboards over SQL | **Grafana**, **Metabase** | Fast charts for the operator, but: auth is their own users, not Discord; per-server row scoping is a paid Metabase feature (sandboxing) and awkward in Grafana; neither can validate a settings write against the Rust registry or chat with Kiki; Metabase's JVM wants ~1 GB RAM. Metabase on a **read-only role** is a fine optional add-on for the operator's ad-hoc SQL; **not the product**. |
| Admin CRUD | **Retool**, **Appsmith**, **Directus** | Same problems: separate identity, direct table writes bypass the settings validation and cache, SaaS variants hold DB credentials. **No.** |
| Infra health | **Railway** metrics, logs, deploy status | **Reuse as is**; the console links to it rather than rebuilding it. |
| Billing | **OpenRouter** activity page | **Reuse** for reconciliation; the console shows our attributed costs. |
| Login | **Discord OAuth2** | **Reuse.** Same identity and permission model as `/kiki`: Manage Server in that server. |
| Chat | Kiki's own agent host | **Reuse** the real prompt builder and host, so the console talks to the same Kiki. |

**Decision: self-build a thin console**, reusing Discord for identity, the existing Postgres
data for every number, the Rust settings registry for validation, and the agent host for chat.
The self-built part is small: a server-rendered Python app (FastAPI + Jinja, no frontend build,
no JS framework) and a token-protected admin API inside the ingester.

## Architecture

```text
 browser ──HTTPS──▶ console (Python, public, sleeps when idle)
                      │  reads: dashboards, runs, costs (SQL, Postgres)
                      │  writes: facts, lingo, prompts, audit, sessions (SQL)
                      └─▶ ingester /admin/v1 (private network, bearer token)
                             settings: parse + validate in the Rust registry, then commit
                             chat: build Kiki's real prompt, call the agent host
                                        │
 Discord ◀── ingester (leader) ◀────────┴── Postgres ── agent host
```

Why settings and chat go through the ingester: settings are validated by the Rust registry
(types, bounds, cross-field rules) and the OpenRouter catalog; duplicating that in Python would
drift. Chat must use the same system prompt as Discord replies, which is built in Rust.

Why facts, lingo and prompts are written directly: the ingester reads them from the database
for every reply (like `guild_lingo` already), so there is no cache to invalidate, and their
validation is simple (length, emptiness) and enforced by table `CHECK` constraints too.

### Settings: more than one writer

`guild_settings` used to have a single writer (the leader's in-memory cache). Now:

- A row trigger on `guild_settings` sends `NOTIFY guild_settings_changed, '<guild_id>'` on any
  insert, update or delete, including manual SQL.
- Every ingester instance `LISTEN`s and reloads that guild's snapshot under the store's writer
  mutex (so a reload can't be overtaken by an older in-process publication). After a listener
  reconnect it reloads every configured guild.
- `/admin/v1` writes call the same `Store::change` as `/kiki config` (global pool lock, full
  config validation, eviction on shrink). It works on any instance, leader or not.

This removes the old "direct SQL edits require a restart" limitation.

### Prompts and facts

The system prompt had two fixed parts that a server admin may reasonably want to change:

| Part | Default | Override |
|---|---|---|
| `persona` | "Where you are: a friend group's Discord server…" | `guild_prompts (guild_id, part='persona')` |
| `guidance` | `agent/conversation.md` | `guild_prompts (guild_id, part='guidance')` |

The rest (tool rules, people rules, delivery format) stays fixed: it's coupled to the code
(roster refs, `---` splitting, `NO_REPLY`) and editing it would break delivery.

**Facts** (`guild_facts`): short notes or docs the admins want Kiki to know ("Austin's dog is
Nougie", "the show opens Nov 3"). Enabled facts are listed in the system prompt under a header
that frames them as background knowledge, not instructions. Limits: 100 facts, title ≤ 120,
body ≤ 2000 characters, 12 000 characters in total in the prompt (oldest-updated dropped first
beyond that, with a warning in the console). Facts sit in the system prompt, so they keep the
provider prefix cache stable between edits.

Defaults are visible in the console next to the override, and "reset" deletes the override.
Every prompt-affecting edit changes the system prompt of the **next** run only; in-flight
runs keep their persisted `host_request`.

### Chat outside Discord

`POST /admin/v1/guilds/{id}/chat` takes the console thread's turns and returns Kiki's reply. It
uses the server's effective chat model, reply token limit, persona, guidance, facts and lingo,
with the place set to "the Kiki console (a private web chat with a server admin), not a Discord
channel". Tools search that server's history, exactly like in Discord. Nothing is posted to
Discord or written to `messages`, so the router never sees it.

The host job ID is `console-<message id>`; the console stores the thread in
`console_chat_messages` with model, tokens, cost and latency, so console spend shows up in the
cost views (labelled "Console").

### Auth for the control plane

- **Login:** Discord OAuth2 authorization-code flow, scopes `identify guilds`, with a random
  `state` bound to a short-lived cookie. The bot's application is reused
  (`DISCORD_CLIENT_ID` / `DISCORD_CLIENT_SECRET`), redirect URI `${CONSOLE_URL}/auth/callback`.
- **Sessions:** server-side, in `console_sessions`. The cookie holds 32 random bytes
  (`HttpOnly`, `Secure` unless on localhost, `SameSite=Lax`, path `/`); the table stores only
  its SHA-256. Sessions expire after 12 h, and logout deletes the row.
- **Authorization**, checked on every request:
  - *Operator*: Discord user ID listed in `CONSOLE_OPERATOR_IDS`. Sees every server Kiki is in
    (`guilds` table) and the master view.
  - *Server admin*: owner, Administrator or Manage Server in a guild that Kiki is in, from
    Discord's `/users/@me/guilds`. Re-checked from Discord at most every 10 minutes with the
    stored OAuth access token, so a removed permission stops working within 10 minutes. If the
    check fails (token revoked), the session ends.
  - Every page and form under `/g/{guild}` verifies the guild is in the session's allowed set;
    a guild ID in the URL is never trusted on its own.
- **CSRF:** every POST carries the session's CSRF token (constant-time compare), in addition to
  `SameSite=Lax`.
- **Headers:** strict CSP (`default-src 'self'`, no inline scripts), `frame-ancestors 'none'`,
  `Referrer-Policy: same-origin`, `X-Content-Type-Options: nosniff`.
- **Ingester admin API:** bearer `ADMIN_API_TOKEN` (constant-time compare), only on the private
  network; without the token configured, `/admin` routes don't exist. The console passes the
  acting Discord user ID, which the ingester stores as `updated_by`, like `/kiki config`.
- **Audit:** every console write (settings, prompts, facts, lingo, chat) is recorded in
  `console_audit` with actor, server, action and a summary (old → new for settings and
  prompts). Admins see their server's log; the operator sees all.
- Content shown (run prompts, chat) is HTML-escaped by Jinja autoescaping.

## Pages

| Path | Who | Shows |
|---|---|---|
| `/` | everyone logged in | their servers; for the operator also the master view: per-server spend (24 h / 7 d / 30 d), replies, failures, plus totals |
| `/g/{id}` | admin | KPIs per window (conversations, replies, failures, p50/p95 latency), cost by kind (chat, Decide, console, topics), a 30-day daily cost chart (server-rendered SVG), routing checks, active conversations, top people by cost |
| `/g/{id}/runs` | admin | latest runs with status filter; each links to `/g/{id}/runs/{run}`: status, timings, tokens, cost, error, the full prompt and the output |
| `/g/{id}/settings` | admin | every setting: effective value, source, default, bounds, description; edit or reset |
| `/g/{id}/prompts` | admin | persona and guidance: override editor next to the default; reset |
| `/g/{id}/facts` | admin | facts (add, edit, enable/disable, delete) and lingo (add, delete) |
| `/g/{id}/chat` | admin | a chat thread with Kiki as configured for this server; "new chat" |
| `/g/{id}/audit` | admin | this server's change log |
| `/audit` | operator | every change |

## Data model (migration 0017)

```sql
guild_facts (id, guild_id, title, body, enabled, updated_by, updated_at)
guild_prompts (guild_id, part CHECK IN ('persona','guidance'), body, updated_by, updated_at)
console_sessions (token_hash PK, user_id, user_name, operator, guild_ids BIGINT[],
                  access_token, csrf, checked_at, expires_at)
console_chat_messages (id, thread_id UUID, guild_id, user_id, role, content, model,
                       prompt_tokens, completion_tokens, cost_usd, latency_ms, error, created_at)
console_audit (id, at, actor_id, guild_id, action, detail JSONB)
trigger guild_settings → NOTIFY guild_settings_changed
```

## Deploying (not applied)

1. Merge; Railway deploys the ingester (migration 0017 runs in pre-deploy).
2. In the Discord developer portal, add the redirect `https://<console domain>/auth/callback`
   to Kiki's application and copy the client secret.
3. Add the `console` service to `.railway/railway.ts` (sketch below), then
   `railway config plan` / `apply`, and generate a public domain for it.
4. Set secrets: `railway variable set --service console DISCORD_CLIENT_SECRET=...`, a shared
   `ADMIN_API_TOKEN` (`openssl rand -hex 32`) on both ingester and console, and
   `CONSOLE_OPERATOR_IDS` (Carlton's Discord user ID) on the console.

```ts
const console = service("console", {
  source: github(REPO, { branch: "main", checkSuites: true }),
  build: { builder: "DOCKERFILE", dockerfilePath: "services/console/Dockerfile",
           watchPatterns: ["services/console/**"] },
  regions: { [REGION]: { replicas: 1 } },
  deploy: { healthcheckPath: "/health", sleepApplication: true, restartPolicyType: "ON_FAILURE" },
  env: {
    DATABASE_URL: db.env.DATABASE_URL,
    PORT: "8095",
    CONSOLE_URL: "https://${{console.RAILWAY_PUBLIC_DOMAIN}}",
    INGESTER_ADMIN_URL: "http://${{ingester.RAILWAY_PRIVATE_DOMAIN}}:8080",
    ADMIN_API_TOKEN: "${{shared.ADMIN_API_TOKEN}}",
    DISCORD_CLIENT_ID: preserve(), DISCORD_CLIENT_SECRET: preserve(),
    CONSOLE_OPERATOR_IDS: preserve(),
  },
});
// ingester env: ADMIN_API_TOKEN: "${{shared.ADMIN_API_TOKEN}}"
```

The console sleeps when idle; the ingester's admin API is on its existing port and private
network only. The IaC change is described rather than applied so that infrastructure changes
stay a deliberate, reviewed step.

## Tests

- Rust: admin API auth (missing/wrong token, disabled without a token), settings read and
  write through the registry, validation errors, prompt overrides and facts in the system
  prompt, fact budget, NOTIFY-driven cache reload from another writer.
- Python (`services/console/tests`): OAuth callback with a mocked Discord (state check,
  permission filtering, operator), session expiry and permission re-check, CSRF rejection,
  guild isolation (an admin of A can't read or write B), dashboard queries on a migrated
  schema, facts/prompts/lingo CRUD and audit rows, settings and chat proxied to a mocked
  ingester.
- Sim (`tools/sim/scenarios/admin-api.yaml`): a settings change through the admin API changes
  the next run's model; a fact and a persona override appear in the next run's prompt; console
  chat returns a reply without posting to Discord.

## Deferred

- OpenTelemetry export (Pydantic AI and Rust `tracing-opentelemetry`) to Railway tracing or
  Langfuse; evals and prompt experiments.
- Editing global settings and models (topic, Decide) from the console; operator ceilings.
- Change history with one-click rollback (the audit log records old values, so it's possible).
- A read-only database role for the console's dashboard queries (Railway gives one superuser
  role today).
- Facts retrieved by relevance instead of all enabled facts in the prompt (needs embeddings;
  see temporal-memory-hld.md); automatic lingo learning (guild-language-learning-hld.md).
- Streaming chat replies; chat with tools disabled; attachments.
- Topic summary cost per window (needs a timestamped cost row per summary).
- Alerting (failure rate, spend thresholds) — Railway or a scheduled job.
