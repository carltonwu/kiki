# Kiki

Discord chat bot. Monorepo layout:

| Path | What |
|---|---|
| `services/ingester/` | Rust service: Discord Gateway, message ingestion, conversation routing |
| `services/agent/` | Python agent host: Pydantic AI, channel history search, durable results |
| `tools/backfill/` | Python one-off: backfill a guild's history into SQLite |
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

The Rust dispatcher (`services/ingester/src/agent/`) claims queued `agent_runs`, builds
context, and sends stable job IDs to the private Python agent host (`services/agent/`).
Python persists execution results in Postgres; Rust delivers them to Discord and tags
replies into the conversation. Topic summaries also run through the Python host. Only
the newest pending run per conversation is answered; older pending runs become `superseded`.

Kiki's conversational clock uses US Eastern (`America/New_York`), including daylight
saving changes. This only affects the time shown in reply context; stored timestamps,
routing windows and search filters keep their existing behavior.

The Python host runs Pydantic AI over OpenRouter once `OPENROUTER_CHAT_API_KEY` is set
(without it, execution returns 501). Replies can search the destination channel's history:
full text plus `qwen/qwen3-embedding-8b` vectors in pgvector, embedded lazily per searched
channel. See [the host setup and contract](services/agent/README.md) for local startup,
shared-token configuration, retries, migrations, and Railway sleeping, and
[the agent tools HLD](docs/agent-tools-hld.md) for the design. No Railway resources are
changed until the IaC is applied.

At most `MAX_ACTIVE_CONVERSATIONS` (8)
conversations are open at once across all channels; opening another closes the least recently
active one (`closed_reason = 'evicted'`).

Slash commands (`services/ingester/src/commands.rs`) are registered per guild at startup. Replies
are only visible to whoever runs them and only cover the server they ran in. `/kiki` shows costs
and topics from every channel, so by default only members with Manage Server can use it; grant
it to other roles under Server Settings → Integrations → Kiki.

| Command | Shows |
| --- | --- |
| `/kiki conversations` | The active pool: channel, messages, people, last activity and cost per conversation, then each one's topic and participants. |
| `/kiki stats` | Conversations, replies, failures and reply latency (p50/p95); messages by routing check; LLM cost (chat, Decide, topics). Each over 24 h, 7 d and all time. |
| `/kiki runs [status] [limit]` | The latest agent runs (default 10, max 25): channel, status, time, cost and age, with error text for failed runs. |
| `/kiki config` | Configured models next to the version that last answered, and the routing and agent settings in effect, by environment variable. |
| `/kiki whois <user>` | Every name Kiki has seen for someone (nickname, display name, username) with first and last seen, plus their message and conversation counts. |

The bot must be invited with the `applications.commands` scope for registration to succeed.

Only one ingester instance holds the Discord Gateway connection at a time: instances take a
Postgres advisory lock first, so during a deploy the new instance waits until the old one has
shut down.

Logs default to INFO: routing decisions with a matching trigger, completed runs and
actionable failures. Set `RUST_LOG=info,sqlx=warn,twilight_http_ratelimiting=warn,ingester=debug` for pool skips,
run claims and retry details, or `LOG_LEVEL=DEBUG` on the Python host for job reuse
and retrieval diagnostics. IDs connect routing pools to runs, host jobs and Discord
replies; logs omit provider error bodies and message content.
