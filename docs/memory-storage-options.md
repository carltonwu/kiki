# Storage and retrieval options for Kiki's memory

2026-10-17 · Research and design proposal, not an implemented or benchmarked system.

## Recommendation after widening the search

Make a temporal evidence graph a first-class part of Kiki's memory, alongside searchable conversations and source messages. The earlier proposal was too quick to dismiss a dedicated graph database. It is a serious option for relationship-heavy retrieval and for making temporal reasoning easier to implement and inspect.

My quality-first prototype choice is **Postgres as the authoritative message/revision store plus Neo4j as a rebuildable temporal graph/search projection**. Evaluate Graphiti as an accelerator, not as the owner of Discord ingestion semantics. Keep existing full-text/vector message retrieval as an independent evidence path. Use Neo4j's own vector and full-text facilities for the graph prototype before adding another search service. Qdrant is the strongest separate retrieval-engine experiment if dense/sparse fusion and multivector reranking improve measured recall.

Compare that against a genuinely equivalent **Postgres-only implementation of the same graph and retrieval policy**. A native graph engine has no intrinsic answer-quality advantage if both systems retrieve the same evidence. Its potential advantages are traversal execution, query expressiveness, inspection and implementation correctness. If it improves none of those materially, remove the extra service. Do not compare a sophisticated graph pipeline against a deliberately weak SQL baseline and attribute the difference to the database.

The most interesting alternate design is **SQLite plus LanceDB and immutable episode files**: a compact, inspectable memory service on one durable host. A more radical **XTDB plus a search projection** deserves consideration if historical state and retroactive corrections become the central product feature.

No live dataset or vendor performance benchmark was run. Capability statements below come from linked primary documentation. Fit, complexity and cost judgments are architectural assessments, not measured vendor rankings.

## Separate four decisions

1. **Memory representation:** raw messages, episodes, topics, entities, claims, evidence, temporal intervals, summaries.
2. **Retrieval policy:** lexical/dense candidates, graph expansion, temporal filters, reranking, uncertainty and token allocation.
3. **Physical engine:** Postgres, Neo4j, Qdrant, LanceDB, SQLite, files, etc.
4. **Consolidation process:** how knowledge is extracted, revised, linked and invalidated.

An excellent database cannot recover a cancellation that extraction discarded. A graph can amplify an incorrect entity merge. A vector index can retrieve an old decision very accurately while missing the subsequent correction. A readable Markdown page can confidently summarize a joke as a preference. Evaluate these failure modes independently.

## What a graph should represent

Build two connected layers, with different trust levels:

- **Structural graph:** message replies, authorship by Discord ID, mentions, thread containment, chronological sequence, episode membership and source revisions. These relationships mostly come from the source and deterministic code.
- **Interpretive graph:** the event being discussed, proposed/confirmed/cancelled plans, recurring topics, participant roles, contradiction, continuation and explicit supersession. These relationships are inferred and need attribution, confidence and source evidence.

Use first-class assertion records/nodes for consequential knowledge. A bare edge `Alex → likes → climbing` loses the distinction between Alex's statement, somebody else's joke, a quote and Kiki's inference. Instead represent an attributed assertion, its subject and object, the source revisions supporting it, and relevant times. Keep conflicting assertions when the evidence does not establish which supersedes which.

Example questions the graph helps answer:

- “Why did we change venues?”: current booking → replaced proposal → reason → supporting exchange.
- “What happened to the project Bea mentioned last month?”: Bea → source episode → project → later episodes about the same project.
- “Who volunteered before we moved the date?”: event → dated proposal/volunteer assertions → date-change edge → preceding evidence.
- “What other conversations relate to this?”: episode → distinctive entities/topics → related episodes, with a bounded traversal.

Separate an event instance such as the October dinner from its recurring topic such as group dinners. Otherwise a graph can incorrectly use last month's reservation to answer this month's question. Co-participation is evidence of a shared exchange, not proof that people are friends. A similarity edge is a navigation hint, not a factual relationship.

Preserve the earlier design's three clocks: message time, source observation time, and referenced event/validity time. Also record projection generation time separately. A graph rebuilt today must not claim Kiki first learned every fact today.

## Engine families and their best roles

### Relational: Postgres plus pgvector and edge tables

Postgres can hold the full logical model: message revisions, evidence dependencies, temporal intervals, graph nodes/edges, full text, and vectors. Temporal ranges and recursive queries are native SQL tools; pgvector offers exact and approximate similarity search. [Postgres ranges](https://www.postgresql.org/docs/current/rangetypes.html), [recursive queries](https://www.postgresql.org/docs/current/queries-with.html), [pgvector](https://github.com/pgvector/pgvector).

This is the cleanest consistency boundary: publishing repaired memberships, source dependencies, summary generations and queue work can happen in one transaction. The advantage is reliability and flexibility, not merely avoiding a new database. Fixed-shape and bounded graph queries are straightforward; increasingly exploratory, variable-depth patterns can become harder to maintain and tune.

A quality trap is filtered approximate vector search. Filtering by a narrow channel, time range or generation can leave too few ANN results. pgvector explicitly documents this behavior and iterative scans; selective exact search or partitioning may be better. Measure recall against exact search on Kiki's real filters rather than assuming an ANN index is an unconditional upgrade. Plain Postgres full-text ranking is also not interchangeable with a tuned BM25/sparse retrieval pipeline.

[Apache AGE](https://age.apache.org/) adds an openCypher graph layer to Postgres. It is worth a spike if Cypher inside the existing transaction boundary is attractive. Verify supported Postgres versions, extension availability, plans and pgvector interoperability on the actual deployment. Do not assume the combined planner behaves like a purpose-built graph/vector query engine.

### Native property graph: Neo4j or FalkorDB

Neo4j makes paths, relationship types and graph patterns natural to express, and supplies full-text and vector indexes. It can be the graph and search projection together. Dates and relationship properties still need a temporal model: supporting timestamps is not automatic bitemporal history. [Vector indexes](https://neo4j.com/docs/cypher-manual/current/indexes/search-indexes/vector-indexes/), [full-text indexes](https://neo4j.com/docs/cypher-manual/current/indexes/search-indexes/full-text-indexes/).

This is my preferred graph prototype because Kiki's hard questions often involve following a plan or entity through several exchanges. Test whether Cypher materially simplifies these queries and whether traversal is faster under realistic bounds. Keep source transcripts in Postgres; graph nodes can reference message revisions instead of copying all raw JSON.

FalkorDB is another candidate supported by Graphiti, including an embedded option. Its temporal representation differs from Neo4j: the project's migration example converts temporal values into numeric timestamps. Compare persistence/recovery, graph traversal, filtering and operating memory on the same corpus rather than accepting an unsupported claim that it must be cheaper. [Graphiti backends](https://github.com/getzep/graphiti), [FalkorDB migration notes](https://github.com/FalkorDB/migrate-neo4j-falkordb).

Graph engines do not eliminate extraction cost, topic churn, source deletion or out-of-order repair. High-degree topics such as “games” can flood traversals with irrelevant evidence. Use typed edges, scope/time restrictions, degree limits and confidence thresholds. Preserve a raw-text path when graph extraction misses a detail.

### Graph memory framework: Graphiti

Graphiti is a library, not a graph database. It provides incremental temporal graph construction, provenance and hybrid retrieval over supported graph backends. Its prescribed entity/edge types are useful for plans, projects and attributed statements. Managed Zep is a separate product; its hosted infrastructure and benchmark claims should not be treated as guarantees for a self-hosted Graphiti deployment. [Official repository](https://github.com/getzep/graphiti).

The currently inspected bulk ingestion path includes date extraction and invalidation. Do not rely on older descriptions claiming bulk mode omits those. Its episode removal code is not evidence of a complete inverse operation restoring every previously invalidated assertion or summary. Kiki needs explicit tests for deleting a cancelling message, late historical contradictions, shared evidence, stable identities and ACL filtering. [Current implementation](https://github.com/getzep/graphiti/blob/main/graphiti_core/graphiti.py).

Use it behind Kiki's source/revision boundary. Compare stock behavior, a constrained adapter, and selected borrowed components before committing. Pin a reviewed release/commit for any experiment; inspected `main` is not a deployment specification. Conversation snapshots may be more useful extraction inputs than single chat fragments, while active conversations need debounced deltas and revision invalidation.

### Dedicated vector/hybrid retrieval: Qdrant

Qdrant's meaningful opportunity here is search quality: dense and sparse candidates, multi-stage fusion, and multivector late-interaction rescoring. It can compare several representations of a conversation: intent, summary and source spans. [Hybrid query documentation](https://qdrant.tech/documentation/search/hybrid-queries/).

A candidate pipeline is dense+sparse top candidates → multivector or cross-encoder rerank → temporal graph expansion → evidence selection. A dedicated vector engine does not itself infer which plan superseded another, and payload metadata is not a substitute for all evidence joins. Use it as a rebuildable search projection whose hits are validated against the authoritative source.

Multivectors can increase storage and computation substantially; rerank a bounded candidate set rather than indexing every token of all history without evidence of benefit. A better embedding or reranker can improve results in Postgres too. Compare the same model and candidate budgets when isolating the database's contribution. Do not add Qdrant simply to duplicate a working single-vector index.

### File-backed vector retrieval: LanceDB

LanceDB supports hybrid full-text/vector search with reranking and can use local or object-backed storage. This makes “file based” a real retrieval architecture rather than a directory of text that must be scanned on every request. [Hybrid search](https://docs.lancedb.com/search/hybrid-search), [storage architecture](https://docs.lancedb.com/storage).

It is attractive for versioned episode/chunk datasets, offline backfill, and embeddings over attachments or other media. A single memory service can maintain its local index and publish snapshot manifests. Keep transactional source bookkeeping in SQLite or Postgres; a table snapshot does not automatically atomically version all graph edges, source revisions and derived topic summaries.

Storage and concurrency details vary by backend/version. Current docs describe native concurrent S3 writes; do not assume all object stores or older clients behave identically. For low-latency live retrieval, test a warm local cache and measure cold starts/object requests. This design can have high answer quality with the same retrieval policy, but needs careful manifest publication and dependency invalidation.

### Files as an explicit memory filesystem

Use structured episode records and generated topic/person/event pages, for example:

```text
source/<channel>/<year-month>/<segment>.jsonl.zst
episodes/<episode-id>/<version>/record.json
episodes/<episode-id>/<version>/transcript.md
topics/<scope>/<topic-id>/timeline.md
entities/<scope>/<entity-id>/index.json
indexes/<generation>/...
manifests/<scope>/current.json
```

This is a proposed format, not a library promise. JSON records carry source IDs/revisions, interval bounds, dependencies, scope and confidence. Markdown is a generated view for inspection and selective agent navigation. The model may read a topic overview then open the relevant episode; it should not receive unrestricted ability to rewrite canonical evidence.

Benefits: portable, easy to inspect, easy to export and easy to compare with human judgments. Problems: global relevance search, concurrent writers, invalidation and consistent multi-file views. Solve them with a single writer or transactional coordinator, immutable generations, an atomic manifest pointer, and lexical/vector indexes. Filesystem rename and object-store publication need different implementations. Batch source events into segments; one tiny object per message can create avoidable I/O and listing overhead.

Agent-led file browsing is useful for deeper investigations but has variable latency and token cost. Always-grepping the entire archive lacks semantic recall. A single mutable `memory.md` loses chronology, precise evidence and conflict history. Git is useful for curated schemas/prompts and small reviewed summaries; it is not the preferred message-ingestion database or deletion mechanism.

### Embedded relational: SQLite plus FTS5, optional sqlite-vec

SQLite is a relational database stored in a file. It can represent the same evidence graph and versioned episodes. FTS5 supplies lexical retrieval, and sqlite-vec is a vector extension. [FTS5](https://sqlite.org/fts5.html), [sqlite-vec](https://github.com/asg017/sqlite-vec).

This is the strongest minimal-service design if one memory process owns the durable volume and serves retrieval over HTTP. It is not inherently lower-quality memory than a hosted database. WAL permits concurrent readers with a writer, but still only one writer at a time and requires same-host shared memory; casually sharing the database across hosts is inappropriate. Backups must include a consistent database/WAL state. [SQLite WAL](https://www.sqlite.org/wal.html).

Because Kiki currently has Rust and Python services, either centralize ownership or expose a memory API. Do not let deployment overlap or separate containers turn one local file into an accidental distributed database. Evaluate vector extension maturity and features in the pinned version rather than treating it as a drop-in replacement for every specialized retrieval engine.

### Temporal database: XTDB

XTDB directly models valid time and system time, including backdated changes and historical views. It could replace much of Kiki's hand-built bitemporal bookkeeping. [SQL temporal overview](https://docs.xtdb.com/quickstart/sql-overview.html).

This is compelling when “what was the plan then, according to what we knew at that time?” becomes common. It does not identify conversation boundaries, understand a joke or infer a referenced event date. Row valid-time must be deliberately mapped to the domain; message send time, assertion validity and event occurrence may all differ. A separate retrieval layer may still be appropriate. Prototype this if the complexity of temporal SQL becomes the dominant engineering burden; it is not the first graph experiment I would run.

### Other useful directions

- **Vespa:** search-engine-centered architecture for BM25, vector candidates, filters and configurable ranking in one serving system. Interesting if retrieval/ranking experimentation dominates; a broader operational commitment than Kiki initially needs. [Official hybrid tutorial](https://docs.vespa.ai/en/learn/tutorials/hybrid-search).
- **SurrealDB:** a multi-model candidate combining document, graph, vector and full-text access. Attractive for a greenfield single-engine design. Verify the actual temporal/versioning model, query plans and recovery behavior rather than equating a feature checklist with a proven end-to-end memory system. [Official overview](https://surrealdb.com/docs/what-is-surrealdb).
- **DuckDB plus Parquet:** excellent for analyzing archive distributions, replaying experiments and evaluating grouping. Use it beside the live store; validate its process/writer model before making it the shared operational database. [Parquet support](https://duckdb.org/docs/extensions/parquet), [concurrency](https://duckdb.org/docs/stable/connect/concurrency).
- **Object storage:** useful for compressed source archives, attachments and reproducible derived snapshots. It is a storage tier, not a complete relevant-memory retrieval strategy. Keep a searchable active projection and an explicit source-of-truth policy.

## Research ideas worth borrowing independently of the engine

- **HippoRAG 2:** joins passages with an associative graph and uses Personalized PageRank-based retrieval. This is a plausible way to find a related episode that lacks the query's exact words. Its reported benchmark improvements do not establish correctness for Discord temporal updates. Test a bounded, scope-aware form against simpler graph expansion. [Paper](https://arxiv.org/abs/2502.14802).
- **RAPTOR:** organizes retrieval at several summary levels. Adapt the idea to spans → episodes → topic-period summaries, while retaining direct access to source text. A repair should rebuild only dependent branches. Do not assume conversations form one permanent tree; topic membership can overlap. [Paper](https://arxiv.org/abs/2401.18059).
- **Microsoft GraphRAG:** local graph+text search and community summaries support different question types. Broad questions such as “what has the server been excited about recently?” can use period-specific community summaries. Global map/reduce search is unnecessary for ordinary banter. Microsoft identifies extraction as a major indexing expense; reserve that work for useful memory. [Query modes](https://microsoft.github.io/graphrag/query/overview/), [indexing methods](https://microsoft.github.io/graphrag/index/methods/).
- **Zep's temporal memory architecture:** directly relevant work on evolving conversational knowledge and temporal graph retrieval. Treat the paper as evidence for studying the architecture, not proof that any graph backend beats an equivalently implemented relational system. [Paper](https://arxiv.org/abs/2501.13956).

## Recommended retrieval behavior

Route by information need, not by database branding. Banter may require only recent raw context. A direct historical lookup starts with lexical/vector search. A current-plan query follows the specific event's correction chain. An associative question expands graph neighbors. A broad retrospective uses dated topic/community summaries and samples supporting episodes.

Fuse candidate IDs from independent paths; validate authorization, time mode, source revision and active generation; expand essential evidence; rerank; then pack a bounded context. Typically allow one targeted expansion when evidence is incomplete rather than an unbounded autonomous database exploration loop. Evaluate a bounded cross-encoder reranker before spending on repeated LLM reranking. Keep contradictions visible when unresolved.

## Multi-store correctness

Adding a graph or vector service creates a consistency boundary. Use a transactional outbox in the canonical store and idempotent consumers. Give every projection record source revisions, scope and generation metadata. Publish rebuilt generations deliberately. A deletion marks affected derived artifacts invalid in the canonical dependency catalog immediately; retrieval must check this even if a remote index still returns a stale ID.

Never solve a failed projection write by rolling back an already accepted Discord observation. Let it retry; live responses fall back to valid raw evidence. Backfill uses the same projection rules, canonical event ordering and tests as live ingestion, with lower scheduling priority. Out-of-order data may require rebuilding an affected component; an incremental library API is not itself a convergence proof.

ACL rules and invalidation apply to summaries and graph paths, not only final citations. A public response cannot use a community summary containing private-channel evidence. Initial scopes should stay channel-local unless cross-channel authorization is explicitly implemented.

## Cost: optimize useful work before storage pennies

Count ingestion extraction, consolidation, embeddings, query reranking, prompt tokens, reprocessing, storage, compute, backups and operator effort. Separate one-time archive processing from steady-state live cost. More engines increase persistent memory and backup work, but extracting low-value assertions from every chat fragment can be the larger avoidable expense; measure both.

Illustrative storage arithmetic, **not the current guild size**: 500,000 vectors at 1,024 half-precision dimensions require about 1.03 GB for pgvector values alone (`2d + 8` bytes each). 25,000 episode vectors require about 51.4 MB. Neither figure includes table rows, text, ANN indexes, replicas or backups. Embedding only summaries saves storage but can lose detail; use summary+chunk representations and retain lexical access to every source message. [pgvector type sizes](https://github.com/pgvector/pgvector).

Batching twenty messages does not make their source tokens disappear. It reduces request count and duplicated extraction context, while potentially improving interpretability. Cache by canonical input hash, debounce provisional updates, extract durable assertions selectively, and consolidate only dirty dependencies. Quantization and model size reductions should pass an evidence-recall evaluation before adoption.

Qdrant currently advertises a prototype free tier with 1 GB RAM and 4 GB disk; this is useful for an experiment, not proof that Kiki fits or that it provides production availability. [Current pricing](https://qdrant.tech/pricing/). Neo4j's current pricing page does not expose a simple dollar figure in the fetched content; no monthly quote is asserted here. [Current pricing](https://neo4j.com/pricing/). For self-hosted choices, benchmark actual RSS, disk, I/O and backup size rather than treating zero license cost as zero operating cost.

## Concrete comparison experiment

Build a representative, authorized corpus and 100–200 adjudicated questions covering exact recall, interleaved chat, indirect associations, revised plans, event dates, conflicting claims, renames, broad retrospectives and unanswerable questions. Include shuffled backfills, edits and deletes. Use the same corpus and generator with fixed token budgets.

Compare in stages:

1. Improved temporal hybrid retrieval with episode expansion, before adding learned graph facts.
2. The same system plus a constrained evidence graph in Postgres.
3. Equivalent graph data and query policy in Neo4j; separately evaluate a Graphiti adapter's extraction and repair behavior.
4. Add Qdrant multirepresentation/multivector retrieval or a reranker to the best candidate, testing the quality change separately from engine latency.
5. If host consolidation matters, reproduce the winning policy with SQLite/LanceDB as an optional deployment experiment.

Measure evidence recall per query category, final-answer support and temporal correctness, abstention, source deletion convergence, p95 latency, backfill throughput and total operating cost. Do not use a weighted average that hides a temporal regression behind improved easy lookups. Choose the simplest candidate that meets quality and reliability requirements; spend more where a measured gain justifies it.

The first implementation should target the graph-aware architecture above, not commit to the most elaborate stack. PostgreSQL-only, native graph and file-backed designs can all reach high quality if they preserve the same evidence and retrieval semantics. The unanswered question is which makes those semantics easiest to implement correctly for Kiki.

---

## Independent review — Claude Opus 5.5 (medium)

2026-10-17 · Written by Claude Opus 5.5 at medium effort, delegated as a reviewer. **Everything above this line is the original author's proposal and is unchanged.** This section is my opinion unless marked as a fact with a source or a code reference. I ran no retrieval benchmarks. I checked the claims above that I could and found nothing objectively false to correct (details under *What I checked*).

### Summary

The memory model in this document and in [the temporal HLD](temporal-memory-hld.md) is good: episodes separate from response routing, many-to-many topics, evidence-linked assertions, separate clocks, repair on late data, and raw-text search kept as a fallback. I agree with almost all of it.

**I disagree with making Neo4j (or any second database) the first prototype.** For a Discord bot serving a few guilds, the engine is about the fifth most important factor in retrieval quality. Four things come first, and none of them needs a new engine:

1. There is no evaluation set, so no change can be shown to help.
2. Short Discord messages are embedded without their context.
3. Messages are not grouped into episodes, so retrieval returns isolated lines.
4. Kiki retrieves only when the model decides to call a tool; nothing is retrieved automatically before a reply.

My recommendation is **one Postgres database, evaluation first, episode-centric, with graph algorithms run in the application process over edge tables**. Add a graph or vector engine only when a measured result calls for it. The criteria are listed under *What would change my recommendation*.

### Facts from the code that shape the recommendation

These come from reading the workspace source, not a production census:

- **Unknown scale.** The local development database has 648 messages in 7 channels (checked with `psql` against the docker-compose database). I did not query production. Run one `SELECT count(*), count(DISTINCT channel_id) FROM messages` on production before choosing any engine. If the answer is in the hundreds of thousands, a channel's whole graph fits in application memory, and both "traversal performance" and "ANN recall under filters" stop mattering.
- **Messages are embedded alone** (`chat/indexing.py` `PENDING` and `openrouter/embeddings.py` `Embedder`). Each message's `content` is embedded by itself. In chat, "yeah let's do Pine instead" or "lol same" carries almost no searchable meaning on its own. I think this is the largest single quality loss in the current retriever.
- **Kiki's own messages can never be found by search** (`SCOPE` has `NOT m.author_bot`). Kiki's past answers and commitments ("I'll remind you Friday") are part of what users expect it to remember. Index them, labelled as bot-authored, and exclude them from the human-evidence count, as the HLD already recommends for assertions.
- **Recency is measured from database `now()`** in `SEARCH`. A historical replay or an "as of T" evaluation therefore cannot reproduce ranking. Pass the reference time as a parameter.
- **Exact vector scan.** There is no ANN index, so the semantic branch does an exact cosine scan with a similarity cutoff. At small scale this is a quality advantage, not a defect. The doc's warning about filtered ANN recall only applies once an index is added. Don't add one before latency requires it.
- **No reaction, pin or attachment signal.** The Gateway intents are `GUILDS | GUILD_MESSAGES | MESSAGE_CONTENT` (`gateway.rs:41`), so live reaction events are not received. Only the `content` column is searched; attachments, embeds and link titles inside `raw` are not. Reactions and pins are cheap human-supplied salience and agreement signals that neither design doc mentions.
- **Source correctness gaps** are as the HLD describes (`store.rs`): `update_message` has no staleness guard, and `delete_messages` is an `UPDATE` that does nothing if the row isn't there yet. I agree these come before any memory layer. The `drop_stale_embedding` trigger in `0007_retrieval.sql` is a good precedent: derived data invalidated in the same transaction as the source change. Generalize that pattern rather than adding an outbox to a second store.

### Where I disagree

**1. Neo4j as the default prototype (strong disagreement).** The doc is careful to say a graph engine has no inherent quality advantage. Given that, I think the burden of proof should be reversed: build the Postgres version first and spike Neo4j only if a specific query becomes painful. The costs of a second store are certain:

- an outbox and idempotent consumers;
- copying ACL scope onto graph paths;
- propagating deletions to a second index;
- one more Railway service with its own memory and backups;
- an engine that adds no correctness guarantees.

That last point is a fact: Neo4j Community lacks the Enterprise online-backup feature ([Neo4j operations manual](https://neo4j.com/docs/operations-manual/current/introduction/)). That is acceptable for a projection you can rebuild, but it confirms Neo4j would be a cache, not a source of truth. The likely traversals (correction chains, reply ancestry, episode→topic→episode with depth 2–3) are a few indexed joins or a short recursive CTE.

**2. A general entity/relation knowledge graph over banter (moderate disagreement).** Graphiti-style triple extraction run on every exchange is where cost and false state accumulate. The questions the doc lists as motivation ("why did we change venues", "who volunteered before we moved the date") are about **state tracking**, not general entities. Specifically: plans, decisions, commitments, open questions, and the supersession links between them. I would build a narrow, typed `state` layer with about five assertion kinds and explicit `supersedes` / `cancels` edges, and skip generic entity extraction. Topics and episodes already cover the associative questions.

**3. Bitemporal "what did Kiki know at time T" (mild disagreement on priority).** Store observation time, which an append-only `message_changes` log gives you for free. But don't build query modes, cutoff-aware summaries or generation pinning for it until a real user need appears. Message time and referenced-event time drive almost every real question. Observation-time replay is mostly an evaluation tool. That also lowers XTDB's appeal further.

**4. Lifecycle complexity (mild).** Episodes and summaries are derived and keyed by input hash, so `open → cooling → sealed` is a **cost policy**: it decides when to spend on expensive consolidation. It is not a correctness state. Correctness should come from one rule: *a derived artifact is valid only while the hash of its inputs matches*. The verified-coverage frontier is worth keeping, because it stops consolidation from running on half-loaded history. Implement it as one per-channel `checked_through_time` column, not a subsystem.

**5. Graphiti.** I verified the doc's claims against current `main` ([graphiti.py](https://github.com/getzep/graphiti/blob/main/graphiti_core/graphiti.py)):

- The `add_episode_bulk` docstring says the bulk path also does edge invalidation and `valid_at`/`invalid_at` extraction.
- `remove_episode` deletes only edges whose *first* episode is the removed one. It contains no logic to restore edges that the removed episode invalidated.

So deleting a cancelling message leaves the original plan invalidated. That is exactly the deletion-repair case Kiki needs, and it fails. I would not use Graphiti as the owner of state. Borrowing its prompts and edge-type ideas is reasonable.

### Ideas missing from both documents

- **Contextual embeddings and lexical indexing** ([Anthropic, Contextual Retrieval](https://www.anthropic.com/news/contextual-retrieval)). Before embedding, prepend a short context to each unit, such as the episode label, participants, the replied-to message and the previous few lines. The post reports these reductions in top-20 retrieval failures on its benchmarks:

  | Change | Failure rate | Reduction |
  | --- | --- | --- |
  | Baseline | 5.7% | — |
  | Contextual embeddings | 3.7% | 35% |
  | Plus contextual BM25 | 2.9% | 49% |
  | Plus reranking | 1.9% | 67% |

  Those are document corpora, not chat. Treat the numbers as a hypothesis for Kiki, but this is the cheapest likely improvement available. A deterministic version that uses no LLM, prepending the reply target and preceding two lines, costs nothing extra to try.
- **Segment-level memory units** ([SeCom, Pan et al. 2025](https://arxiv.org/abs/2502.05589)). The paper finds that turn-level, session-level and summary-level memory units all fall short. It reports better results on LOCOMO and Long-MT-Bench+ from topically coherent segments plus denoising. That is direct evidence for the HLD's `memory_chunks`, which are bounded spans inside an episode. **Retrieve and inject segments, not single messages.**
- **Surprise-based boundaries plus contiguous retrieval** ([EM-LLM, Fountas et al., ICLR 2025](https://arxiv.org/abs/2407.09450)). This puts the brain analogy into practice. It sets event boundaries from model surprise, refines them with graph methods, and retrieves by similarity *plus* temporally adjacent events. For Kiki, a cheap embedding-shift or small-model boundary score can propose segment splits before any LLM judge runs.
- **A long-context baseline.** If a channel's whole history of episode summaries fits in a few tens of thousands of tokens, the strongest baseline may be "put the dated digest of the whole channel in a cached prompt prefix and let the model read it." Prompt caching makes a stable prefix cheap. Every retrieval design should beat this on the evaluation, or the extra machinery isn't paying for itself. Neither document considers it.
- **Retrieval before every reply**, with a gate. The HLD says this, but it should be the *first* product change because it fixes "the model didn't know to search". Use a cheap check: does the query mention the past, a person, a plan or a named thing? If so, run hybrid retrieval and inject at most about 1–2k tokens of dated segments.
- **Time-expression resolution in queries** ("last month", "before the move", "at the October dinner"). Turn these into `before`/`after` bounds or into event anchors (an episode ID) before searching. The current tool takes explicit dates only. This step drives the temporal-reasoning category of LongMemEval more than any storage choice does.
- **Postgres 18 temporal keys** (Kiki already runs Postgres 18 per `docker-compose.yml`). `PRIMARY KEY (plan_id, valid_at WITHOUT OVERLAPS)` and `FOREIGN KEY (..., PERIOD valid_at)` enforce "one current venue per plan at a time" in the database ([CREATE TABLE, PG 18](https://www.postgresql.org/docs/18/sql-createtable.html)). Caveat from the same page: temporal foreign keys don't support `CASCADE` or `SET NULL` actions. This narrows the gap with XTDB for the specific state-tracking layer I recommend.
- **Salience signals.** Reactions (they need the `GUILD_MESSAGE_REACTIONS` intent), pins, thread titles and channel topics are human-written labels and importance signals. They can help both topic labelling and consolidation priority, which is a more grounded "prioritized replay" signal than a model-estimated utility.
- **Attachment and link text.** Index captions, filenames, embed titles and descriptions from `raw`. Many plan messages are just a link.

### What the brain analogy actually contributes

In my view, four ideas from the research pay off in engineering terms, each with a concrete mechanism:

- **Event segmentation** gives you episodes and segments with boundaries detected from shifts in content (EM-LLM, SeCom, Baldassano).
- **Temporal contiguity** means expanding a retrieved hit to its neighbors and reply ancestors. This is arguably the single most useful mechanism.
- **Complementary learning systems** means fast raw storage plus slow consolidation that never discards the source episodes.
- **Pattern separation** means keeping the October dinner distinct from the dinner topic. The doc already says this; the name is useful as a test category.

Two other terms are only evocative. "Replay" and "prioritization" here are just job scheduling, and decay is just a ranking feature. One idea should be actively avoided: **reconsolidation**, where retrieval rewrites memory. Retrieval must never mutate stored evidence or summaries. The HLD already warns against counting repeated retrieval as corroboration, and I would make that a hard invariant.

### My favored architecture

All of this lives in one Postgres database, with logical records as in the HLD:

```text
messages + message_changes (append-only, tombstones-before-create)   ← source, transactional
segments  (episode_id, span, bounds, input_hash, contextual_text, tsvector, embedding)
episodes  (bounds, participants, label, summary, input_hash, state)
edges     (src, dst, kind: reply|continues|references|topic|supersedes|cancels, evidence, confidence)
topics    (stable id, aliases, period summaries with evidence)
state     (typed plan/decision/commitment assertions; valid_at range; WITHOUT OVERLAPS where it fits)
memory_work (queue rows written in the same transaction as source changes; SKIP LOCKED workers)
```

Retrieval runs as follows:

1. Parse time expressions and resolve people to IDs.
2. Run BM25-ish full text and exact vector search over **contextual segments**, plus the existing raw-message branch.
3. Expand along edges to depth 2, following `supersedes`/`cancels` forward for current-state questions.
4. Optionally run personalized PageRank over the scope-filtered subgraph, loaded into the worker in memory. At this scale that takes milliseconds and replaces the main argument for HippoRAG-on-Neo4j.
5. Rerank with a cross-encoder.
6. Pack dated segments under a token budget.

Every derived row carries an `input_hash`. Workers recompute any row whose inputs changed. Invalidation for deletes and edits happens in the source transaction, following the precedent of the existing embedding trigger.

### Implementation order

0. **Evaluation harness first.** Build 100–200 adjudicated questions from the real guild, using the categories above plus LongMemEval's ([paper](https://arxiv.org/abs/2410.10813)), as the doc proposes. Add a shuffled, duplicated and deleted replay suite for convergence. Score the current retriever and the long-context baseline. Without this step, every later step is guesswork.
1. **Source correctness.** Add `message_changes`, tombstones, staleness guards on partial updates, and stop excluding the whole current conversation from search.
2. **Context and timing.** Retrieve automatically before replies, add timestamps and reply edges to the prompt, include Kiki's own messages, and pass the reference time explicitly.
3. **Episodes and segments.** Combine deterministic temporal/reply rules with a small model for ambiguous boundaries. Use contextual segment embeddings and index attachment text. **Measure.**
4. **Reranker and edge expansion,** including correction-chain following. **Measure.**
5. **Topics and period summaries** with evidence, plus a narrow `state` layer for plans, decisions and commitments. **Measure** the update/temporal categories in particular.
6. Only if steps 3–5 leave a multi-hop or associative gap: in-process PPR first, then a graph-engine spike.

### What would change my recommendation

- **Production scale.** More than roughly 5–10M messages, or many guilds with cross-guild memory, would make a dedicated vector or graph engine's indexing worth revisiting. Even then I'd try pgvector HNSW with iterative scans first.
- **Measured traversal pain.** If the depth-2/3 edge queries Kiki needs exceed about 100 ms p95 in Postgres with proper indexes, or the SQL becomes hard to maintain, I would favor a Neo4j or AGE spike.
- **Measured multi-hop gap.** If graph or PPR expansion improves evidence recall on multi-hop/associative questions by ≥10 points over segment and neighbor expansion at equal token budget, I would invest in the graph layer. The engine would still be chosen by the traversal criterion above.
- **Long-context baseline wins.** If the cached whole-channel digest matches retrieval recall at acceptable cost, I'd ship that and shrink the retrieval stack to the digest plus raw search.
- **No gain from context.** If contextual segment embeddings show no gain on Kiki's data, drop them and spend on a reranker instead.
- **Graphiti passes repair tests.** If a pinned Graphiti release passes Kiki's deletion and late-insert test suite (delete-the-cancellation, backdated contradiction), I would reconsider it as an accelerator.
- **Retrospection becomes central.** If "what did we believe then" becomes a frequent user request, I'd move observation-time querying up, and XTDB or PG 18 temporal tables become relevant.

### What I checked and what I didn't

**Verified:**

- Graphiti's `add_episode_bulk` docstring and `remove_episode` code on current `main`. The doc's claims are accurate.
- The Neo4j Community vs Enterprise feature split (online backup).
- PG 18 `WITHOUT OVERLAPS` and `PERIOD` semantics.
- The SeCom, EM-LLM and Contextual Retrieval claims, quoted above.
- The pgvector storage arithmetic in the cost section (`2d+8` bytes × 500k ≈ 1.03 GB; × 25k ≈ 51.4 MB). It is correct.
- The code facts: `store.rs`, `retrieval.py` (now `chat/`), migrations 0001/0007/0008, Gateway intents, and local DB counts.

**Not verified:**

- Production data volume.
- FalkorDB, LanceDB, Qdrant, Vespa and SurrealDB specifics. I took the doc's statements as given and didn't re-check them.
- Any latency or recall number for Kiki. Every threshold in this section, including 10 points, 100 ms and 5–10M, is my judgment, not a measurement.

### Parent verification notes — Codex

I read the completed review and checked its linked research references and PostgreSQL 18 syntax. Claude's recommendation and disagreements above are preserved. These qualifications apply when turning the review into an implementation:

- **Scale does not remove the need to measure.** Fitting an adjacency graph into application memory does not establish vector-scan latency, graph-expansion latency or filtered ANN recall. The review's milliseconds and message-count thresholds are hypotheses, not hardware-independent guarantees. A rebuildable Neo4j projection can still offer transaction guarantees inside its own store; its backup edition split does not establish that it has no correctness guarantees.
- **Temporal keys are not automatic history.** PostgreSQL 18's range-overlap and temporal-reference constraints help enforce a selected state projection; they do not automatically retain system-time history as XTDB does. Apply non-overlap rules to resolved state for the appropriate plan/attribute, while retaining conflicting attributed assertions separately. [PostgreSQL 18 constraints](https://www.postgresql.org/docs/18/sql-createtable.html).
- **Graphiti's deletion finding is from code inspection.** The missing restoration logic makes deletion of a cancellation an important suspected failure case. Neither reviewer ran the end-to-end reproducer, so treat it as a repair risk to test on a pinned version, rather than a published test result.
- **Contextual embeddings add dependencies.** If a segment's embedding includes a reply parent or neighboring messages, editing or deleting those messages must invalidate that embedding too. Extra context increases embedding input and repair work even when its construction uses no LLM. Historical retrieval must also prevent context after the requested cutoff from entering an otherwise eligible segment.

My response to the review is to favor its implementation order: establish the evaluation and contextual episode retrieval first, then test graph expansion and the physical engine separately. The native-graph option remains a serious experiment, with explicit criteria for earning its additional service.
