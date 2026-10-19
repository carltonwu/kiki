# kiki-sim: a local Discord for testing Kiki — HLD

2026-10-18 · **v1 implemented.** Revised after review (GPT-6.1-Sol): isolation is
enforced rather than assumed, stub answers are matched to requests rather than
queued, assertions are split by pipeline stage, and v1 drops mixed live modes,
judges and payload recording.

## Goal

An agent (or a person) that has just changed Kiki should be able to check the
change locally, end to end, without a Discord account, a bot token or any
production system:

```
$ tools/sim/kiki-sim up
$ tools/sim/kiki-sim say --as alex --in general "kiki who talked about the release?"
[general] alex: kiki who talked about the release?
  routed: keyword → new (jev new 0.90), conversation 1 · run 1 done
[general] Kiki ↩ alex: stub: kiki who talked about the release?
$ tools/sim/kiki-sim test
<passed> passed, 0 failed, <skipped> skipped
```

It covers what this repo owns: the Gateway listener, storage, catch-up, routing
(pooling, checks, Jev, the active pool), the agent host, tools and retrieval,
delivery and reactions, and `/kiki` commands. Discord's own behavior is out of
scope beyond the payloads Kiki reads.

## Shape

```
 kiki-sim CLI ──control API──▶ fakecord ◀──Gateway WS── ingester ──HTTP──▶ agent host
  (up, say, test,             (Discord: Gateway,  ──REST /api/v10──▶  (unchanged)   (unchanged)
   inspect)                    REST; OpenRouter:  ◀── Jev, models ──┘          │
      │                        chat, embeddings)  ◀── chat, embeddings ────────┘
      └──── SQL ───▶ sim Postgres (own container, port 54339, one database per stack)
```

The real ingester and agent host run with their Discord and OpenRouter base
URLs pointed at **fakecord**, one Python process. The **CLI** owns the stack's
lifecycle, talks to fakecord's control API and reads Postgres to report what
the router decided.

Injecting rows into `messages` directly was rejected. Replies, reactions and
commands go through Discord REST anyway, and the Gateway half is small while
covering the listener, `store` and catch-up.

## Isolation (must never touch production)

- **Own Postgres.** `tools/sim/compose.yml` runs a separate container
  (project `kiki-sim`, port 54339). Before dropping or creating anything, the
  CLI checks the server's `sim_marker` table, created by its init script.
  `KIKI_SIM_POSTGRES` can select another localhost-only server (as in CI), which
  must also have that table. Service database URLs are built by the CLI; the
  repository `.env` does not select the database.
- **No inherited config.** Both services load `.env` (`dotenvy` in Rust,
  `python-dotenv` in Python) from their working directory and its parents. The
  CLI starts them with a scrubbed environment (`PATH`, `HOME`, the toolchain
  variables, and the sim's own values) and a working directory under `/tmp`, so
  no repo `.env` is ever found. Every variable either service reads is set
  explicitly: the token is `sim-token`, the guild IDs come from the fixture,
  and the provider keys are `sim-key` in stub mode.
- **Both Discord overrides or neither.** The ingester refuses to start when
  only one of `DISCORD_API_PROXY` / `DISCORD_GATEWAY_URL` is set, so half the
  traffic can never reach real Discord. It logs a warning whenever they're set.
- **Stub mode has no egress.** `OPENROUTER_BASE_URL` points at fakecord. Live
  mode (`--live`) leaves it unset and copies only `OPENROUTER_CHAT_API_KEY` and
  `OPENROUTER_DECISION_API_KEY` from the environment or repo `.env` (environment
  wins). Discord stays fake.
- **Telemetry is off.** Neither service gets `OTEL_EXPORTER_OTLP_ENDPOINT`
  unless `KIKI_SIM_OTEL_ENDPOINT` (localhost only, e.g.
  `http://localhost:4318` with `grafana/otel-lgtm`) is set when running `up`;
  then both export there (docs/observability.md).

## Product changes (env-gated, default off)

| Variable | Where | Effect |
|---|---|---|
| `DISCORD_API_PROXY=127.0.0.1:<fakecord port>` | `main.rs` | `twilight_http::Client::builder().proxy(url, true).ratelimiter(None)`. Requests go to `http://127.0.0.1:<port>/api/v10/…`. |
| `DISCORD_GATEWAY_URL=ws://127.0.0.1:<fakecord port>/gateway` | `gateway.rs` | `ConfigBuilder::proxy_url`. twilight connects to `…/gateway/?v=10&encoding=json&compress=zstd-stream`. |
| `OPENROUTER_BASE_URL=http://127.0.0.1:<fakecord port>/openrouter` | `jev.rs` (`/alpha/decisions`), `commands.rs` (`/v1/models`), agent host | The agent host builds `OpenRouterProvider(openai_client=AsyncOpenAI(base_url=…/v1))`, since the provider takes a client, not a URL. Embeddings use the same base. |

## fakecord

One `aiohttp` app (`tools/sim/src/kiki_sim/fakecord.py`) with in-memory state
loaded from a fixture.

**Gateway** (`/gateway/`, any query string): HELLO (`heartbeat_interval`), then
on IDENTIFY: READY (`session_id`, `resume_gateway_url` = itself, `user`,
`guilds`, `application`) followed by GUILD_CREATE (name, channels, threads,
roles). Heartbeats get ACKs. RESUME gets `INVALID_SESSION` with `d: false`,
which tests re-identifying and catch-up, not a true resume. Every dispatch has
an increasing numeric `s`. Frames are plain text, which twilight accepts even
with `zstd` on. fakecord echoes Kiki's sent messages back as MESSAGE_CREATE, as
Discord does.

**REST** under `/api/v10`:

| Caller | Endpoints |
|---|---|
| router startup | `GET /users/@me`, `GET /guilds/{g}/roles` (with a managed role whose `tags.bot_id` is Kiki) |
| commands | `GET /applications/@me`, `PUT /applications/{a}/guilds/{g}/commands`, `POST /interactions/{id}/{token}/callback`, `PATCH /webhooks/{a}/{token}/messages/@original` |
| catch-up | `GET /guilds/{g}`, `/guilds/{g}/channels`, `/guilds/{g}/threads/active`, `/channels/{c}/threads/archived/{public,private}`, `/channels/{c}/messages?after=&limit=` (paginated: fixtures can hold more than 100 messages per channel) |
| router, agent | `PUT`/`DELETE /channels/{c}/messages/{m}/reactions/{emoji}/@me`, `POST /channels/{c}/messages` |
| agent host | `POST /attachments/refresh-urls` (expired attachment links), CDN avatars and attachments |

**Unknown calls fail tests.** The ingester treats some failures as "skip" (a
404 means inaccessible in `rest.rs`), so a 404 alone proves nothing. fakecord
records every unmatched request as `unexpected`, and the runner fails any
scenario whose `unexpected` list isn't empty at teardown.

**Time and IDs.** Every message gets a snowflake minted from its own timestamp,
strictly increasing, and the payload's `timestamp` matches it. Live messages are
stamped "now", which keeps them inside the 1-minute notify trigger and the
router's 5 s age limit. Fixture history uses `days_ago` or `minutes_ago` and
reaches the database only through REST catch-up, too old to route. A scenario
can also set `age_s` on a live message to test the age boundaries.

**Fixture** (`tools/sim/fixtures/minimal.yaml`): a guild, channels (general,
lore, a private channel, a thread), members with usernames, global names, nicks
and permission bits (an admin for `/kiki config`), two members who share a
name, Kiki's user and managed role, and seeded history.

**Control API** (`/_sim/…`): `POST messages` (author, channel, content,
reply_to, mentions, age_s, attachments → the minted message), `PATCH`/`DELETE
messages/{id}`, `POST interactions`, `GET state` (messages, reactions,
interaction responses, `unexpected`, recorded LLM requests), `POST script`
(stub answers), `POST reset`, `POST drop-gateway` (to test reconnects), and
`POST threads` (`from`, `name`, `as`: a thread started from a message, as Discord
makes one: the thread has the message's ID, the parent gets a `THREAD_CREATED`
notice and the thread opens with an empty thread starter message).

## Stub LLMs (default) and live mode

Stub answers are **matched by content, not by arrival order**, so concurrent
channels and background topic jobs can't take each other's answers.

- **Jev** (`/alpha/decisions`): a script entry matches when its regex matches
  any text in `new_messages`. Without a match, the rule is: an @mention, a
  keyword or a reply → `new`, or the conversation whose participant is
  speaking if there is one. Otherwise `no`. Confidence is 0.9.
- **Chat** (`/v1/chat/completions`): the response is OpenRouter-shaped (adds
  `provider`, plus `usage` with `cost`), which pydantic-ai validates. Requests
  that offer tools are reply jobs; requests without tools are topic jobs. Topic
  jobs always get `stub topic`. A reply-script entry matches on the trigger
  message and can make one tool call (`id`, JSON-string `arguments`), then
  answer with a template that may include the tool result's message IDs. By
  default a reply job answers `stub: <trigger text>`. A `web:` entry stands in for
  OpenRouter's web search/fetch server tools: if the request offered them, the
  answer carries `server_tool_use_details` usage and `url_citation` annotations. A `{image_message}`
  placeholder in tool arguments becomes the message ID in the trigger's image
  marker, else the newest one in the conversation.
- **Vision** (chat requests carrying images): `Image n: <label>` per image,
  read back from a tEXt chunk in the 1×1 PNGs that fakecord serves for
  attachments at `/cdn/…`, plus `Gallery: …` for several and the model used.
  The agent host gets `OPENROUTER_VISION_MODEL=sim/vision` and may download from
  fakecord (`DISCORD_MEDIA_EXTRA_HOSTS`). Live mode passes
  `OPENROUTER_VISION_MODEL` through from the environment or the repo's dotenv
  file when set. A drawing's reference check gets `Image n: <label> => yes`, or
  `no` for labels saying meme or screenshot.
- **Images** (`/v1/images`): a 1×1 PNG labeled with the prompt, or a 400 for
  prompts containing "refuse". Reference images must be inline data URLs (it
  rejects links, which expire on Discord); their labels are recorded in order for
  `image_references`. A drawing's failure note (a tool-less request asking for
  one) gets `stub note, in her words: <reason>`.
- **Discord media**: `/cdn/avatars/{user}/{hash}.png` serves a fixture member's
  `avatar` as a PNG labeled `avatar of <username>`. An attachment scripted with
  `expired: true` has `ex=0` and is a 404 until the host refreshes it through
  `POST /api/v10/attachments/refresh-urls` (the host gets `DISCORD_API_URL` and
  `DISCORD_BOT_TOKEN` pointing at fakecord).
- **Embeddings** (`/v1/embeddings`): 4096-dimensional vectors (the host keeps
  the first 1024) from a stable SHA-256 bag of words. They're never zero, and
  they're returned with `index`.
- **Models** (`/v1/models`): a catalog in which the stub models advertise text
  output and tools.

Every request is recorded so tests can check prompts. Script entries left
unused when a scenario ends fail it, which catches tests that no longer
exercise what they claim to.

**Live mode** (`up --live`, or `test --live` for scenarios marked `live: true`)
uses real OpenRouter for Jev, chat and embeddings at about $0.001 per reply.
It's for prompt and behavior work. Its assertions are limited to routing and
delivery, and the CLI prints transcripts for a person or agent to read.

## Assertions, by pipeline stage

A message can stop at any stage for a legitimate reason: an ignored author or a
bot isn't routed, an old message isn't classified, a deleted pool disappears, a
routed run can end with `NO_REPLY` or be superseded, and the 👀 reaction is
removed asynchronously after the run. So assertions name a stage, and each
stage has its own completion barrier:

| Stage | Barrier | Assertions |
|---|---|---|
| `stored` | the row exists in `messages` | `stored: true` |
| `classified` | a `classifications` row, or proof it can't come (ignored author, bot, `age_s` > limit; or "settled": no row 3 s after `POOL_MAX_WAIT_MS`) | `check`, `outcome`, `jev`, `conversation: same_as <step>` / `new` |
| `run` | the `agent_runs` row for the pool reaches `done`, `failed` or `superseded` | `run: done / failed / superseded / none` |
| `delivered` | the run is finished and fakecord holds its sent messages | `reply: true/false`, `text_contains`, `text_matches`, `tool_called` (`web_search`/`web_fetch` are read from the job's saved `web` result, so they work live too), `tools_offered` |
| `drawn` | every `agent_image_jobs` row of the run is `done` or `failed` | `followup: {count, status, attachments, text_contains, text_lacks}` (each picture's follow-up must reply to the request), `image_references`, `image_prompt_contains` (the newest image model call) |
| `reactions` | eventually, within 5 s | `reaction: {emoji: 👀, present: false}` |
| any | none | `prompt_contains`, `prompt_lacks` (last recorded chat request), `rest_calls`; `decide_contains`, `decide_lacks` (compact JSON of the message's recorded Decide state; fail if Decide was not called) |

`expect` waits on the barrier of the latest stage it mentions, for the latest
`say` by default or for a named step (`of: greet`). Steps can be non-blocking
(`send` instead of `say`, or a `batch` of messages sent in quick succession), so
pooling and edits within a pool can be tested. An `exec: "SQL…"` step runs SQL against
the sim database to set up state such as curated `guild_lingo` entries.

## Lifecycle and readiness

- `up`: starts the sim Postgres container if needed, verifies `sim_marker`,
  migrates a template database (`kiki_sim_template_<migrations hash>`, shared by
  every stack), gets the ingester binary and starts fakecord. The binary comes
  from `~/.cache/kiki-sim/ingester/<hash>/`, where the hash covers the content of
  `services/` (minus the agent host), `Cargo.toml`, `Cargo.lock` and the
  toolchain file. On a miss, `up` runs `cargo build` and stores the result. A
  new worktree with unchanged Rust starts in about 2 s instead of a 20 s build.
  The key is content, not file times: a shared cargo target directory judges
  freshness by mtime, so one worktree could run another's ingester.
- **Per scenario**: stop the ingester and agent host and wait for exit (which
  frees the leader lock), recreate the stack's database from the template,
  reset fakecord, start both services, and wait until ready. Ready means the agent
  host's `/health` is OK, the ingester log shows `router listening for new
  messages`, fakecord has sent GUILD_CREATE, and the fixture history count is
  in `messages`.
- `down` stops the stack and drops its database. The container is kept, and
  `down --purge` removes it.

## Stacks

A stack is one fakecord, agent host, ingester and database. Several run side by
side, in one worktree or in many, so parallel agents each own one.

- **Name.** `--stack NAME` or `KIKI_SIM_STACK` picks it; the default is `main`.
  State, pids and logs live in `.sim/<name>/`.
- **Ports.** `up` picks three free ports and saves them in
  `.sim/<name>/config.json`. Later commands read them from there.
- **Database.** `kiki_sim_<worktree path hash>_<name>`, so two worktrees with a
  `main` stack don't share one. Every stack shares the Postgres container.
  Setup they share (the container, the template, the binary cache) runs under
  `~/.cache/kiki-sim/setup.lock`.
- **Capacity.** A stack holds about 15 connections. The container allows 500.
  `up` refuses when the server lacks room for another stack, and says to stop
  idle ones (`kiki-sim stacks`, `kiki-sim down --all`).
- **Parallel tests.** `test -j N` runs scenarios from one queue, longest first
  (durations from the last run in `.sim/durations.json`), on this stack and
  `<name>-2` … `<name>-N`. It starts the extra stacks with the same settings and
  leaves them up for the next run. Each scenario runs in its own
  `kiki-sim --stack <worker> test` process.

A reset can't get much below 1.2 s: twilight-gateway sleeps `2^attempt` seconds
before every connect, the first one included (`shard.rs`, 1 s). Parallel stacks
are what shorten a run.

## CLI (`tools/sim/kiki-sim`)

| Command | Does |
|---|---|
| `up [--fixture F] [--live] [--set NAME=VALUE]` | Starts the stack; repeat `--set` to override ingester settings. Run again after service changes. |
| `--stack NAME …` | Runs any command against stack `NAME` (default `$KIKI_SIM_STACK` or `main`). |
| `down [--all] [--purge]` | Stops the stack and drops its database; `--all` does every stack in this worktree; `--purge` also removes the local sim Postgres container. |
| `stacks` | This worktree's stacks, their ports, what's running and their databases. |
| `reset` | Fresh database and guild with the saved stack settings. |
| `say --as U --in C [--reply-to ID\|kiki] [--age S] TEXT` | Sends a message, waits through `delivered` (or `classified` when it isn't routed), and prints the transcript lines, the classification, the run and the reply. |
| `send …` | Same, without waiting. |
| `slash --as U [--in C] SUBCOMMAND [name=value …]` | Sends a `/kiki` interaction and prints the response, e.g. `slash --as alex config key=chat_model value=sim/other` or `slash --as alex config key=history_messages value=50`. |
| `transcript [--in C]`, `inspect MESSAGE_ID`, `logs [ingester\|agent\|fakecord]` | Inspection. |
| `test [PATH…] [--live] [-k NAME] [-j N]` | Runs scenarios (default `tools/sim/scenarios`), on N stacks with `-j`. Exits non-zero on failure, printing the transcript, the unexpected calls and the last 30 ingester log lines. |

## Scenarios

```yaml
name: a reply after the skulls still gets an answer
steps:
  - say: {as: ethan, in: general, text: "kiki rate my lunch", id: start}
  - expect: {outcome: new, reply: true}
  - say: {as: ethan, in: general, text: "💀💀💀", jev: {choice: "no", confidence: 0.9}}
  - expect: {outcome: "no", reply: false}
  - say: {as: ethan, in: general, text: "Do I not have to reply to you to get a response"}
  - expect: {outcome: continue, conversation: {same_as: start}, reply: true}
```

v1 scenarios cover: a mention, a reply to Kiki, a keyword with Jev, an active
conversation, a non-addressed message, pooling, an edit inside a pool, an ignored
author, eviction at `MAX_ACTIVE_CONVERSATIONS`, cross-channel search via a
scripted tool call, `/kiki config`, low-confidence keyword and participant
follow-ups, direct-follow-up guards, explicitly addressed keywords overriding a
Decide `no`, recorded Decide state, separate simultaneous conversations in one
channel, ambiguous-person lookup, curated lingo in reply prompts, catch-up beyond 100 messages, a
Gateway reconnect, and explicit and implicit web search and fetch
(`web-tools.yaml`; `live-web-tools.yaml` checks real models).

`live-2026-10-18.yaml` replays paraphrased real exchanges with real OpenRouter
calls. It is marked `live: only`, so stub-mode CI skips it. To run it, start with
`tools/sim/kiki-sim up --live`, then use
`tools/sim/kiki-sim test --live -k live-2026-10-18`; read the printed transcript
to assess reply style. Live runs require provider keys and incur charges.

## How agents use it

A project skill (`.claude/skills/kiki-sim/SKILL.md`) and a pointer in
CLAUDE.md say: after changing `services/`, run `kiki-sim test`. Add or extend a
scenario that fails without the change. For prompt or behavior changes, also
`kiki-sim up --live` and `say` a few messages, and quote the transcript in the
summary.

Agents working in parallel each take their own worktree, or their own
`--stack` in a shared one. Their stacks share nothing but the container.

CI adds a `sim` job with its own Postgres service that runs `kiki-sim test -j 4`
in stub mode.

## Not in v1

Mixed live/stub modes, LLM judges, payload recording, real Discord permissions,
rate limits, and Railway deploys. A staging guild with a second bot could cover
the last three later.
