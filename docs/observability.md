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
  loop, once the response is out. The span and log batch processors wake every
  few seconds but send only when they hold something. Shutdown (SIGTERM, via the
  app's lifespan, and the end of `kiki-agent-host index`) flushes once more.
  So the host's metric samples are sparse: a counter has a sample only after a
  job, and an instant query goes stale five minutes later. Query them over a
  range (`increase(kiki_agent_jobs_total[$__range])`, `last_over_time(…[1h])`).
- **The ingester runs always**, so it exports normally: spans and logs in batches,
  metrics every 60 s. Its main flushes and stops the exporters on every exit path,
  bounded to 5 s. `ingester migrate` (the pre-deploy step) doesn't export.
- **Temporality is cumulative**, what Mimir (Prometheus) expects.
- **Traces:** the ingester exports only its own spans (`tracing` spans from the
  `ingester` crate; dependencies' spans, like one per twilight REST call, would
  each start a trace). The agent host traces inbound requests (FastAPI, `/health`
  excluded) and outbound HTTP (httpx, and httpx2 for the OpenAI SDK), with the W3C
  `traceparent` extracted and injected; client span URLs lose their query string
  (Discord's signed attachment links). GenAI instrumentation (pydantic-ai,
  logfire) stays off: no prompt or reply text goes into telemetry.
- **Logs:** the ingester bridges `tracing` events (`opentelemetry-appender-tracing`),
  the agent host the root `logging` logger. Neither exports the exporter's own logs
  (`opentelemetry*`, and its HTTP clients: reqwest/hyper/h2 in Rust,
  urllib3/requests in Python), which would otherwise feed back into the export.
  The agent's JSON stdout lines carry `trace_id`/`span_id` inside a span.

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
(`RAILWAY_ENVIRONMENT_NAME`, else `local`), `service.instance.id`
(`RAILWAY_REPLICA_ID`, else the service name; never a random per-process ID,
which would start new series on every wake of the agent host). In Grafana Cloud
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

### Ingester

| Metric | Type | Unit | Attributes | Prometheus |
|---|---|---|---|---|
| `kiki.gateway.events` | counter | `{event}` | `type` (handled Gateway events, else `other`) | `kiki_gateway_events_total` |
| `kiki.gateway.reconnects` | counter | `{reconnect}` | `reason` | `kiki_gateway_reconnects_total` |
| `kiki.gateway.lag` | histogram | `s` | | `kiki_gateway_lag_seconds_*` |
| `kiki.gateway.store_errors` | counter | `{error}` | | `kiki_gateway_store_errors_total` |
| `kiki.messages.ingested` | counter | `{message}` | `author=human\|bot\|kiki` | `kiki_messages_ingested_total` |
| `kiki.router.admissions` | counter | `{message}` | `result`, `reason` | `kiki_router_admissions_total` |
| `kiki.router.classifications` | counter | `{pool}` | `check`, `outcome` | `kiki_router_classifications_total` |
| `kiki.jev.confidence` | histogram | `1` | | `kiki_jev_confidence_*` |
| `kiki.agent_runs.queued` | counter | `{run}` | `kind` | `kiki_agent_runs_queued_total` |
| `kiki.agent_runs.finished` | counter | `{run}` | `kind`, `status=done\|failed\|superseded\|expired\|interrupted` | `kiki_agent_runs_finished_total` |
| `kiki.agent_run.duration` | histogram | `s` | `kind` | `kiki_agent_run_duration_seconds_*` |
| `kiki.agent.workers.active` | gauge | `{worker}` | | `kiki_agent_workers_active` |
| `kiki.agent_host.requests` | counter | `{request}` | `endpoint=jobs\|images\|emojis`, `status` (HTTP code, or `error`) | `kiki_agent_host_requests_total` |
| `kiki.agent_host.retries` | counter | `{retry}` | `reason` | `kiki_agent_host_retries_total` |
| `kiki.drawings.finished` | counter | `{drawing}` | `outcome` | `kiki_drawings_finished_total` |
| `kiki.conversations.started` | counter | `{conversation}` | | `kiki_conversations_started_total` |
| `kiki.conversations.closed` | counter | `{conversation}` | `reason` | `kiki_conversations_closed_total` |
| `kiki.conversation.messages` | histogram | `{message}` | (recorded at close) | `kiki_conversation_messages_*` |
| `kiki.discord.messages_sent` | counter | `{message}` | `kind` | `kiki_discord_messages_sent_total` |
| `kiki.discord.send_errors` | counter | `{error}` | `kind` | `kiki_discord_send_errors_total` |
| `kiki.discord.reactions` | counter | `{reaction}` | `result` | `kiki_discord_reactions_total` |
| `kiki.discord.rest.retries` | counter | `{retry}` | `reason` | `kiki_discord_rest_retries_total` |
| `kiki.reply.latency` | histogram | `s` | | `kiki_reply_latency_seconds_*` |
| `kiki.leader.held` | gauge | | | `kiki_leader_held` — **live** |
| `kiki.db.pool.connections` | gauge | `{connection}` | `state=used\|idle` | `kiki_db_pool_connections` |

`kiki.reply.latency` (trigger message created → first reply sent) is the
headline SLO. `kiki.gateway.lag` is Discord's message timestamp → receipt.
`kiki.leader.held` is 1 while the instance holds the leader lock, 0 after release.

### Agent host

| Metric | Type | Unit | Attributes | Prometheus |
|---|---|---|---|---|
| `kiki.agent.jobs` | counter | `{job}` | `kind=reply\|topic\|image`, `outcome=completed\|failed\|running\|interrupted` | `kiki_agent_jobs_total` — **live** |
| `kiki.agent.job.duration` | histogram | `s` | `kind` | `kiki_agent_job_duration_seconds_*` |
| `kiki.agent.jobs.rejected` | counter | `{job}` | `reason=busy\|conflict` | `kiki_agent_jobs_rejected_total` |
| `kiki.agent.job.model_requests` | histogram | `{request}` | | `kiki_agent_job_model_requests_*` |
| `kiki.images.attempts` | counter | `{attempt}` | `model`, `result=drawn\|safety_blocked\|refused\|failed` | `kiki_images_attempts_total` |
| `kiki.images.fallback` | counter | `{attempt}` | `result=drawn\|failed` | `kiki_images_fallback_total` |
| `kiki.images.duration` | histogram | `s` | `model` | `kiki_images_duration_seconds_*` |
| `kiki.tools.calls` | counter | `{call}` | `tool`, `outcome=ok\|error\|timeout\|refused\|invalid_arguments\|budget_exhausted` | `kiki_tools_calls_total` |
| `kiki.tools.duration` | histogram | `s` | `tool` | `kiki_tools_duration_seconds_*` |
| `kiki.web.calls` | counter | `{call}` | `kind=search\|fetch` | `kiki_web_calls_total` |

`kiki.agent.jobs` counts answered job requests (`running`: another request holds
the same job ID); `interrupted` ones return 503 and are retried with the same ID.
Busy and conflicting requests go to `kiki.agent.jobs.rejected` instead. The
emoji-description endpoint is not a job.

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
