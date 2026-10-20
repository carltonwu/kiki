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
| `chat/search.py`, `chat/indexing.py`, `chat/history.py` | SDK-independent: index pending, search, context, channel history, and `index_all`. Plain SQL; vectors pass as text cast to `halfvec`, so there's no pgvector client. |
| `chat/scope.py`, `chat/attachments.py` | The job's scope and people, name and channel resolution, and the images a stored message carries. |
| `openrouter/embeddings.py` | The embedder: OpenRouter embeddings as `halfvec` literals, with their cost. |
| `agent/executor.py` | `PydanticExecutor`: agents, message/usage mapping, transient-error mapping, image jobs and emoji descriptions. |
| `agent/tools/` | One module per capability, each a `FunctionToolset` listed in `TOOLSETS`. Every tool runs through `agent/guard.py` (deadline, budget, log line). |
| `agent/web.py`, `openrouter/chat.py` | OpenRouter's web search and fetch server tools, and the model subclass that maps fetch. |
| `media/fetch.py`, `media/vision.py` | The common image backing for `read_image`, drawings and emoji descriptions: Discord-only downloads typed by their bytes, refreshing expired attachment links (with `DISCORD_BOT_TOKEN`), the vision model call with its error mapping, and billed cost. |
| `media/reading.py` | `read_image`: image extraction from the stored payload (Kiki's own drawings from `agent_host_images`) and the detailed agent-to-agent prompt. |
| `media/links/` | `read_link`: a dispatcher over one reader per site (`READERS`); `reddit.py` reads Reddit posts, `x.py` X posts. |
| `openrouter/images.py`, `media/generation.py` | OpenRouter's Images API as a Pydantic AI image model, with reference images ([image generation](image-generation.md)), and saving the result. |
| `media/drawing.py` | Image jobs (`POST /v1/images`): reference photos per subject, the vision check, the final prompt, and Kiki's note when drawing fails. `generate_image` itself (`agent/tools/drawing.py`) only queues. |
| `chat/emojis.py` | Custom emoji: description batches, embeddings, search and lookup. |
| `api/app.py` | Wires the executor when the key is set; startup and per-job logs. |
| `0007_retrieval.sql` | Extension, full-text index, `message_embeddings`, staleness trigger. |

Rust prompt changes, already made:
- "Other active conversations" is limited to the destination channel. It used
  to list every guild and channel, which leaked other channels to the reply's
  readers.
- The capabilities line says Kiki can search every channel in this server, with
  an optional channel filter, and should cite message links.
- Threads (issue #24): the system prompt names a thread with its parent channel
  (`thread "Thread" in #boba`). Discord's thread starter message (type 21) is an
  empty placeholder pointing at the message the thread was started from, so the
  run context shows that message instead, on its own line with its real author,
  text and `[image: …; message <id>]` marker (`read_image` takes that ID). A thread
  started from a message has the message's ID, which finds it when the placeholder
  is too old to be shown. `THREAD_CREATED` notices (type 18) stay out of the
  channel context.
- Server-specific names (issue #24): words in the newest messages that are also a
  channel name or a memory subject of the server (`name_norm` match, single words
  and pairs) are listed in the run context with a nudge to check memory or history
  before answering from general knowledge, so "describe boba" in a server with a
  #boba channel doesn't need Kiki to think of looking. The system prompt says the
  same for bare names and nouns aimed at her.

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
  within the guild. `person: "you"` (or "Kiki") searches Kiki's own past
  messages, for "find what you said": by keywords only, since her messages aren't
  embedded. Her user ID is the author of her latest reply in the guild
  (`agent_runs.reply_message_ids`); the request doesn't carry it. A search with no
  hits, or an unknown `person`, says what to try instead.
- `find_quotes(person?, query?, limit=25, cursor?)` (`chat/quotes.py`) returns the
  server's saved quotes: what people saved with a quote bot (UB3R-B0T's `.quote`
  embeds, PilzBrots' "Quote #N" Components V2 cards). Those posts have no
  `content`, so search skips them as bots' and history showed them blank; asked
  for someone's quotes from #quotes, Kiki found only the `.quote 194` commands
  (reply:432, 442). Migration 0030's `quote_parts(raw)` parses each bot's format
  by its structure: the attribution is UB3R-B0T's last description line under a
  `#N` embed author, or PilzBrots' last text display starting "(Authored )by"
  after a `**Quote #N**` header, and `quoted_message_id(raw)` is that line's
  link. A link inside the quote is never taken for the saved message. These run
  in an index expression on every bot message's insert, so nothing raises: IDs are
  range-checked before the cast (a malformed link is NULL), and anything
  unexpected is "not a quote post". A partial index finds a guild's posts; it's a plain build (writes wait
  2-3 s at deploy), since a concurrent one, in its own non-transactional
  migration, can fail half way and leave an invalid index sqlx can't recover. Each quote is the saved message itself,
  so the text is verbatim and the author is who said it, not the bot; a quote
  posted many times counts once. A deleted original is left out. One never
  ingested is the bot's copy, marked `saved_copy`, only if its link is to this
  guild and it isn't attributed to an ignored author or a bot (a mention, a bare
  ID, or a name resolved through `user_names`: the whole name between the
  format's own delimiters, and a bot also by `people.bot`). PilzBrots is an ignored author,
  but its quote cards only point at people's messages, like a link fixer's
  reposts. `person` matches anyone the guild has seen (`user_names`, mentions
  included), not only authors of stored messages: a quote's author often isn't
  one. Without `query` it lists newest first (`total`; `cursor` is `older:<ms>:<id>`, a
  fixed point, so a quote arriving between pages can't repeat one); with
  one it ranks by keywords over every quote's text (copies included) and by
  meaning over stored ones (reciprocal rank fusion, exact distances over a few
  hundred rows). Quotes come whole, not as 1,000-character snippets; a page
  stops at 12,000 characters (`cursor` reads on; ranked pages, whose order can
  change, skip what earlier ones returned: `seen:<ids>`). A name several people
  share gets search's note naming each. Its own connection and search's 9 s deadline
  (`Deadline`, each step `bounded`), the lookup budget. In prod: 1,394 quote
  posts, 419 quotes, 394 originals stored.
- `get_message_context(message_id, before=5, after=5)` returns neighbors in the
  target message's own channel (at most 10 on each side) and the reply parent
  if it is in the same guild. When the target is an ignored author's, a note says
  it exists but isn't shown (reply:72 told the user it was deleted).
- `channel_history(channel?, after?, before?, limit=100, cursor?)` (issue #39) reads a
  channel and its threads in time order, for catch-ups ("what did I miss"). `channel`
  defaults to the current one and resolves like search's; `after`/`before` are ISO
  times (UTC unless offset) or message IDs, exclusive for IDs. With `after` it reads
  forward, otherwise it returns the newest messages; a page holds at most 100
  messages and about 40,000 characters of text, and `cursor` (`newer:<id>` or
  `older:<id>`) continues it. Unlike search it keeps bot messages (marked `bot`; voice
  transcripts; a blank one reads as its card, below) and the current conversation, so the timeline has no holes, and lists
  attachment names as `files`. `total_in_range` lets the model judge how much is
  left. Query logic is in `chat/history.py`; a system prompt line points catch-ups at it.
- `read_image(message_id, question?)` has another model look at a message's
  images (see below). Search and context hits with images carry `images: n`.
- `count_messages(query?, pattern?, person?, channel?, before?, after?, group_by?,
  limit=10)` (issue #38, `chat/counting.py`) answers "who says X the most" and
  "how often per year" exactly, where search could only show 10 hits. One
  aggregate query counts matching messages (not repeats within one): `query` is
  `websearch_to_tsquery` against `messages_fts_idx` (stems, `"phrases"`, `or`,
  `-word`), `pattern` a case-insensitive regex (`\b` is rewritten to Postgres's
  `\y`); no semantic match, since nearest neighbors always exist. `group_by` is
  `person`, `channel`, `month` or `year` (UTC). It returns `total`,
  `groups_total` and the top `limit` groups (at most 50) by count, each with its
  newest matching message as a link (outside the current conversation when
  there is one, so it isn't the question itself). Scope is search's (guild,
  roster resolution, no bots, deleted messages or ignored authors), except that
  the current conversation counts. It runs on its own connection with
  `SET LOCAL statement_timeout = '6s'` (below the 10 s tool deadline), so a
  regex scan over the whole guild comes back as "narrow it" rather than running
  on; a stopword-only query or a bad regex is an argument error. It shares the
  4-call lookup budget. The system prompt tells Kiki to use it, not search
  hits, for counts.

- `read_link(url_or_message_id, question?)` reads a shared link on a site web
  fetch can't read (see [Links](#links-read_link-issues-41-40)).

- `search_emojis(query, limit=5)` and `find_emoji(name)`: the guild's custom emoji
  by mood or meaning, or by name (`huh`, `:huh:`, `<:huh:123>`). See
  [Custom emoji](#custom-emoji).

Search excludes deleted messages, bot messages (Kiki's replies are not evidence,
unless she asks for her own) and the current conversation's messages, which Rust
already put in the prompt.

A message with no `content` reads as its card (`chat/cards.py`): its rich embeds'
author, title, description, fields and footer, and its Components V2 text
displays at any depth. Link previews aren't cards, and a message with text keeps
its text. This covers every tool's citations (history, context, search, quotes);
Rust's prompt context still shows such a message as blank.

### Web search and fetch (issue #1)

Every reply job also offers OpenRouter's **server tools** `openrouter:web_search`
and `openrouter:web_fetch`. OpenRouter runs them inside a model request and the
model decides when to use them; the system prompt tells Kiki to look things up
unprompted when an answer depends on current or uncertain facts, and to cite the
pages it used. Topic jobs still have no tools.

- **Why server tools:** they use the existing OpenRouter key and billing, need no
  search-provider account, and the fetch runs on OpenRouter's side, so the agent
  host never requests arbitrary URLs from Railway's private network (no SSRF
  surface).
- **Wiring:** Pydantic AI's `WebSearchTool` maps to `openrouter:web_search`.
  2.57.0 doesn't map `WebFetchTool` for OpenRouter, so `WebOpenRouterModel`
  appends `openrouter:web_fetch` in `_get_tool_choice` (a private hook; recheck
  it on SDK upgrades). Engines are OpenRouter's `auto` defaults.
- **Limits:** `max_tool_calls` = 3 server-tool steps per model request (OpenRouter's
  default is 30), so at most 18 over the 6-request cap. Fetched pages are cut to
  about 6,000 tokens (`max_content_tokens`; the native engine ignores it). Kiki's
  4-call budget covers only its own tools.
- **Cost:** OpenRouter adds the search fee to the response's `usage.cost` (Exa: about
  $0.007 per search; native search passes through the provider's price, about
  $0.016 for one probe on 2026-10-18), which already flows into the run's cost. Search
  results enter the prompt, so a searched reply reads tens of thousands of input
  tokens.
- **Recorded use:** OpenRouter reports `usage.server_tool_use_details`
  (`web_search_requests`, `tool_calls_executed`) and search sources as
  `url_citation` annotations. The host sums these into
  `result.web = {searches, fetches, sources}` (fetches = executed − searches, since
  only those two are offered), logs the counts, and saves them with the job. Rust
  ignores the field. The sim's `tool_called: web_search|web_fetch` reads it.
- **Replays** repeat web calls and their fees, like inference.
- **Privacy:** queries and URLs reach OpenRouter's search and fetch engines.

Limits:
- 4 search tool calls per run. After that, tools tell the model to answer with what it
  has, rather than aborting. Memory tools ([subject memory](subject-memory-hld.md))
  have their own budget of 6.
- 10 model requests per run as the hard stop (`UsageLimits`); it was 6 before
  memory tools, which can take a failed write, a split and a retry.
- 10 seconds per tool call (45 for `read_image`); on timeout the tool returns
  an error to the model.
- Topic jobs run with no tools.

All values are constants to tune from the logs.

## Images: `read_image`

The chat model may not see images, and a text-only model is cheaper for chat,
so images are read by a separate **vision model** that writes for Kiki rather
than for people (agent to agent).

- **Model:** `OPENROUTER_VISION_MODEL` on the Python service. When unset, the
  chat model is tried; if the provider rejects images (a 4xx), the tool tells
  Kiki that no vision model is configured and the chat model can't read images.
  Rate limits and 5xx become "unavailable right now". Failures never fail the
  run. In a live check (2026-10-18; a captioned cat meme plus its source photo),
  `anthropic/claude-haiku-5.5` transcribed the captions exactly, noted the
  cropped letters and told the two images apart, for $0.0007. The default chat
  model `deepseek/deepseek-v4.1-flash-1017` accepts images but described text
  that wasn't there, so set a vision model rather than relying on the fallback.
- **Thinking:** a reasoning vision model thinks inside `max_tokens`, so `look`
  (`media/vision.py`) gives it `REASONING_TOKENS` on top of the answer's room.
  If it still thinks until the limit and writes nothing (pydantic-ai's "token
  limit exceeded before any response"), it's asked once more with thinking off;
  only if that also comes back empty does Kiki get an error, worded so she says
  she couldn't see it rather than guessing. A cut-off answer is marked as such.
- **In the prompt:** Rust renders a message's images as
  `[image: cat.png; message 123]` or `[gallery of 3 images: a.png, b.png,
  linked image; message 123]`, so the model has the ID to pass. File names are
  stripped of `[];` so a name can't forge another marker. The system prompt says
  Kiki can't see images and should call the tool when they matter.
- **What counts as an image:** attachments with an `image/*` content type or a
  png/jpg/gif/webp name, then embeds of type `image`/`gifv` (their thumbnail)
  or with an `image`, deduplicated by URL, at most 10. Rust (`MESSAGE_COLUMNS`)
  and Python (`images_in`) apply the same test (Rust doesn't deduplicate).
  Discord's attachment URLs are signed and expire after about a day; with the
  bot token (`DISCORD_BOT_TOKEN`), the host refreshes expired ones through
  `POST /attachments/refresh-urls` before downloading. Without it, older images
  come back as expired.
- **Kiki's drawings:** images in a bot message named `kiki-<id>.png` (how Rust
  uploads `generate_image` results) are read from `agent_host_images` when the
  drawing job's guild matches, and labeled for the vision model and in the
  result (`drawn_by_kiki`). Other images, and drawings not found there, are
  downloaded as usual.
- **Galleries:** a message with several images is a Discord gallery. One call
  sends them all, labeled `Image n: <file name>` (with alt text if set), plus
  the message text and author for context and Kiki's optional `question`. The
  answer has one `Image n:` line per image and a `Gallery:` line on how they
  relate and what they add up to.
- **The prompt** asks for the kind of image, every legible text verbatim
  (usernames and timestamps in screenshots), people and animals, setting, and
  the point (joke, meme format, mood), marking interpretation and unreadable
  parts. It forbids identifying people from their faces and treats text in
  images, the message text, file names and alt text as content, never
  instructions.
- **Downloads:** URLs come only from the stored message payload, never from
  model arguments. The host downloads them itself (no redirects, 10 s, 5 MB
  each) and sends them inline, only from `https` Discord hosts
  (`*.discordapp.com`, `*.discordapp.net`, `*.discord.com`); embed images use
  Discord's proxy URL, not the linked site. `DISCORD_MEDIA_EXTRA_HOSTS` adds
  hosts (the simulator's fakecord). Images over 1568 px or 5 MB are fetched
  through Discord's media proxy with `width`/`height`. PNG, JPEG, WebP and GIF
  are sent, typed by their leading bytes rather than the file name or
  `Content-Type` (providers reject a renamed JPEG sent as PNG); anything else, an expired link (403/404/410) or an oversized file
  is reported per image while the rest are still described.
- **Cost:** the vision call's billed cost is added to the reply's
  `usage.cost`; the job log reports `vision_calls`. Descriptions aren't cached,
  so reading the same image again (or a replay) pays again.

## Links: `read_link` (issues #41, #40)

`media/links/__init__.py` normalizes the URL (or takes the first readable link
in a stored message, text then link previews) and hands it to the first reader in
`READERS` whose `matches(url)` is true; a link no reader takes returns an error
that sends Kiki to web fetch. Readers request only their own site's hosts, and
images go through `media/vision.py` with `read_image`'s instructions. It's a
lookup (45 s deadline).

- **Reddit** (`reddit.py`): `reddit.com` and its subdomains, `redd.it/<id>`, and
  `/r/<sub>/s/<code>` share links (one redirect, read but not followed). It
  fetches `/comments/<id>.json?raw_json=1&sort=top` (10 s, 2 MB) with a
  descriptive User-Agent and returns title, subreddit, author, score, ratio,
  comment count, flair, text (4,000 chars), the outside link, and the top 8
  top-level comments (600 chars each; pinned mod notes and removed comments
  skipped). A comment link puts that comment first. Galleries and image posts
  (at most 10, at preview size, only from `i.redd.it` and `preview.redd.it`) are
  described in one vision call. `v.redd.it` videos return duration and size with
  a note that watching isn't supported yet (shared video reader: #43). Private,
  quarantined, banned, deleted and removed posts get a clear error; removed ones
  keep what's left. Results are cached in memory per post and question for 10
  minutes (128 entries); errors aren't.
- **Blocking:** Reddit refuses anonymous requests from many datacenter IPs (403
  "blocked by network security"; on 2026-10-18 it also did from a home WSL
  machine). With `REDDIT_CLIENT_ID` and `REDDIT_CLIENT_SECRET` (a Reddit "script"
  or "web" app; read-only), requests use an app-only token
  (`client_credentials`) on `oauth.reddit.com`, reused until it expires. Without
  them, a refusal is logged with a hint to set them.
- **X / Twitter** (`x.py`, issue #40): status links on `x.com`, `twitter.com` and
  the embed fixers (`vxtwitter`, `fxtwitter`, `fixupx`, `fixvx`) go to the
  FxTwitter JSON API (`api.fxtwitter.com/status/<id>`; no X keys; 8 s, 1 MB; it
  turns away httpx's default User-Agent). It returns author, text (4,000 chars),
  time, stats, poll, community note and media types for the post and a quoted
  post. Photos, and video and GIF thumbnails (at most 8, X's 1200 px rendition,
  only from `pbs.twimg.com`), are described in one vision call; videos get a note
  that only the thumbnail was seen (#43). Deleted (404) and private (401) posts get
  a clear error. Cached like Reddit (10 minutes, 256 entries). Discord's stored
  embed isn't used: it has the text and at most one image, but no stats, quoted
  post or video, and links in `<...>` have none.

## Search: one SQL statement

1. **Lexical:** top 50 by `ts_rank_cd`, using an OR of the query's English
   lexemes, so stopwords and unmatched words don't block hits:
   `replace(plainto_tsquery('english', q)::text, ' & ', ' | ')::tsquery`,
   matched against the `to_tsvector('english', content)` GIN index.
2. **Semantic:** top 50 by cosine distance within the guild (or selected
   channel). An HNSW index on the embeddings' first 512 dimensions shortlists
   200 neighbours (iterative scans keep going when filters drop some), and the
   full 1024-dimension vectors rank the shortlist. Afterwards it keeps only
   candidates with similarity ≥ `MIN_SIMILARITY` (0.25). Without a
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

- **Pending** means a searchable (non-bot or relayed, non-empty, undeleted, not
  ignored) message with no row in `message_embeddings`. Pending messages are
  listed in `message_embedding_queue` (migration 0029): replies read the queue,
  not every message. Before it, finding pending messages walked all 236k
  messages from the newest down, 5 to 7 s per reply in production, and
  searches timed out behind it.
- **Queue:** an `AFTER INSERT` trigger queues a new searchable message. A
  saved vector takes its row out in the same statement. The host's query
  checks every condition again. Each reply drops stale rows (already embedded
  or no longer searchable) among the guild's 1,024 newest queued ones, so its
  cost doesn't grow with a backlog. Stale rows past that window go as the
  queue drains to them, or in `kiki-agent-host index`'s full sweep. Both
  queries walk the queue first and look messages up by primary key (a
  LATERAL subquery the planner can't reorder), whatever the statistics say;
  `test_retrieval.py` checks the plan with 20k embedded messages and a drained
  queue. The searchability rule is repeated in the migration's trigger
  function; `test_retrieval.py` checks that the two agree.
- **Ignoring and un-ignoring authors:** nothing reacts to a change in
  `ignored_authors`. A newly ignored author's queued messages are skipped and
  dropped. An un-ignored author's messages are never re-queued by a trigger:
  **run `kiki-agent-host index` after removing someone from
  `ignored_authors`.**
- **Staleness:** an `AFTER UPDATE OF content, deleted_at` trigger deletes the
  vector and queues the message again, or takes it out of the queue if it's
  deleted or empty. That covers every Rust write path (live update and REST
  upsert), so Rust has no indexing code.
- **Write:** an `INSERT … SELECT … FOR SHARE` saves only when `md5(content)`
  still matches the embedded text. A concurrent edit is waited out and then
  rejected.
- **Text:** raw `content`. Discord's 4,000-character limit is far below the
  model's context, so there's no chunking.
- **Query text** gets Qwen's instruction prefix; documents get none.
- **When indexing happens:**
  - When a reply job starts, the guild's 256 newest pending messages are
    embedded in the background, in batches of 64, while the model reads the
    prompt, on a connection of its own (saving vectors means HNSW inserts,
    which took up to 11 s on a cold cache). A search waits up to 2 s for
    indexing, then searches what's already indexed and reports `"partial"`;
    if the run ends without a search, indexing is cancelled. Indexing errors,
    database ones included, also mean `"partial"`, never a failed reply. The
    search works to one 9 s deadline inside its 10 s tool deadline, and each
    step gets at most its cap and never more than is left: the indexing wait
    (2 s), connecting (2 s), the query embedding (4 s, a total; past it the
    search is lexical), the hybrid query (3 s, a `statement_timeout` on the
    search's own connection; past it a lexical query is tried), with 1 s held
    back for that and 1 s for citing. Only when the lexical query also runs
    out is the result an error the model sees. Resolving `person` and `channel`
    (1 s) and citing (what is left) are bounded too; if citing runs out, the
    results come back with the names at hand and channel IDs, marked
    `"enriched": false`. If 256 were found, the guild reports
    `"partial"`. A sleeping service does nothing; a reply can index pending
    messages from other channels in its guild.
  - History uses `kiki-agent-host index` (for example, through `railway ssh`).
    It first queues every searchable message without a vector (the one full
    scan, so it also repairs the queue after a change to `ignored_authors`),
    then walks the queue newest first with a keyset cursor, logs
    progress and cost, and exits. Rerun it any time.
- **Cost:** about 25 tokens per message, so roughly $0.25 per million messages.
  Storage, about 2 GB per million, is the larger cost. A reply's embedding cost
  is added to its `usage.cost`.
- **Scale:** the exact scan read every embedding per search (2.7 s and ~1 GB of
  page cache at 208k messages), so migration 0023 added HNSW on the 512-dimension
  prefix. On production's embeddings the shortlist-and-rerank finds 96% of the
  exact top 50; the index is 241 MB, half the full-width one, so it stays cached
  under Postgres's memory limit.

## Custom emoji

- **Storage:** `guild_emojis` (migration 0017), one row per Discord emoji ID, with
  name, animated/available flags, the raw object, a description and its
  `halfvec(1024)` embedding of `:name: description`. Emoji deleted in Discord get
  `removed_at`: they're excluded from search, but `find_emoji` still explains one
  written out in an old message. A trigger drops the embedding when the name or
  description changes.
- **Sync (Rust):** the Gateway keeps the list current. `GUILD_CREATE`, after every
  new session, carries the whole list, and `GUILD_EMOJIS_UPDATE` (the
  non-privileged `GUILD_EMOJIS_AND_STICKERS` intent) sends it again whenever an emoji
  is added, renamed or deleted. Both replace the list (`emojis::sync_listed`) but
  describe nothing: an event shouldn't spend on a vision model. `/kiki emojis
  resync:true` (Manage Server) replaces the list from `GET guilds/{id}/emojis` and
  describes the new ones; until then, new emoji are found by name only.
  Descriptions survive renames, since an emoji's image never changes. Syncs of a
  guild take turns (an advisory lock), and `guild_emoji_syncs` (migration 0032)
  keeps the latest one's snapshot time. A sync whose list was read earlier is
  skipped, so a resync's REST list, read before a Gateway update was applied,
  can't bring a deleted emoji back. The table also tells a synced guild with no
  emoji from one never synced.
- **Describe (Python):** Rust then calls `POST /v1/emojis/describe` in batches of 8
  until none are left (at most 10 minutes; the interaction token lasts 15). The host
  downloads each emoji as a 128 px PNG (the first frame if animated) and asks
  `OPENROUTER_EMOJI_MODEL` (Rust's env, sent per request like the chat model;
  default `deepseek/deepseek-v4.1-flash-1017`) for one or two sentences on what it shows
  and when people use it. Download and model call are `read_image`'s
  (`media/fetch.py`, `media/vision.py`): Discord hosts only, the type checked from the bytes, and a
  provider error fails just that emoji. Each emoji is tried at most once per resync
  (`describe_attempted_at` against the round's start), so failures don't loop.
  `redescribe:true` also replaces older descriptions. Cost is shown in the command
  reply and kept per emoji (`describe_cost_usd`).
- **Search:** like message search, full text (name with `_` as spaces, plus the
  description) and vectors fused by reciprocal rank, `MIN_SIMILARITY` floor, no
  recency. Only the guild's current, available emoji. Undescribed emoji match by
  name only. Pending embeddings (renames) are filled in at search time.
- **Prompt:** one stable instructions line tells Kiki the tools exist, to search
  for an emoji that fits the moment rather than reuse familiar ones, and to write
  emoji exactly as returned. The emoji list itself is not in the prompt, and
  neither are emoji in `guild_lingo` (migration 0022 removed them): a fixed few
  listed in every prompt became all she used. The run context names emoji she
  has used at least twice in the messages shown (text or reactions) and asks her
  to give them a rest.
- **Checked before posting:** Discord only renders a custom emoji whose ID Kiki
  can use. Anything else is posted as text, or silently left off as a reaction:
  an invented `<:salute:1>` (Discord stores it as `:salute:`), a `<:huh:>` with
  no ID, or a `:skull:` shortcode, which Discord never converts in a bot's
  message. So the reply agent has an output validator (`tools.check_emojis`,
  `chat/emojis.py` `check`). It compares every custom emoji and `:shortcode:`
  (run together too, like `:a::b:`) against the catalog as it is when each
  answer arrives. It skips code (CommonMark fences and backtick runs, with
  escapes; `chat/markdown.py`), links and quotes, which aren't Kiki's own words.
  A markdown link's destination is skipped from `](` to its balanced closing
  parenthesis, and a bare URL up to whitespace or `<` (which a URL can't
  contain unescaped), so an emoji written right after a link is still checked.
  Every scan is linear, so replies of any length are checked.
  - A known name is rewritten as Discord needs it: its real ID, the `a` for an
    animated emoji, and its exact name.
  - The catalog is read again for every answer that writes emoji, so one the
    Gateway added or deleted mid-run counts.
  - An unusable emoji raises `ModelRetry`, naming close matches, while the run has
    an output retry and a model request left. A retry costs at least one request,
    more if the model searches first. `LastRequestAnswers` sends no function tools
    on the last request the limit allows (OpenRouter's web search and fetch stay
    on; they run within the request), so a retry can't push the run over it.
  - Otherwise the emoji is cut. Only the gap is tidied, so code stays verbatim. A
    `react:` line keeps its good emoji and drops leftover commas, a line left empty
    is dropped, and so is a `---` separator with no message left on one side.
    The reply keeps its kind: a message left with nothing to say becomes 🙂, and a
    reaction-only answer left with no reaction reacts 🙂. Silence instead would
    ignore a server's `chat_allow_silence` and a direct question.
  - Until a server's list is first synced, written-out emoji can't be checked and
    pass. The emoji tools are offered (a toolset `prepare`) only when it has some.

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
   - Web tools: pick engines explicitly (e.g. Exa for search, `openrouter` for free
     fetch) once logs show cost and quality.
   - Image results: done for generated images ([image generation](image-generation.md));
     search results still carry no images.
   - `read_image` on old messages: Discord's signed attachment URLs expire
     after about a day. Refreshing them needs the bot token
     (`POST /attachments/refresh-urls`), which only Rust has.
   - Caching image descriptions per attachment, if images get re-read often.
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
