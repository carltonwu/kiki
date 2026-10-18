# Temporal conversation memory for Kiki

2026-10-17 · Proposal; no runtime or schema changes implemented.

## Recommendation

Build a conversation-first memory system over the existing Postgres message store, with separate background topic consolidation and a retrieval step before every reply. Keep source messages authoritative and every derived artifact replaceable.

Extend continuous learning to **how each guild communicates**: local vocabulary, evolving meanings, register, multilingual usage, and code-switching. The [guild language learning proposal](guild-language-learning-hld.md) specifies research grounding, evidence records, backfill, incremental consolidation, and response-time adaptation. It shares this proposal's source revisions, work queue, temporal cutoffs, and repair semantics; language artifacts are another derived memory layer.

The central distinction is **a conversation is an episode; a topic connects episodes**. The same people can discuss different things in one sitting, different groups can discuss the same topic, and the same group can return to a topic months later. These should remain distinguishable.

Separate memory organization from response routing. Organize human chat whether or not Kiki participates. The decision to respond remains a live-only action with its own policy and queue.

## What the code does today

This analysis describes the workspace source, not a census of a running database. `HANDOFF.md` predates much of the implementation; source files are the reference.

- `services/ingester/migrations/0001_ingestion.sql`: `messages` stores one current row per Discord snowflake, guild/channel/author IDs, text, reply target, creation/edit/deletion times, full JSON, and ingestion time. `channels.parent_id` represents channel/thread containment. There is no message revision log. Reply targets need not exist locally.
- `services/ingester/src/store.rs`: REST and live creates share upserts. Full-object writes reject an older edit timestamp and preserve deletion state on existing rows. Partial updates merge fields, but do not apply the full-object timestamp guard. Deletes update existing rows; a delete received before its message has no durable tombstone. These last two cases need explicit handling for stronger out-of-order guarantees.
- `store_page` atomically stores a REST page and advances `channels.complete_through`. Live messages advance `last_message_id`, not that completeness cursor. This separation is useful and should survive the redesign. Completeness means API-visible messages, not recovery of messages Discord already deleted.
- `0003_routing.sql`, `router/mod.rs`, `router/classify.rs`: fresh human messages are pooled by channel and author, then classified for whether they address Kiki. The default age limit is five seconds. Only routed messages join `conversation_messages`; Kiki's own replies are tagged separately. Historical backfill and ordinary human discussions have no general conversation assignment.
- `conversation_messages.message_id` is a primary key: a message has at most one conversation. `via` describes response routing, not general discourse relationships. Replies, participants, and channel containment provide useful raw links, but there is no general learned conversation/topic graph.
- `conversation.rs`: the active pool defaults to eight conversations per guild. Least-recently-active eviction closes them; there is no sealing TTL. A reply to Kiki can reopen a closed conversation. `created_at` and `last_active_at` use processing time. `0004_agent.sql` adds last message ID and an insert-only count, but no first message ID or message-derived start/end times; the count includes subsequently soft-deleted messages.
- `agent/context.rs`: the default prompt contains the latest 30 conversation messages, eight surrounding channel messages from the last 30 minutes, and other active conversation labels in the channel. Messages are ordered by ID, but normal transcript entries omit timestamps, IDs, and reply edges. A current clock and approximate conversation age provide limited temporal context. Nearby channel text is included in the final user turn alongside trusted run context.
- `agent/topic.rs`: after a successful reply, the background refresh uses the latest 12 messages and previous label to produce a current topic of under 15 words, normally every four new messages. It deliberately replaces the old topic rather than preserving the conversation's history. There are no persistent topic entities or many-to-many assignments.
- `services/agent/src/kiki_agent_host/retrieval.py`: tools search individual non-bot messages across the job's guild, optionally narrowed to a channel using English full text and 1,024-dimensional pgvector embeddings, reciprocal-rank fusion, and a small 30-day recency decay. Explicit date ranges disable that decay. Search returns timestamps and links; neighborhood expansion includes a direct reply parent. Embedding work happens lazily, up to 256 pending messages per guild, with an optional bulk indexing command. No vector ANN index is defined. Search excludes every message assigned to the current conversation, including older messages absent from the 30-message prompt; that can make its earlier details unreachable through search.
- `0008_people.sql` records stable user IDs and current/historical names. Search resolves roster refs, IDs, mentions and exact normalized current/historical names to user IDs; transcript labels pair roster refs with display names. The later identity-learning phases in `docs/identity-hld.md` remain proposals; memory should share the implemented ID-based resolution.

The major limitation is not lack of raw timestamps or vectors. It is that response sessions currently double as memory episodes, and retrieval has no durable representation of what a historical exchange meant or how its outcome changed.

## Research inspiration and its limits

These are engineering inferences from research, not claims that this architecture reproduces a brain or that neuroscience establishes the proposed thresholds.

1. **Event segmentation:** Baldassano et al. found event structure at multiple timescales and associations between event boundaries and later reinstatement. Use coherent episodes with optional smaller segments; neither one giant transcript nor fixed clock windows capture all useful boundaries. [Primary paper, Neuron 2017](https://www.dpmlab.org/papers/Neuron17.pdf).
2. **Complementary learning systems:** the McClelland, McNaughton, and O'Reilly framework distinguishes rapid episode learning from gradual integration of shared structure. Use immediate conversation memory plus slower topic consolidation. Preserve examples behind abstractions. [Authors' publication index, 1995 paper](https://stanford.edu/~jlmcc/papers/).
3. **Temporal context and contiguity:** Sederberg, Howard, and Kahana model recall through changing context and associations between nearby experiences. A retrieved message should bring relevant conversation neighbors and reply ancestors, not merely isolated similar text. [Primary paper, Psychological Review 2008](https://memory.psych.upenn.edu/files/pubs/SedeEtal08.pdf).
4. **Prioritized replay:** Mattar and Daw model memory access ordered by expected usefulness. Prioritize unresolved plans, changes, and weakly understood or frequently needed episodes for consolidation; do not reprocess every message equally. This is a computational theory, not experimental validation of our queue policy. [Primary paper, Nature Neuroscience 2018](https://www.nature.com/articles/s41593-018-0232-z).

Do not imitate reconstructive errors or treat repeated retrieval as independent evidence that a statement is true. Decay can influence ranking and processing priority without deleting source evidence.

## Proposed representations

The logical model below can live in Postgres, a native graph engine, or a hybrid. The wider [storage evaluation](memory-storage-options.md) treats a native temporal evidence graph as a serious quality-first prototype and compares it against equivalent SQL edges. Physical engine selection should follow retrieval and repair evaluation. The following are logical records, not a final migration spec.

| Record | Purpose and important fields |
| --- | --- |
| `messages` | Current authoritative projection: IDs, event timestamps, raw content, source revision/hash, first/last observed time, deletion state. |
| `message_changes` | Durable accepted mutations: message ID, source, operation, before/after revision, observation time, dedup key. Keep revisions needed for audit or historical queries. A deletion tombstone can exist before a full message row. |
| `memory_work` | Transactional work queue: affected scope/range, requested generation, job kind, state, lease, retries. Enqueued with source changes; notifications only wake workers. |
| `conversations` | Stable logical episode ID, guild/channel, current version pointer. Memory episodes are channel-scoped initially. |
| `conversation_versions` | State, first/last message IDs, `starts_at`, `ends_at`, participant statistics, title, structured summary, revision, input hash, algorithm/model/prompt versions, coverage and confidence, sealing time. |
| `conversation_memberships` | Version, message ID, primary/context role, assignment method and confidence. Exactly one primary episode per eligible message within an active generation; contextual references may be many-to-many. |
| `conversation_edges` | `continues`, `references`, `corrects`, or replacement lineage; source message IDs and confidence. Overlapping simultaneous conversations are allowed. |
| `memory_chunks` | Bounded ordered spans within an episode, with time range, evidence IDs, content hash, full text and embeddings. A short episode needs just one chunk. |
| `topics`, `topic_aliases` | Stable ID, scope, name, description, aliases, status, first/last evidence times, revision. Names are labels, not keys. |
| `conversation_topics` | Many-to-many links to conversation versions or evidence spans, confidence and assignment provenance. |
| `memory_assertions` (later) | Attributed decisions, plans, preferences and claims; subject IDs, supporting message revisions, asserted time, optional event/validity interval, status and explicit correction/supersession edges. |

Use normalized evidence tables for references where integrity and invalidation matter; the table above abbreviates them as evidence IDs. Every summary statement and assertion should resolve to source messages. Summaries need whole-input hashes as well as sentence-level evidence: deleting a non-cited input can still change their interpretation.

Compute `starts_at = min(message.created_at)` and `ends_at = max(message.created_at)` over primary, non-deleted membership. First/last message IDs come from the corresponding `(created_at, id)` endpoints, not processing order. Recompute bounds, participant counts and visible message count on repair, including deletions and splits. Keep processing/sealing timestamps separate. Start with B-tree indexes on scope/time and membership, GIN full text, and indexes in both directions for topic and conversation edges; introduce vector ANN indexes when measured latency warrants them.

Separate an episode's short label from its summary and topics. Suggested summary fields are `what_happened`, `decisions`, `unresolved`, and `changes`, each with evidence. Always distinguish a suggestion from an agreement, a third-party claim from a self-report, and a bot answer from independent human evidence.

## Grouping conversations

Use temporal and social cues first, then semantic cues for ambiguous cases.

1. Order messages per channel by `(created_at, id)`. Burst grouping is only an optimization; retain individual messages, timestamps and reply targets. A same-author burst can contain a topic switch.
2. Generate a bounded set of candidate episodes: recent channel episodes and the episode containing an explicit reply target. A Discord thread is strong scope evidence, but a long-running thread can contain multiple episodes.
3. Score direct replies, mentions/addressees, participant overlap, time since the candidate's last message, continuity of intent, and semantic similarity. A reply to a human matters just as much as a reply to Kiki. Mentions can be references rather than addressees; retain uncertainty about who is talking to whom.
4. Prefer deterministic decisions when evidence is strong. Use a small constrained model over the short local transcript when candidates are close. Offer `new`, candidate episode IDs, and `uncertain`; store reasons and model version. A confident subject change can justify a new episode even with the same people and no long pause.
5. Allow several interleaved episodes in one channel. A/B planning dinner and C/D debugging Rust at the same time should not collapse into one conversation. Do not use unrestricted connected components over all reply/mention edges: a few bridging messages can join years of history.
6. Keep isolated messages as singleton episodes or explicitly pending assignments. A cross-topic bridge has one primary episode and contextual links elsewhere; avoid duplicate counting of its evidence.

Initial tuning candidates: consider recent episodes within 30 minutes, begin cooling after 20 minutes of inactivity, and split very long continuous sessions around a coherent boundary near four hours or a configured size bound. These are hypotheses to evaluate, not research-derived defaults. Candidate limits are local resource bounds; exceeding one must not close unrelated episodes globally. Hard size bounds also limit repair costs; preserve continuity with edges across splits.

An old reply during a new episode creates a `references` or `continues` edge to the historical episode. It does not make a months-old episode appear continuously active. Explicit addressing rules for Kiki can still start a fresh response session without forcing an artificial memory boundary.

## TTL, sealing, and correction

Use `open → cooling → sealed`. New activity can return cooling episodes to open. Sealing freezes a **version** against normal appends; it does not promise the underlying historical interpretation can never be corrected.

Separate the inactivity boundary from allowed lateness. A starting policy could use 20 minutes to begin cooling and 24 hours of event-time lateness before sealing. An episode is sealable only when the channel's verified coverage frontier passes `ends_at + allowed_lateness`, with no unresolved gap in the local boundary context. Backfill must establish the preceding boundary too; a partially loaded historical slice is provisional.

Do not derive coverage from the maximum message seen, a quiet socket, or the wall clock alone. On an idle channel, a successful check through the current API head can advance a verified time frontier even when no new message exists. Track checked time separately from `complete_through`'s last-message ID. Permission failures produce unknown/unavailable coverage, not an empty completed interval. Choose an explicit operational policy for long-unavailable channels rather than silently declaring them complete.

If a late message changes a sealed episode, create a replacement version. If it bridges two episodes, publish replacement membership and lineage atomically; do not merely attach it to whichever episode was processed most recently. A normal new reply to an old message is different from discovering an old message: the former belongs to the current episode, the latter repairs historical segmentation.

Edits and deletions invalidate chunks, summaries, topic summaries, and assertions derived from the affected input. Immediately make dirty derived text ineligible for retrieval, then rebuild. Retrieval can use valid raw messages while repair runs. An archived version may remain available for explicitly authorized audit, but normal retrieval must not resurface deleted text through an old summary.

If absolute permanence after TTL is required, corrections must be separate overlays that retrieval always resolves. That adds complexity and can still change the effective conversation. Version replacement is the simpler recommendation.

## Learning topics

Use both real-time assignment and background consolidation.

- On meaningful episode growth, produce a provisional label and candidate descriptors. Match to existing topic descriptions/aliases using lexical and semantic candidates, then validate the match. Do not require the ontology to be complete before an episode becomes searchable.
- On sealing or repair, extract fuller topic assignments from the complete episode, including earlier subject spans. One conversation may have multiple topics; two conversations sharing a topic remain distinct episodes.
- Queue uncertain assignments and novel descriptors. A periodic worker groups candidates into stable topics, proposes aliases, and merges/splits overly broad categories. A daily reconciliation pass can repair missed work; durable jobs handle ordinary incremental processing. Debounce live updates and coalesce backfill jobs to avoid one model call per imported message.
- Use stable topic IDs and explicit lineage/aliases on merges. Avoid creating a permanent topic for every sentence or merging topics solely because names resemble one another. A unique event can be searchable as an episode without becoming a reusable topic.
- Prefer evidence-linked topic timelines and topic summaries scoped to a time interval. A single overwriting topic paragraph loses change history. Incremental topic state must be recomputed in event order or from its evidence set, rather than arrival-order appends.
- Keep topic scope no broader than the retrieval authorization scope initially. A summary trained on private-channel text cannot be safely injected into public chat merely by hiding its citations.

An optional later layer extracts durable assertions. A topic such as “summer trip” is a retrieval handle, not proof that a date or venue is settled. Record competing statements and only mark an earlier decision superseded when the new evidence identifies the same plan and supports the change. Repeated quotes and Kiki's own summaries do not count as independent corroboration.

## Timeline-aware retrieval and prompt construction

Retrieval should run before generation, with tools available for follow-up. Automatic retrieval can be lightweight or return nothing; the reply model should not need to realize that a forgotten fact exists before it can retrieve it.

Build the query from the latest messages, explicit reply target, participants, current episode intent, and resolved time constraints. Use user IDs, not names, for people filters. Keep three clocks separate:

1. **Message/event time:** when the message was sent and where the conversation sits chronologically.
2. **Observation time:** when Kiki learned a message or revision. A backfilled June message learned in October still belongs in June, but was unknown to Kiki in July.
3. **Referenced event/validity time:** when a described dinner, deadline, or state applies. A message today about next Friday is not an event occurring today. Store normalized dates with source phrase, timezone/precision and confidence; leave unresolved dates unknown.

Distinguish “what was said before July?” from “what did Kiki know in July?”. The latter requires observation-time history; neither can be reconstructed from today's current row alone. Use a fixed retrieval snapshot, `now`, message cutoff and artifact generation for each response. In historical replay, filter source revisions and summary inputs to that cutoff: even a correctly dated episode can contain later statements.

Suggested retrieval sequence:

1. Enforce channel/guild authorization and temporal constraints in every candidate path, including topic summaries, chunks, reply edges, and caches.
2. Include active episode context and the direct reply target. Find candidates through message/chunk full text and vectors, episode summary search, topic links and participant/time indexes. Keep raw-message search as a parallel path so an imperfect summary or topic assignment cannot hide a detail. Exclude only message IDs already present in the prompt, not the entire current episode.
3. Fuse candidates and rerank for query relevance, explicit relationships, temporal fit, unresolved/current state, and source quality. Recency is query-dependent: use it for “what is the plan now?”, time proximity to the requested interval for historical questions, and no blanket decay for enduring preferences.
4. Expand selected hits into compact episodes: relevant chunk, reply ancestors/children, local neighbors, and linked correction/decision episodes. Bound expansion and recheck scope. A directly relevant older decision must bring its later explicit correction for a current-state question, even if that correction has low lexical similarity. At a historical cutoff, omit corrections that had not yet occurred or been observed, according to query mode.
5. Select a diverse evidence set under a token budget, suppress near-duplicates, and retain both sides of unresolved contradictions. Retrieve more evidence rather than compressing away a decisive qualification. Relevance decides inclusion; chronological order inside each topic/episode helps explain progression.
6. Render a memory packet with absolute timestamps, current reference time, time ranges, stable speaker refs, message IDs/links, attributed summaries, source revision/coverage, and uncertainty. Mark current discussion versus past evidence explicitly. “No match in indexed material” must not become “this never happened”.

Example, with invented evidence:

```text
Now: 2026-10-17T18:00:00Z
Question: Where did we end up booking dinner?

Topic: October group dinner
2026-10-10 18:00–18:12Z · Alex, Bea · episode E17
  Alex proposed Cedar; Bea agreed. [m101, m108]
2026-10-16 20:05–20:15Z · Alex, Bea · episode E42
  Alex said Cedar was full and booked Pine for Oct 18 at 19:00
  America/New_York; Bea confirmed. Explicitly replaces E17's plan. [m902, m910]

Current discussion, 2026-10-17 17:59Z:
  [u3] Where did we end up booking dinner?
```

For “what was the plan on October 12?”, retrieve E17 without treating E42 as knowledge available then. For an ambiguous “dinner” across several unrelated plans, retain the ambiguity instead of selecting whichever record is newest.

Budget by tokens, not message count alone. An illustrative 8,000-token memory allocation is 3,000 for the active transcript, 3,000 for historical evidence and context, 1,000 for pertinent state/changes, and 1,000 for metadata and headroom. This excludes static instructions and generation budget and should be tuned. Long episodes can use a grounded prior summary plus the latest raw turns. Put quoted Discord text and derived memories in a data/context message, separate from trusted instructions; escape transcript structure and retain attribution.

## Backfill and live processing

Both paths must converge on the same canonical source store, grouping code, and derived-artifact definitions. Arrival order is not chronology.

1. **Capture reliably.** Persist accepted source changes and memory work atomically. Deduplicate creates/edits by source identity and revision/hash; duplicate observations must not bump semantic revisions. Preserve pre-create tombstones, guard partial updates against stale edits when comparable version information exists, and reconcile incomparable partial snapshots explicitly. Do not pretend Discord supplies a globally ordered mutation log across REST and Gateway. Retain provenance and use authoritative refetch where ordering cannot be determined.
2. **Process in bounded scopes.** Serialize segmentation writes per channel/range and generation, with leases/retries. Model calls operate on snapshots outside long-held database locks; publishing uses compare-and-swap on input revisions. At this scale, a Postgres queue and workers suffice.
3. **Do not use maximum message ID as the memory completion cursor.** An older insertion, edit or deletion can change an already-processed range without advancing that ID. Track dirty ranges and input generations. Existing REST completeness cursors keep their ingestion role.
4. **Replay chronologically.** Backfill batches accumulate coverage before expensive consolidation. Partition work for throughput but carry boundary context across page/day boundaries; do not split conversations at REST page boundaries or midnight. A late bridging message can affect both neighboring episodes. Expand repair until boundary decisions stabilize, with a configured work limit and explicit deferred repair if necessary.
5. **Publish complete replacements.** Build changed memberships, metadata, chunks, evidence and indexes in a staging generation. Verify source inputs have not changed, then atomically activate that channel/range generation and enqueue dependent topic work. Validity checks prevent old cached topic summaries from surviving dependency changes. Queries use a consistent generation and valid artifacts; they never combine new membership with an old summary.
6. **Preserve identity where possible.** Retain logical episode IDs for unchanged episodes; record replacement/merge/split lineage for changed ones. Do not require stable IDs for inherently different partitions. Capture model decisions and algorithm versions; a fixed model name and low temperature alone do not guarantee replay determinism. Cache outputs by canonical input hash. Compare partitions/evidence in replay tests rather than auto-increment IDs.
7. **Isolate response side effects.** Historical work cannot enqueue reply jobs. A live reply may proceed with its recent raw transcript while grouping or consolidation catches up; it should not wait for a bulk archive job. Give live repair and retrieval higher priority, and expose partial coverage. Keep bot-authored messages available as dialogue/context while treating them separately from human assertions.
8. **Reconcile gaps.** Periodically compare source revisions against derived inputs and reclaim abandoned leases. Expose coverage by channel/time interval for ingestion, grouping, summary and embedding stages; one overall “complete” flag is insufficient. Discord REST cannot discover old edits or recover deletions that happened while the bot was offline without additional observations or rescanning; report this source limitation.

Retrieval caches should include query scope, cutoff/mode, ontology revision and affected source/artifact generations, or be explicitly invalidated on those changes. Embeddings should be generated in the background for live messages/chunks with a separately throttled backfill queue; query-time indexing should be a fallback. Track embedding model/version and index coverage. Profile before choosing ANN indexes; vector indexing is a performance choice, not a prerequisite for correct grouping.

## Alternatives and rollout

- **Temporal sessions only:** cheapest baseline; useful for initial grouping and benchmarking. It merges simultaneous conversations and misses semantic continuations.
- **Conversation episodes plus learned topics:** recommended balance. Preserves temporal/social context and supports cross-episode recall. Main costs are segmentation repair and consolidation quality.
- **Assertion/entity graph first:** attractive for rich cross-episode questions, but extraction mistakes become durable false state and every source correction fans out. Add selected assertions after episode retrieval works.

Implement in three increments:

1. Decouple response sessions; add source revisions/tombstones, transactional memory work, message-derived episode bounds, grouping, version repair and timestamped context. Retrieve raw messages plus episode context automatically. Keep the current lexical/vector path as a baseline.
2. Add durable episode summaries/chunks, background embeddings, provisional topic matching and consolidation, evidence-linked topic timelines, and correction expansion.
3. Add a small set of explicit assertion kinds, event-date extraction and utility-based consolidation priorities after evaluation shows where they help.

Language learning runs alongside these increments: start with evidence-linked local terms and comprehension, then consolidate time-scoped language profiles, then enable conservative response adaptation after guild-specific evaluation. Both historical and live messages feed the same learner, including human exchanges that never address Kiki. See the [language rollout and acceptance criteria](guild-language-learning-hld.md#rollout-and-evaluation).

Before choosing production thresholds, hand-label representative examples from the actual guild: rapid banter, interleaved exchanges, topic drift, old replies, jokes, renames, plans revised later, and attachment-dependent messages. No dataset-derived threshold claims are made in this proposal.

Evaluate retrieval and answers separately. Measure evidence recall at a fixed token budget, source-link precision, temporal ordering/current-state accuracy, correct abstention under incomplete coverage, segmentation quality, latency and background cost. The [LongMemEval paper](https://arxiv.org/abs/2410.10813) provides useful categories—especially temporal reasoning, knowledge updates, multi-session reasoning and abstention—but needs Discord-specific cases here.

Required invariants include: shuffled/duplicated source delivery produces equivalent partitions for fixed canonical inputs and recorded grouping decisions; batch and live converge after repair; late inserts repair both sides of a boundary; deletion-before-create stays deleted; stale model jobs cannot publish; old edits/deletions invalidate summaries even when maximum message ID is unchanged; retrieved packets never cross authorization scope or historical cutoffs; and backfill produces zero outbound replies. Run end-to-end comparisons against the existing message-only retriever before adopting more elaborate memory layers.
