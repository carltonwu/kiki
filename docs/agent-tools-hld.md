# Agent tools and message retrieval — HLD

2026-10-17 · **MVP implemented; updated 2026-10-18 for server-wide retrieval.**
The host runs Pydantic AI once
`OPENROUTER_CHAT_API_KEY` is set; without it, jobs return 501.

## Decisions

- **Agent SDK: Pydantic AI**, `pydantic-ai-slim[openrouter]==2.57.0`. Its
  releases often carry compatibility notes, so upgrade deliberately.
- **Embeddings: `qwen/qwen3-embedding-8b` via OpenRouter**, same key, $0.01 per
  million input tokens. The first 1024 of its 4096 dimensions are stored as
  `halfvec(1024)`, about 2 KB per message; Qwen3 embeddings are Matryoshka, so a
  prefix is a valid embedding. Calls go through the `openai` client the OpenRouter
  extra already installs, since Pydantic's embedder needs `tiktoken`.
  Re-embedding is cheap, so changing the model or dimensions later is fine.
- **Storage: existing Postgres** with full-text search and pgvector. Railway's
  Postgres image ships pgvector 0.8.7; local and CI use
  `pgvector/pgvector:pg18-trixie`. No vector database, no local model, no new
  service.
- **Scope: the job's guild.** Search covers ingested history across that server;
  an optional channel argument narrows it. Guild boundaries come from trusted run
  context. Retrieval does not apply the requesting member's channel permissions.

## Ownership

Rust keeps Discord, routing, `agent_runs`, prompt snapshots, wake-up/retries,
delivery and all migrations. Python's `Executor` is a Pydantic AI `Agent`, whose
runner owns model turns and tool calls. The
[HTTP contract](../services/agent/README.md) is unchanged: trusted `scope.guild_id`
bounds retrieval, while `scope.channel_id` identifies the reply destination.

| File | Contents |
|---|---|
| `retrieval.py` | SDK-independent: embed, index pending, search, context, and `index_all`. Plain SQL; vectors pass as text cast to `halfvec`, so there's no pgvector client. |
| `agent.py` | `PydanticExecutor`: agents, trusted deps, two tools, message/usage mapping, transient-error mapping. |
| `app.py` | Wires the executor when the key is set; startup and per-job logs. |
| `0007_retrieval.sql` | Extension, full-text index, `message_embeddings`, staleness trigger. |

Rust prompt changes, already made:
- "Other active conversations" is limited to the destination channel. It used
  to list every guild and channel, which leaked other channels to the reply's
  readers.
- The capabilities line says Kiki can search every channel in this server, with
  an optional channel filter, and should cite message links.

## Tools

The trusted deps hold the connection, the scope (guild, channel, conversation),
the embedder and the per-run counters. They come from the request, never from
model arguments, and arguments can only narrow them.

- `search_messages(query, person?, channel?, before?, after?, limit=10)` returns, for each
  hit, an ID, author, source channel, time, Discord URL and the first 1,000
  characters of text, plus `semantic: "full" | "partial" | "off"`. `person` is a roster ref, an
  @username or any name the person has had. It's resolved to user IDs
  ([identity-hld](identity-hld.md)), and authors are shown by their current
  label. `channel` accepts a name, channel mention or ID and only narrows search
  within the guild.
- `get_message_context(message_id, before=5, after=5)` returns neighbors in the
  target message's own channel (at most 10 on each side) and the reply parent
  if it is in the same guild.

- `search_emojis(query, limit=5)` and `find_emoji(name)`: the guild's custom emoji
  by mood or meaning, or by name (`huh`, `:huh:`, `<:huh:123>`). See
  [Custom emoji](#custom-emoji).

Search excludes deleted messages, bot messages (Kiki's replies are not evidence)
and the current conversation's messages, which Rust already put in the prompt.

Limits:
- 4 tool calls per run. After that, tools tell the model to answer with what it
  has, rather than aborting.
- 6 model requests per run as the hard stop (`UsageLimits`).
- 10 seconds per tool call; on timeout the tool returns an error to the model.
- Topic jobs run with no tools.

All values are constants to tune from the logs.

## Search: one SQL statement

1. **Lexical:** top 50 by `ts_rank_cd`, using an OR of the query's English
   lexemes, so stopwords and unmatched words don't block hits:
   `replace(plainto_tsquery('english', q)::text, ' & ', ' | ')::tsquery`,
   matched against the `to_tsvector('english', content)` GIN index.
2. **Semantic:** top 50 by cosine distance, as an exact scan within the guild
   (or selected channel),
   keeping only candidates with similarity ≥ `MIN_SIMILARITY` (0.25). Without a
   floor, nearest neighbors of a nonsense query still rank, and the recency
   bonus would promote recent noise.
3. **Fuse:**

   `score = 1/(k + r_lex) + 1/(k + r_sem) + w/(k + 1) · 2^(−age_days/30)`

   - `k = 60`, `w = 0.15`.
   - A missing branch contributes 0.
   - Age uses the original `created_at`.
   - Explicit `before`/`after` filters set `w = 0`.

The recency term is a fraction of a top-ranked vote that decays with time. A new
hit overtakes a very old one only if it ranks within about 5 places in both lists
(`2/(60+r) + 0.15/61 > 2/61` holds for r ≤ 5). Recency never adds candidates.

A rank-based recency term was tried and dropped: its strength depended on
candidate count. With two candidates it couldn't break even a one-rank tie.

If query embedding fails, the search runs lexically and reports `"off"`. Empty
queries are an argument error returned to the model.

## Embeddings

- **Pending** means a non-bot, non-empty, undeleted message with no row in
  `message_embeddings`. There's no queue.
- **Staleness:** an `AFTER UPDATE OF content, deleted_at` trigger deletes the
  vector. That covers every Rust write path (live update and REST upsert), so
  Rust has no indexing code.
- **Write:** an `INSERT … SELECT … FOR SHARE` saves only when `md5(content)`
  still matches the embedded text. A concurrent edit is waited out and then
  rejected.
- **Text:** raw `content`. Discord's 4,000-character limit is far below the
  model's context, so there's no chunking.
- **Query text** gets Qwen's instruction prefix; documents get none.
- **When indexing happens:**
  - When a reply job starts, the guild's 256 newest pending messages are
    embedded in the background, in batches of 64, while the model reads the
    prompt. The first search waits for this to finish; if the run ends without
    a search, indexing is cancelled. If 256 were found, the guild reports
    `"partial"`. A sleeping service does nothing; a reply can index pending
    messages from other channels in its guild.
  - History uses `kiki-agent-host index` (for example, through `railway ssh`).
    It walks all pending messages newest first with a keyset cursor, logs
    progress and cost, and exits. Rerun it any time.
- **Cost:** about 25 tokens per message, so roughly $0.25 per million messages.
  Storage, about 2 GB per million, is the larger cost. A reply's embedding cost
  is added to its `usage.cost`.
- **Scale:** add HNSW on `halfvec` only when measured scan latency requires it,
  and check filtered recall when you do.

## Custom emoji

- **Storage:** `guild_emojis` (migration 0017), one row per Discord emoji ID, with
  name, animated/available flags, the raw object, a description and its
  `halfvec(1024)` embedding of `:name: description`. Emoji deleted in Discord get
  `removed_at`: they're excluded from search, but `find_emoji` still explains one
  written out in an old message. A trigger drops the embedding when the name or
  description changes.
- **Sync (Rust):** `/kiki emojis resync:true` (Manage Server) replaces the list from
  `GET guilds/{id}/emojis`. There's no Gateway sync yet (it would need the
  `GUILD_EXPRESSIONS` intent); admins resync after adding emoji. Descriptions survive
  renames, since an emoji's image never changes.
- **Describe (Python):** Rust then calls `POST /v1/emojis/describe` in batches of 8
  until none are left (at most 10 minutes; the interaction token lasts 15). The host
  downloads each emoji as a 128 px PNG (the first frame if animated) and asks
  `OPENROUTER_EMOJI_MODEL` (Rust's env, sent per request like the chat model;
  default `google/gemini-2.5-flash-lite`) for one or two sentences on what it shows
  and when people use it. Each emoji is tried at most once per resync
  (`describe_attempted_at` against the round's start), so failures don't loop.
  `redescribe:true` also replaces older descriptions. Cost is shown in the command
  reply and kept per emoji (`describe_cost_usd`).
- **Search:** like message search, full text (name with `_` as spaces, plus the
  description) and vectors fused by reciprocal rank, `MIN_SIMILARITY` floor, no
  recency. Only the guild's current, available emoji. Undescribed emoji match by
  name only. Pending embeddings (renames) are filled in at search time.
- **Prompt:** one stable instructions line tells Kiki the tools exist and to write
  emoji exactly as returned. The emoji list itself is not in the prompt.

## Failures and retries

- **Transient failures** are provider 429/5xx, connection errors, and Postgres
  `OperationalError` (including inside tools). They raise `TransientError`,
  which isn't persisted, so the host returns 503 and Rust retries the same ID.
- **Other exceptions** persist `failed`, as before.
- **Replays** after a timeout or crash repeat inference and reads, which is
  safe. Embedding writes are idempotent.
- **Usage mapping:** input/output tokens, model requests → `steps`, and
  Pydantic's run cost plus embedding cost → `cost`.

## Logs (never message bodies or queries)

- **Startup:** `agent host ready ms rss_mb executor`. Locally: 0.6 s and 90 MB
  with the SDK, 0.2 s and 53 MB without. Measure on Railway after deploy.
- **Job:** status, latency, RSS; model requests, tool calls, tokens, cost.
- **Tool call:** latency, hits, semantic mode, channel; each search's top
  similarity; indexing counts.

## Next

1. For a new installation, set the key, optionally run `index`, then set
   `AGENT_HOST_URL`. Read the cold-start and memory log lines.
2. Build 30–50 real queries with expected message IDs, covering paraphrases,
   exact names, old exact versus recent weak matches, date filters,
   edits/deletes and outages. Calibrate `MIN_SIMILARITY` from logged
   similarities, then tune `w`, candidate counts and limits on recall@10,
   citation accuracy, latency and cost.
3. **Later:**
   - Member-specific channel permission filtering and optional allowlists;
     current retrieval is bounded by guild, with an optional channel filter.
   - OpenRouter server tools: Pydantic's `WebSearchTool` maps to
     `openrouter:web_search` and the engine isn't selectable. Cap
     `max_tool_calls` (OpenRouter's default is 30), accept repeated cost on
     replay, and note that queries reach outside engines.
   - Image results: done for generated images ([image generation](image-generation.md));
     search results still carry no images.
   - Embedding windows of adjacent messages, if short messages retrieve poorly.
   - `/kiki conversations` still lists the active pool across all channels to
     whoever runs it.

## Alternatives considered

Stable core Python releases, 2026-09-18 to 2026-10-17 UTC, from each repo's
GitHub releases API:

| SDK | Backing | Releases | Why not chosen |
|---|---|---|---|
| LangChain agents | LangChain Inc. | `langchain` 5, LangGraph core 5 | Runner-up; checkpointing and pause-and-resume aren't needed yet. Server-tool support undocumented on ChatOpenRouter. |
| OpenAI Agents SDK | OpenAI | 3 | OpenRouter only through adapters; OpenAI hosted tools ≠ OpenRouter server tools. |
| Google ADK | Google | 4 | Non-Gemini models go through LiteLLM; Google-native features don't port. |
| Microsoft Agent Framework | Microsoft | 4 | More framework than Kiki needs; OpenRouter path unverified. |
| Claude Agent SDK | Anthropic | 12 (9 only bump the bundled CLI) | Runs the Claude Code binary. OpenRouter documents it but guarantees only Anthropic's first-party provider. |

Pydantic AI had 14 (13 on v2 plus a v1 security backport).
Sources: [Pydantic/OpenRouter](https://ai.pydantic.dev/models/openrouter/),
[OpenRouter embeddings](https://openrouter.ai/docs/api/reference/embeddings),
[OpenRouter server tools](https://openrouter.ai/docs/guides/features/server-tools),
[OpenRouter + Claude Code](https://openrouter.ai/docs/guides/coding-agents/claude-code-integration),
[pgvector](https://github.com/pgvector/pgvector),
[Postgres full text](https://www.postgresql.org/docs/18/textsearch-controls.html).
