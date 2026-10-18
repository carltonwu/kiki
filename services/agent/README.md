# Kiki agent host

Python 3.13 service managed with Astral `uv`. FastAPI is the HTTP transport and
[Pydantic AI](https://ai.pydantic.dev/) (`agent.py`) runs the model loop over
OpenRouter behind `executor.Executor`. It generates both replies and topic summaries.

Without `OPENROUTER_CHAT_API_KEY`, the host serves `/health`, but authenticated
execution requests return **501**. Keep Rust's `AGENT_HOST_URL` unset until the key
is set.

## Tools and search

Reply jobs get four tools, scoped to the job's guild (every channel Kiki can read) by
trusted run context, never by model arguments ([design](../../docs/agent-tools-hld.md)):

- `search_messages(query, person?, channel?, before?, after?, limit)`: `channel` (a
  name like `#general`, a `<#mention>` or an ID) narrows to one channel. Results carry
  their `channel` and link. `person` is a roster ref (`u2`), an @username or a name,
  resolved to user IDs by exact normalized match
  ([people design](../../docs/identity-hld.md)). Search itself is full text (English, OR
  of terms) plus `qwen/qwen3-embedding-8b` vectors (first 1024 dims, pgvector
  `halfvec`). The two rankings are fused by reciprocal rank, with a recency bonus
  that halves every 30 days. Date filters turn the bonus off.
- `get_message_context(message_id, before, after)`: neighbors in the message's own
  channel and the reply parent.
- `search_emojis(query, limit)`: the server's usable custom emoji by mood or meaning
  (full text over name and description, plus vectors of `:name: description`).
- `find_emoji(name)`: by name (exact, else substring), or an emoji as written in a
  message (`<:huh:123>`) or its ID, which also explains emoji since removed.

Emoji results carry the `<:name:id>` form to write in a reply. Descriptions come
from `POST /v1/emojis/describe` (below).

Results exclude deleted messages, bot messages, and the current conversation
(already in the prompt). A run gets 4 tool calls and 6 model requests. Topic jobs have
no tools. When a reply job starts, the guild's 256 newest pending messages are
embedded in the background, so the first search rarely waits. Results report `semantic: full | partial | off`. A database trigger drops
vectors when messages are edited or deleted. `kiki-agent-host index` embeds all
pending history, then exits; rerun it any time. Embeddings cost about $0.01 per
million tokens.

Logs omit message bodies and provider exception text. At INFO, startup reports
latency, RSS and capacity; executions report job, channel and conversation IDs,
model requests, tool calls, tokens, cost and latency. Tool calls report outcomes,
latency, hits and semantic coverage. Set `LOG_LEVEL=DEBUG` for saved-result reuse,
HTTP job status, indexing and top vector similarity (to calibrate
`retrieval.MIN_SIMILARITY`). Routine HTTP client logs are suppressed at INFO.
The container writes JSON logs to stdout so Railway records their actual severity;
local logs use plain text unless `LOG_FORMAT=json` is set.

## Local setup

From the repository root:

```sh
uv sync --project services/agent --locked
cargo run -p ingester -- migrate
DATABASE_URL=postgres://kiki:kiki@localhost:54329/kiki \
  AGENT_HOST_TOKEN=local-development-token \
  uv run --directory services/agent --locked kiki-agent-host
curl http://localhost:8081/health
```

Locally, the host loads the nearest `.env` (the root one, as Rust does) through the
`python-dotenv` dev dependency; set `OPENROUTER_CHAT_API_KEY` there. Real environment
variables win. The image has no dev dependencies, so Railway uses its variables only. `PORT` defaults to 8081; Rust defaults to 8080. The server uses a
dual-stack socket for IPv4 health checks and Railway's IPv6 private networking.

With `OPENROUTER_CHAT_API_KEY` set, set Rust's `AGENT_HOST_URL=http://localhost:8081`
and the same `AGENT_HOST_TOKEN`. Provider keys belong on the Python service;
Rust retains only its separate Jev decision key. Model names are supplied in
the job request from Rust's `OPENROUTER_CHAT_MODEL` and `OPENROUTER_TOPIC_MODEL`.

## Contract and ownership

`POST /v1/jobs`, with `Authorization: Bearer <AGENT_HOST_TOKEN>`, accepts:

```json
{
  "id": "reply:42",
  "kind": "reply",
  "scope": {"guild_id": "1", "channel_id": "2", "conversation_id": "3"},
  "model": "provider/model",
  "messages": [{"role": "user", "content": "[u1 Alex] hello"}],
  "max_output_tokens": 2000,
  "people": [{"ref": "u1", "user_id": "4", "label": "Alex", "names": ["Alex", "alex_99"]}]
}
```

`kind` is `reply` or `topic`. IDs are `reply:<run_id>` or
`topic:<conversation_id>:<newest_message_id>`. Discord and conversation IDs are
strings on the wire. A completed response is:

```json
{
  "id": "reply:42",
  "status": "completed",
  "result": {
    "text": "A reply",
    "model": "provider/model",
    "steps": 1,
    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "cost": 0.0001}
  },
  "error": null
}
```

The host persists input and results in `agent_host_jobs`, created by Rust's
migrations. Matching retries return the saved result. A session advisory lock
prevents simultaneous execution of one ID; a duplicate in progress returns 202
with `status: running`. Reusing an ID with changed input returns 409. Execution
failures are persisted with `status: failed`; transient failures (provider
429/5xx or network, database errors, timeouts) are not persisted and return 503. Authentication errors return 401. The executor must return `JobResult` and
must not send messages to Discord.

Rust owns queue polling, wake-up retries, prompt snapshots, and Discord delivery.
It retries cold-start 502s, temporary 503/504s, 429s and in-progress responses
within `AGENT_HOST_TIMEOUT_MS` (180000 by default). Startup resumes interrupted
execution from the saved request. Before a Discord send, it records `delivering`;
interrupted deliveries are failed rather than automatically resent. This can
omit a reply if a crash happens before the send; it avoids blind duplicate posts.
Legacy runs already in progress when the migration runs are also failed.

If the Python process dies before saving a result, inference may repeat on retry.
Current tools only read; future tools with side effects or costs must tolerate replay.
Job tables contain private prompts and replies and currently have no automatic
retention policy.

### Emoji descriptions

`POST /v1/emojis/describe` (same bearer token) describes one batch of a server's
custom emoji that `/kiki emojis resync:true` copied into `guild_emojis`:

```json
{"guild_id": "1", "model": "google/gemini-2.5-flash-lite",
 "attempted_before_ms": 1792000000000, "redescribe": false}
```

It claims up to 8 emoji not tried since `attempted_before_ms` (the database clock
when the resync started), downloads each as a 128 px PNG from Discord's CDN
(`DISCORD_CDN_URL` overrides it for the simulator), asks the model for a short
description, then embeds the described ones. The response is
`{"described", "failed", "remaining", "cost"}`; Rust repeats the call until
`remaining` is 0 or nothing progresses. A failed emoji (download or model) waits
for the next resync. It's not a durable job: writes are idempotent, so a retry
after 503 simply continues. It shares the job concurrency slots and timeout.

## Railway and idle resources

The repository's IaC defines an `agent` service with Serverless enabled, one
replica, one Python process, and no public domain. Apply migrations on the Rust
service before enabling dispatch. Create a shared secret named `AGENT_HOST_TOKEN`
in the Railway environment, then set the same reference on both services (already
declared in IaC). Once `OPENROUTER_CHAT_API_KEY` is set, set Rust's `AGENT_HOST_URL` to
`http://${{agent.RAILWAY_PRIVATE_DOMAIN}}:8081`.

Build with `docker build -f services/agent/Dockerfile .`. Dependencies are installed
from `uv.lock` at build time; the container runs Python directly as an unprivileged
user. Do not install dependencies or run `uv sync` on cold start.

The host has no database poller, connection pool, idle heartbeat, model warm-up,
or telemetry exporter. Connections open during requests and close afterwards.
`/health` checks only process liveness and reports `executor_configured` without
contacting Postgres. Avoid external uptime probes that keep the service awake.
Rust wakes it only for actual jobs, never for periodic health checks.

`AGENT_HOST_MAX_CONCURRENT_JOBS` defaults to 2; extra requests return 503 for Rust
to retry. `AGENT_HOST_JOB_TIMEOUT_SECS` defaults to 120. Railway sleeping and
cold-start behavior should be verified after deployment from the startup log line.
Locally, startup takes about 0.6 s and 90 MB RSS with the SDK loaded.

## Checks

```sh
uv run --project services/agent --locked ruff check services/agent
uv run --project services/agent --locked ruff format --check services/agent
TEST_DATABASE_URL=postgres://kiki:kiki@localhost:54329/kiki \
  uv run --directory services/agent --locked pytest
```

Integration tests apply every Rust migration in an isolated schema (the database
needs pgvector, as in `docker-compose.yml`). They cover durable result replay,
concurrent duplicates, conflicting input, bounded capacity, failure and transient
handling, cancellation recovery, and connection cleanup. They also cover hybrid
search scope, recency, edit/delete invalidation, embedding outages, and an executor
run with Pydantic AI's `TestModel` and fake embeddings. They never call providers.
Without `TEST_DATABASE_URL`, only the HTTP checks run.
