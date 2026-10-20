# Observability

Both services export OpenTelemetry traces, metrics and logs over OTLP/HTTP
(protobuf) straight to Grafana Cloud. Export is off unless
`OTEL_EXPORTER_OTLP_ENDPOINT` is set, so tests, CI, the simulator and local runs
send nothing and log to stdout as before.

## Architecture

```
ingester (Rust) ──OTLP/HTTP──┐
                             ├─▶ Grafana Cloud OTLP gateway ─▶ Mimir (metrics) · Loki (logs) · Tempo (traces)
agent host (Python) ─OTLP────┘
```

- **No collector.** A collector would be a third always-on Railway service, billed
  for its memory and CPU around the clock, to do what the SDKs already do:
  batch and send. Each service talks to the gateway directly.
- **No Railway log drain.** Logs go out through the same OTLP pipeline, already
  correlated with traces (`trace_id`/`span_id`). Railway's own log view keeps the
  stdout copy.
- **The agent host sleeps.** Railway sleeps it (`sleepApplication`) only after 10
  minutes without outbound packets, so it never exports on a timer. Its metric
  reader has an infinite interval (no thread at all), and the API flushes all
  three signals after each job request (`/v1/*`, success or error), off the event
  loop, once the response is out; flushes requested while one runs coalesce into
  one more. The span and log batch processors wake every few seconds but send
  only when they hold something. Shutdown (SIGTERM, via the app's lifespan, and
  the end of `kiki-agent-host index`) stops scheduling flushes and flushes once
  more, all three signals in parallel within 4 s (each export request times out
  after 3 s): uvicorn may have used most of Railway's 30 s drain by then. That
  is the only wait at exit: per-job flushes run on daemon threads, and the
  providers register no atexit handlers (both would drain batches unbounded).
  HTTP spans use the stable semantic conventions only (`OTEL_SEMCONV_STABILITY_OPT_IN`
  is forced to `http`; `http/dup` would add the legacy `http.url`).
  So the host's metric samples are sparse: a counter has a sample only after a
  job, and an instant query goes stale five minutes later. Query them over a
  range (`increase(kiki_agent_jobs_total[$__range])`, `last_over_time(…[1h])`).
- **The ingester runs always**, so it exports normally: spans and logs in batches,
  metrics every 60 s. Its main flushes and stops the exporters on every exit path,
  all three in parallel within 5 s (3 s per export request), so a stalled one
  can't hold back the others, such as the final `kiki.leader.held = 0`.
  `ingester migrate` (the pre-deploy step) doesn't export. Stdout is unchanged:
  the console layer sees events only, never span names or fields.
- **Temporality is cumulative**, what Mimir (Prometheus) expects.
- **Traces:** the ingester exports only its own spans (`tracing` spans from the
  `ingester` crate; dependencies' spans, like one per twilight REST call, would
  each start a trace). An agent run is the `agent_run` span; its requests to the
  agent host carry its W3C `traceparent` (and only those requests), so the host's
  FastAPI span (`/health` excluded) joins the same trace. The host extracts trace
  context but never injects it: its outbound calls (OpenRouter, Discord, fetched
  links) are third parties. They are still client spans (httpx, and httpx2 for the
  OpenAI SDK), with URLs stripped of credentials, query and fragment (Discord's
  signed attachment links carry tokens there). GenAI instrumentation (pydantic-ai,
  logfire) stays off: no prompt or reply text goes into telemetry.
- **Logs:** the ingester bridges `tracing` events (`opentelemetry-appender-tracing`),
  the agent host the root `logging` logger. Neither exports the exporter's own logs
  (`opentelemetry*`, and its HTTP clients: reqwest/hyper/h2 in Rust,
  urllib3/requests in Python), which would otherwise feed back into the export.
  The agent host never exports HTTP client loggers (`httpx`, `httpx2`,
  `httpcore`, `httpcore2`, `h11`, `openai`) at any `LOG_LEVEL`: their lines hold
  signed URLs and, at DEBUG, request bodies. The agent's JSON stdout lines carry
  `trace_id`/`span_id` inside a span; the ingester's logs get them in Loki only.

Code: `services/ingester/src/telemetry.rs`, `services/agent/src/kiki_agent_host/telemetry.py`.
Each exposes a meter (`telemetry::meter()`, `telemetry.meter()`) that is a no-op
when export is off; record through it from anywhere.

## Configuration

| Variable | Where | Value |
|---|---|---|
| `OTEL_EXPORTER_OTLP_ENDPOINT` | both | Grafana Cloud's gateway, `https://otlp-gateway-prod-us-east-2.grafana.net/otlp` (in `.railway/railway.ts`). Unset: no export. |
| `OTEL_EXPORTER_OTLP_PROTOCOL` | both | `http/protobuf` (the only one built in). |
| `OTEL_EXPORTER_OTLP_HEADERS` | both, secret | `Authorization=Basic%20<base64(instance ID:token)>`. Both SDKs percent-decode it. `tools/railway-sync-env` derives it from `GRAFANA_CLOUD_ID` and `GRAFANA_APP_TOKEN` in the local `.env` and never prints it. |
| `OTEL_SERVICE_NAME` | both | `ingester` / `agent` (also the defaults). |
| `OTEL_SDK_DISABLED=true` | both | Kill switch. |
| `GRAFANA_CLOUD_ID`, `GRAFANA_APP_TOKEN`, `GRAFANA_CLOUD_INGESTION_URL` | local `.env` only | Grafana Cloud stack details; not synced to Railway themselves. |

Resource attributes: `service.name`, `service.namespace=kiki`, `service.version`
(`RAILWAY_GIT_COMMIT_SHA`), `deployment.environment.name`
(`RAILWAY_ENVIRONMENT_NAME`, else `local`), `service.instance.id` (the service
name: each runs as a single instance, and a random per-process or per-replica ID
would start new series on every restart, deploy and wake of the agent host). In Grafana Cloud
metrics carry `job="kiki/<service>"` and `instance=<service.instance.id>`; the
other resource attributes are on `target_info`.

## Seeing it locally

```
docker run --rm -p 3000:3000 -p 4318:4318 grafana/otel-lgtm
```

Then set `OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318` for a service, or
run the simulator with `KIKI_SIM_OTEL_ENDPOINT=http://localhost:4318
tools/sim/kiki-sim up` (localhost only; off by default), and open Grafana on
http://localhost:3000 (Explore: Tempo, Loki, Prometheus).

## Cardinality rules

The free plan allows 10k active series (plus 50 GB each of logs and traces, 14
days). Every distinct label combination is a series, and each histogram bucket
is one more.

- **Never** put guild, channel, user, message, run, conversation or job IDs, URLs,
  model output or any other free text on a metric attribute. Those go on spans
  and log lines, where they cost nothing per value.
- Attributes are bounded enums only: `purpose`, `kind`, `outcome`, `result`,
  `status`, `reason`, `tool`, `check`, `model` (models are a handful per
  deployment), `queue`, `table` (a fixed allowlist), `direction`, `state`.
  Unknown values map to `other`.
- Histograms use explicit, few buckets (about 6 to 10), not the SDK defaults:
  - durations (s): `0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120`
  - latencies to a reply (s): `1, 2, 5, 10, 20, 40, 80`
  - ratios and confidences: `0.1, 0.25, 0.5, 0.75, 0.9, 1`
  - counts (messages, model requests): `1, 2, 5, 10, 20, 50`
- Instrumentation-made metrics are kept to what we read: the agent host keeps
  FastAPI's `http.server.request.duration` and `http.server.active_requests`
  (three routes) and drops the body-size histograms; httpx client metrics are off
  (read_link's hosts are unbounded). The ingester makes none.
- A new metric's series estimate (attribute values multiplied, times buckets + 2
  for histograms) goes in its PR description.

## Naming

- OTel names, dot-separated, under `kiki.`: `kiki.<area>.<thing>`. A count of
  things is plural (`kiki.agent_runs.finished`); a per-item measurement is
  singular (`kiki.agent_run.duration`). Words within a segment use `_`.
- Units in UCUM: `s` (never ms), `By`, `1` for ratios, `{thing}` annotations for
  counts (`{run}`, `{message}`). No unit in the name itself.
- Grafana Cloud translates to Prometheus names: `.` becomes `_`, the unit adds a
  suffix (`s` → `_seconds`, `By` → `_bytes`, `1` → `_ratio` on gauges, `{…}`
  nothing), and counters get `_total`. Histograms become `_bucket`, `_sum` and
  `_count` series.
- Follow semantic conventions where one exists (`http.*`, `gen_ai.*`
  attribute names), except where they'd cost series (see `kiki.llm.tokens`).

## Metric catalog

The contract for the instrumenting PRs. Status: **planned** unless marked
**live**. Attribute values listed are the full set.

### Shared (both services)

| Metric | Type | Unit | Attributes | Prometheus |
|---|---|---|---|---|
| `kiki.llm.calls` | counter | `{call}` | `purpose`, `model`, `result=ok\|error\|timeout` | `kiki_llm_calls_total` |
| `kiki.llm.cost_usd` | counter | `{USD}` | `purpose`, `model` | `kiki_llm_cost_usd_total` |
| `kiki.llm.tokens` | counter | `{token}` | `purpose`, `model`, `type=input\|output` | `kiki_llm_tokens_total` |
| `kiki.llm.duration` | histogram | `s` | `purpose`, `model` | `kiki_llm_duration_seconds_*` |

`purpose` ∈ `jev, reply, topic, vision, embedding, image, emoji, failure_note`.
Which service made the call is the `job` label, so there is no `service`
attribute. Cost comes from OpenRouter's `usage.cost`. Tokens are a counter, not
semconv's `gen_ai.client.token.usage` histogram: per-call distributions aren't
worth ten buckets per purpose and model; rates and totals are what we read.
Cost keeps the currency in its name: the annotation unit `{USD}` adds no suffix.
`kiki.llm.*` is recorded where the model is called: the ingester calls only Jev
(`purpose=jev`, **live**: calls, cost and duration; the Decisions API reports no
token counts), and the agent host every other purpose, including the topic,
drawing and emoji jobs the ingester asks it for (never counted again from their
results). `model` is the model the provider reports (`~typesafe/jev-latest`),
else the configured one; anything not shaped like a model ID is `other`.
Admins choose models and aliases resolve to new versions, so `model` isn't an
allowlist but capped per process: the first 16 distinct values keep their name,
later ones are `other` (`metrics::Capped` in the ingester; the agent host does
the same).

In the agent host these are **live** for every purpose but `jev` (the ingester's).
Each is recorded where the call is made, with the model actually called (the
vision model, or the chat model standing in for it; the fallback image model).
The host keeps the first 16 distinct model IDs per process as `model` labels
(here and on `kiki.images.*`); later ones are `other`, while spans keep the ID.
A reply or topic run is one call however many model requests it makes:
`duration` is the whole run, its tools' time included, `cost_usd` its billed cost
with OpenRouter's web fees, and `tokens` its requests' sum (the requests per run
are in `kiki.agent.job.model_requests`). Vision, embedding and drawing calls made
during a reply count under their own purposes. An image attempt that doesn't draw
(a safety block included) is an `error`; the Images API reports no tokens, so
`image` has cost only. `timeout` is a client or tool deadline. A call given up
on (indexing a reply stopped waiting for, a run past the job's deadline) has no
result, so it is only a span (`error.type=CancelledError`); a run cut off that
way still shows as an `interrupted` job.

### Ingester

| Metric | Type | Unit | Attributes | Prometheus |
|---|---|---|---|---|
| `kiki.gateway.events` | counter | `{event}` | `type` (Discord's dispatch name, e.g. `MESSAGE_CREATE`, for the 15 handled; else `other`) | `kiki_gateway_events_total` — **live** |
| `kiki.gateway.reconnects` | counter | `{reconnect}` | `reason=requested\|invalid_session\|transport\|closed` | `kiki_gateway_reconnects_total` — **live** |
| `kiki.gateway.lag` | histogram | `s` | | `kiki_gateway_lag_seconds_*` — **live** |
| `kiki.gateway.store_errors` | counter | `{error}` | | `kiki_gateway_store_errors_total` — **live** |
| `kiki.messages.ingested` | counter | `{message}` | `author=human\|bot\|kiki` | `kiki_messages_ingested_total` — **live** |
| `kiki.router.admissions` | counter | `{message}` | `result=admitted\|skipped`, `reason=none\|bot\|other_guild\|too_old\|channel_not_allowed\|unparseable` | `kiki_router_admissions_total` — **live** |
| `kiki.router.classifications` | counter | `{pool}` | `check=reply\|mention\|keyword\|active_conversation\|none`, `outcome=continue\|new\|no` | `kiki_router_classifications_total` — **live** |
| `kiki.jev.confidence` | histogram | `1` | | `kiki_jev_confidence_*` — **live** |
| `kiki.agent_runs.queued` | counter | `{run}` | `kind=reply\|mention\|keyword\|active_conversation\|other` | `kiki_agent_runs_queued_total` — **live** |
| `kiki.agent_runs.finished` | counter | `{run}` | `kind`, `status=done\|failed\|superseded\|expired\|interrupted` | `kiki_agent_runs_finished_total` — **live** |
| `kiki.agent_run.duration` | histogram | `s` | `kind` | `kiki_agent_run_duration_seconds_*` — **live** |
| `kiki.agent.workers.active` | gauge | `{worker}` | | `kiki_agent_workers_active` — **live** |
| `kiki.agent_host.requests` | counter | `{request}` | `endpoint=jobs\|images\|emojis`, `status` (HTTP code, or `error`) | `kiki_agent_host_requests_total` — **live** |
| `kiki.agent_host.retries` | counter | `{retry}` | `reason=timeout\|connection\|http\|running` | `kiki_agent_host_retries_total` — **live** |
| `kiki.drawings.finished` | counter | `{drawing}` | `outcome=drawn\|refused\|failed\|expired\|interrupted` | `kiki_drawings_finished_total` — **live** |
| `kiki.conversations.started` | counter | `{conversation}` | | `kiki_conversations_started_total` — **live** |
| `kiki.conversations.closed` | counter | `{conversation}` | `reason=evicted\|cleared` | `kiki_conversations_closed_total` — **live** |
| `kiki.conversation.messages` | histogram | `{message}` | (recorded at close) | `kiki_conversation_messages_*` — **live** |
| `kiki.discord.messages_sent` | counter | `{message}` | `kind=reply\|drawing` | `kiki_discord_messages_sent_total` — **live** |
| `kiki.discord.send_errors` | counter | `{error}` | `kind` | `kiki_discord_send_errors_total` — **live** |
| `kiki.discord.reactions` | counter | `{reaction}` | `result=ok\|error` | `kiki_discord_reactions_total` — **live** |
| `kiki.discord.rest.retries` | counter | `{retry}` | `reason=rate_limit\|server\|network` | `kiki_discord_rest_retries_total` — **live** |
| `kiki.reply.latency` | histogram | `s` | | `kiki_reply_latency_seconds_*` — **live** |
| `kiki.leader.held` | gauge | | | `kiki_leader_held` — **live** |
| `kiki.db.pool.connections` | gauge | `{connection}` | `state=used\|idle` | `kiki_db_pool_connections` — **live** |

`kiki.reply.latency` (trigger message created → first reply sent) is the
headline SLO: from the Discord creation time of the message the reply answers
(its snowflake) to the first message of the reply sent; reaction-only and
silent runs have none, and drawings' follow-ups don't count. `kiki.gateway.lag`
is Discord's message timestamp → receipt, for live `MESSAGE_CREATE`s.
`kiki.leader.held` is 1 while the instance holds the leader lock, 0 after release.

- Gateway: `reconnects` counts dropped connections by cause: Discord asked
  (`requested`, opcode 7), invalidated the session (`invalid_session`, opcode
  9), the connection broke (`transport`, 1006 or none), or Discord closed it with
  a code (`closed`). Whether the shard then resumed or started a new session is
  `kiki.gateway.events{type="RESUMED"|"READY"}`. `messages.ingested` counts live
  `MESSAGE_CREATE`s (catch-up pages aren't counted).
- Router: an admission is a stored message the router considered (every
  `message_inserted` notification); `classifications` counts tagged pools.
  Pools dropped as too old or all deleted aren't classifications.
- Agent runs: `kind` is the routing check that queued the run (its first
  trigger message's `classifications.check_hit`; `other` when unknown).
  `expired` runs are older than `AGENT_RUN_MAX_AGE_SECS` (stored as `failed`);
  `interrupted` ones were cut off mid-delivery and failed at startup. Runs and
  drawings are counted once their final status is stored, so none counts twice.
  `duration` is claim → finished, so queueing is in `kiki.reply.latency` only.
- Agent host: one `requests` sample per POST attempt; `retries` by why the job
  is asked again (`running`: the host is still on it).
- Drawings: `refused` is the host's `refused` (the image model and its fallback
  wouldn't draw it); `failed` includes host and Discord errors.
- Discord: `reactions` are the ones Kiki chooses (not the pending emoji).
  `rest.retries` are `rest::get_json` retries (sends aren't retried).

Series, at most (attribute values multiplied, histograms × buckets + 2): about
225, of which `kiki.agent_run.duration` is 60. Seen in the simulator after a
few replies and drawings: 96.

**Traces.** Spans carry IDs as attributes, never message text: `message_create`
(a live message's storage) → `classify` (with `jev` and `tag`) → `agent_run` →
`agent_host_request` per attempt (the agent host's `POST /v1/…` joins under it),
`discord_send` per message, `topic_refresh` and `drawing`. `catch_up` is one span
per run. Other Gateway events get no span. The router learns of a message from a
Postgres notification, so the Gateway span's context is handed over in memory
(bounded; the router runs in the same process). Runs and drawings are queued in
Postgres: `agent_runs.trace_context` and `agent_image_jobs.trace_context` hold
the queuing span's W3C `traceparent` (NULL when export is off), and the task that
picks the row up starts its span as that span's child (and links the runs it
superseded), so one trace runs from
the message to Kiki's reply and her drawing. A missing or malformed value starts
a new trace.

### Agent host

| Metric | Type | Unit | Attributes | Prometheus |
|---|---|---|---|---|
| `kiki.agent.jobs` | counter | `{job}` | `kind=reply\|topic\|image`, `outcome=completed\|failed\|running\|interrupted` | `kiki_agent_jobs_total` — **live** |
| `kiki.agent.job.duration` | histogram | `s` | `kind` | `kiki_agent_job_duration_seconds_*` — **live** |
| `kiki.agent.jobs.rejected` | counter | `{job}` | `reason=busy\|conflict` | `kiki_agent_jobs_rejected_total` — **live** |
| `kiki.agent.job.model_requests` | histogram | `{request}` | | `kiki_agent_job_model_requests_*` — **live** |
| `kiki.images.attempts` | counter | `{attempt}` | `model`, `result=drawn\|safety_blocked\|refused\|failed` | `kiki_images_attempts_total` — **live** |
| `kiki.images.fallback` | counter | `{attempt}` | `result=drawn\|failed` | `kiki_images_fallback_total` — **live** |
| `kiki.images.duration` | histogram | `s` | `model` | `kiki_images_duration_seconds_*` — **live** |
| `kiki.tools.calls` | counter | `{call}` | `tool`, `outcome=ok\|error\|timeout\|refused\|invalid_arguments\|budget_exhausted` | `kiki_tools_calls_total` — **live** |
| `kiki.tools.duration` | histogram | `s` | `tool` | `kiki_tools_duration_seconds_*` — **live** |
| `kiki.web.calls` | counter | `{call}` | `kind=search\|fetch` | `kiki_web_calls_total` — **live** |

`kiki.agent.jobs` counts answered job requests (`running`: another request holds
the same job ID); `interrupted` ones return 503 and are retried with the same ID.
Busy and conflicting requests go to `kiki.agent.jobs.rejected` instead. The
emoji-description endpoint is not a job.

- Every count is per request, not per job: the ingester retries a job with the
  same ID after a 503 (`interrupted`, or `busy`) and polls a `running` one, so one
  job can count as `interrupted` and then `completed`, and a retry answered from
  the saved result counts again.
- `kiki.agent.job.duration` is the time spent running the job, per attempt that
  ran it (a retry answered from the saved result, or turned away as running,
  didn't). `kiki.agent.jobs.rejected{reason}`: `busy` is the 503 when no slot frees
  within 0.1 s, `conflict` the 409 for a job ID reused with different input.
- `kiki.agent.job.model_requests` is per finished reply or topic run.
- `kiki.images.attempts` is per model attempt, with the error the drawing reports
  for it: `safety_blocked` (the model's safety filter), `refused` (no usable image,
  or another 4xx), `failed` (5xx, network, timeout, bad prompt). A safety block
  tries the fallback model (`OPENROUTER_IMAGE_FALLBACK_MODEL`), if one is set;
  `kiki.images.fallback` counts those attempts by whether they drew.
- `kiki.tools.calls` is per call through the tool guard (`agent/guard.py`).
  `tool` is one of the reply agent's fixed tools (`search_messages`, `recall`,
  …). `error` is an error result or an exception; `budget_exhausted` a call
  turned away by the per-reply budget, which never ran, so has no duration. As
  for model calls, a cancelled tool call or image attempt is only a span.
- `kiki.web.calls` is OpenRouter's web searches and fetches per reply run, as its
  server tool usage reports them.

Agent host spans (no prompt, reply, message or SQL values in any; a failure keeps
its `error.type` only, never the exception's message):

- `llm <purpose>`, one per model call or run: `kiki.purpose`,
  `gen_ai.request.model`, `gen_ai.usage.input_tokens`/`output_tokens`,
  `kiki.cost_usd`, `kiki.llm.result`. Vision (`llm vision`), embedding and the
  reply run itself (`llm reply`, with its tools' spans inside) included.
- `image attempt`, one per image model attempt: the same, plus `kiki.image.result`.
- `execute_tool <tool>`: `gen_ai.tool.name`, `kiki.tool.outcome`.
- Postgres queries (psycopg instrumentation), named by operation (`SELECT`), with
  the SQL text (`db.statement`, placeholders only: parameters aren't captured,
  and no query inlines a value). Only inside a request's trace, so the `index`
  command doesn't start one per query.

The agent host's log lines keep their `key=value` text, so Loki parses them with
`| logfmt` (`{service_name="agent"} | logfmt | outcome="timeout"`).

### Platform poller (ingester, leader only, every 60 s)

| Metric | Type | Unit | Attributes | Prometheus |
|---|---|---|---|---|
| `kiki.railway.cpu` | gauge | `{vCPU}` | `service` | `kiki_railway_cpu` |
| `kiki.railway.memory` | gauge | `By` | `service` | `kiki_railway_memory_bytes` |
| `kiki.railway.network` | gauge | `By` | `service`, `direction=rx\|tx` | `kiki_railway_network_bytes` |
| `kiki.railway.disk` | gauge | `By` | `service` | `kiki_railway_disk_bytes` |
| `kiki.pg.connections` | gauge | `{connection}` | `state` (`pg_stat_activity` states) | `kiki_pg_connections` |
| `kiki.pg.database.size` | gauge | `By` | | `kiki_pg_database_size_bytes` |
| `kiki.pg.table.size` | gauge | `By` | `table` (allowlist) | `kiki_pg_table_size_bytes` |
| `kiki.pg.transactions` | counter | `{transaction}` | `result=commit\|rollback` | `kiki_pg_transactions_total` |
| `kiki.pg.cache.hit_ratio` | gauge | `1` | | `kiki_pg_cache_hit_ratio` |
| `kiki.pg.deadlocks` | counter | `{deadlock}` | | `kiki_pg_deadlocks_total` |
| `kiki.pg.temp.written` | counter | `By` | | `kiki_pg_temp_written_bytes_total` |
| `kiki.pg.oldest_transaction.age` | gauge | `s` | | `kiki_pg_oldest_transaction_age_seconds` |
| `kiki.pg.dead_tuples` | gauge | `{tuple}` | `table` (allowlist) | `kiki_pg_dead_tuples` |
| `kiki.queue.pending` | gauge | `{item}` | `queue=agent_runs\|drawings` | `kiki_queue_pending` |
| `kiki.queue.oldest_pending.age` | gauge | `s` | `queue` | `kiki_queue_oldest_pending_age_seconds` |
| `kiki.agent_runs.stuck` | gauge | `{run}` | | `kiki_agent_runs_stuck` |
| `kiki.conversations.open` | gauge | `{conversation}` | | `kiki_conversations_open` |
| `kiki.embeddings.backlog` | gauge | `{message}` | | `kiki_embeddings_backlog` |

`service` here is the Railway service the sample describes (`ingester`, `agent`,
`postgres`), not the exporter. `kiki.pg.transactions`, `kiki.pg.deadlocks` and
`kiki.pg.temp.written` mirror Postgres's cumulative `pg_stat_database` counters
(observable counters). `kiki.railway.network` is what Railway's metrics API
reports for the sample window; the poller PR states whether that is cumulative.
The poller runs only on the leader so one instance reports it.
