# Kiki

Discord chat bot. Monorepo layout:

| Path | What |
|---|---|
| `services/ingester/` | Rust service: Discord Gateway, message ingestion, conversation routing |
| `services/agent/` | Python agent host: Pydantic AI, server history search, durable results |
| `tools/backfill/` | Python one-off: backfill a guild's history into SQLite |
| `tools/sim/` | Local Discord and OpenRouter simulator for end-to-end checks |
| `.railway/railway.ts` | Railway infrastructure as code (Postgres + services) |
| `docker-compose.yml` | Local Postgres (with pgvector) |

## Local development

```sh
cp .env.example .env
docker compose up -d --wait           # Postgres 18 + pgvector on localhost:54329
cargo run -p ingester -- migrate      # apply migrations
cargo run -p ingester                 # run; health check at http://localhost:8080/health
```

Add a migration: create `services/ingester/migrations/NNNN_description.sql`. Migrations are
embedded in the binary and applied by `ingester migrate`, which Railway runs before each deploy.

## Local simulator

With Docker Compose, Rust and `uv` installed, run the real services against a fake
Discord server and stub model responses, without production credentials:

```sh
tools/sim/kiki-sim up
tools/sim/kiki-sim test
tools/sim/kiki-sim say --as alex --in general "kiki hi"
tools/sim/kiki-sim slash --as alex config key=history_messages
tools/sim/kiki-sim down
```

The simulator uses its own Postgres on port 54339 and stores state and logs in
`.sim/`. `up` rebuilds the ingester; each scenario resets the simulated database
and guild. `down` retains the database container; `down --purge` removes it and
its volume. `up --live` uses real OpenRouter keys and incurs provider charges,
while Discord stays simulated. The optional `KIKI_SIM_POSTGRES` override must
point to a localhost server with the simulator's `sim_marker` table.

After changing `services/`, run `up` again, run the scenarios and the service
checks in `.github/workflows/ci.yml`, and add a regression scenario. CI also runs
the simulator in stub mode. See [the simulator design and CLI](docs/kiki-sim-hld.md)
for fixtures, scripted responses, live mode and debugging commands.

## Deploying (Railway)

Infrastructure is defined in `.railway/railway.ts`. Services build from GitHub on push to `main`,
after the GitHub Actions checks in `.github/workflows/ci.yml` pass (a failing check skips the deploy).

```sh
cd .railway && npm install && cd ..
railway link                          # select the project
# Add a shared AGENT_HOST_TOKEN in the Railway environment before applying the new service.
railway config plan                   # review changes
railway config apply                  # apply them
railway variable set --service ingester DISCORD_BOT_TOKEN=...
railway variable set --service agent OPENROUTER_CHAT_API_KEY=...
railway ssh --service agent kiki-agent-host index   # optional: embed existing history
railway variable set --service ingester 'AGENT_HOST_URL=http://${{agent.RAILWAY_PRIVATE_DOMAIN}}:8081'
```

On start, the ingester connects to the Gateway and stores live message, edit, delete
and channel events. Each `GUILD_CREATE` starts a REST catch-up in the background,
resuming from each channel's `complete_through` cursor. The first pass fills history;
later passes fill gaps. Live chat stays available while history is fetched.

After a message is stored, a Postgres trigger notifies the router (`services/ingester/src/router/`),
which pools rapid messages per channel and user, classifies them (reply to Kiki or @Kiki, keyword,
active conversation, then Jev via OpenRouter to decide), and records the result: every classified message gets a
`classifications` row, messages that join a conversation get a `conversation_messages` row, and
routed pools are queued in `agent_runs`. Messages older than `ROUTE_MAX_MESSAGE_AGE_MS` (5 s) at
classification time are never classified, so backfill and catch-up don't trigger responses.
Each Jev request includes up to eight open conversations in the current channel, most recently
active first, with no idle-time cutoff.
A direct follow-up can continue without Jev when Kiki just answered its author,
no other eligible human has spoken since, and the message asks a question or uses
second-person language without mentioning someone else. Keyword-triggered Jev
decisions use a lower confidence threshold (0.25 by default). An uncertain answer
can continue a participant's conversation Kiki answered within the last five
minutes. A trigger word at the start or end of a question or second-person message
can also override Jev's `no`, continuing a recent conversation or starting one.
Server admins can tune `decide_keyword_min_confidence`, `follow_up_window` and
`follow_up_max_no_confidence` through `/kiki config`.

The Rust dispatcher (`services/ingester/src/agent/`) claims queued `agent_runs`, builds
context, and sends stable job IDs to the private Python agent host (`services/agent/`).
Python persists execution results in Postgres; Rust delivers them to Discord and tags
replies into the conversation. Topic summaries also run through the Python host. Only
the newest pending run per conversation is answered; older pending runs become `superseded`.

Kiki's conversational clock uses US Eastern (`America/New_York`), including daylight
saving changes. This only affects the time shown in reply context; stored timestamps,
routing windows and search filters keep their existing behavior.

The Python host runs Pydantic AI over OpenRouter once `OPENROUTER_CHAT_API_KEY` is set
(without it, execution returns 501). Replies can search ingested history across the
job's server, optionally narrowed to one channel: full text plus
`qwen/qwen3-embedding-8b` vectors in pgvector, embedded lazily for that server. Results cite their source channel and message link. See
[the host setup and contract](services/agent/README.md) for local startup,
shared-token configuration, retries, migrations, and Railway sleeping, and
[the agent tools HLD](docs/agent-tools-hld.md) for the design. No Railway resources are
changed until the IaC is applied.

Each server keeps at most `max_active_conversations` (default 8) conversations open
across its channels; opening another closes the least recently
active one (`closed_reason = 'evicted'`).

Slash commands (`services/ingester/src/commands.rs`) are registered per guild at startup. Replies
are only visible to whoever runs them and only cover the server they ran in. `/kiki` shows costs
and topics from every channel, so by default only members with Manage Server can use it; grant
it to other roles under Server Settings → Integrations → Kiki.

| Command | Shows |
| --- | --- |
| `/kiki conversations` | The active pool: channel, messages, people, last activity and cost per conversation, then each one's topic and participants. |
| `/kiki clear` | Closes every active conversation in the server (`closed_reason = 'cleared'`), so the next message starts fresh. History and costs stay. Requires Manage Server. |
| `/kiki stats` | Conversations, replies, failures and reply latency (p50/p95); messages by routing check; LLM cost (chat, Decide, topics). Each over 24 h, 7 d and all time. |
| `/kiki runs [status] [limit]` | The latest agent runs (default 10, max 25): channel, status, time, cost and age, with error text for failed runs. |
| `/kiki config [key] [value] [reset]` | View effective server settings, their source and bounds. Use `key` + `value` to edit or `key` + `reset:true` to inherit the current default. Writes require Manage Server. |
| `/kiki users [period] [sort]` | Top 20 active people by cost (default), messages or conversations. Period: `24h`, `7d` (default) or `all`. Shows total messages, messages with Kiki, conversations and attributed LLM cost. |
| `/kiki whois <user>` | Up to 15 recently seen names (nickname, display name, username), first/last seen, join and message dates, plus messages, conversations and attributed cost over 24 h, 7 d and all time. |

Per-person cost splits reply cost across the triggering messages and includes Decide;
it excludes topic summaries. `/kiki runs` accepts `pending`, `running`, `delivering`,
`done`, `failed` or `superseded` as its status filter.

The bot must be invited with the `applications.commands` scope for registration to succeed.

Server settings persist in Postgres and apply without a restart. Examples:

- `/kiki config key:pool_debounce value:750ms`
- `/kiki config key:history_messages value:50`
- `/kiki config key:reply_max_tokens value:3000`
- `/kiki config key:chat_model value:deepseek/deepseek-v4.1-flash-1017`
- `/kiki config key:image_model value:google/gemini-2.5-flash-image` (must output images)
- `/kiki config key:image_generation value:false` (stop Kiki drawing images)
- `/kiki config key:pool_debounce reset:true`

Use `/kiki config key:chat_model` to view the chat model, or add `reset:true` to restore
the current default. Model IDs must be listed by OpenRouter and
support text output and tools. Unset keys inherit startup env defaults, then built-in defaults.
New pools capture routing settings; new reply jobs capture context, model and token settings.
In-flight jobs retain their saved model input. Topic summaries and Decide keep their global
models. See [server settings](docs/server-settings-hld.md) for the inventory and reviewed scope.

Kiki can draw: with `image_generation` on (the default), reply jobs get a
`generate_image` tool backed by the server's `image_model`, and the picture is
attached to her first message. See [image generation](docs/image-generation.md).

Server phrases and custom emoji come from the manually curated `guild_lingo`
table (migration 0014 seeds one server). Reply prompts include only that server's
entries. `/kiki config key:lingo_nudge value:35` sets how often a reply gets an
extra suggestion to use one phrase (default 35%, range 0–100). Setting it to 0
removes the extra suggestion; the phrase list remains in the prompt, so natural
use is still possible. Selection is stable for the triggering message and retries.
There is no automatic phrase learning or Discord command to edit the phrase list.

Only one ingester instance holds the Discord Gateway connection at a time: instances take a
Postgres advisory lock first, so during a deploy the new instance waits until the old one has
shut down.

Logs default to INFO: routing decisions with a matching trigger, completed runs and
actionable failures. Set `RUST_LOG=info,sqlx=warn,twilight_http_ratelimiting=warn,ingester=debug` for pool skips,
run claims and retry details, or `LOG_LEVEL=DEBUG` on the Python host for job reuse
and retrieval diagnostics. IDs connect routing pools to runs, host jobs and Discord
replies; logs omit provider error bodies and message content.
