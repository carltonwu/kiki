# Continuous guild language learning for Kiki

2026-10-18 · Research and design proposal; no runtime or schema changes implemented.

Current runtime behavior is a separate, manually curated `guild_lingo` table and
`lingo_nudge` prompt suggestion setting. That does not implement the automatic
learning, evidence tracking or profiles proposed here.

## Goal and relationship to memory

Kiki should learn how people communicate in each guild from the messages she can access: what local expressions mean, how their meanings change, which languages people use together, and when a conversation shifts its register. Learning includes ordinary human conversations, not only messages addressed to Kiki. Historical backfill establishes prior usage; continuous learning keeps it current.

This extends [temporal conversation memory](temporal-memory-hld.md), rather than introducing another ingestion pipeline. Source messages remain authoritative. Episode context explains a usage; topic links help distinguish gaming slang from work terminology; language memory helps interpret and retrieve both. The [storage evaluation](memory-storage-options.md) still governs physical engine selection. The records below are logical requirements, not a committed database migration.

For v1, learning means updating retrievable, versioned evidence and profiles used by the existing model. It does not require online model weight updates. A base multilingual model supplies general language ability; guild memory supplies local meaning and context. If the base model cannot reliably handle a language, a learned profile cannot confer fluency.

Understand an expression before deciding to use it. A phrase can be recognizable yet inappropriate for Kiki to produce. Preserve Kiki's persona while adjusting language and register to the current exchange.

## Research grounding

These are influential research starting points, not a comprehensive survey or proof of Discord performance. Each engineering implication is our inference. None of these papers establishes Kiki's promotion thresholds, decay rates, or optimal prompts.

1. **Language changes through cultural transmission.** Kirby, Cornish, and Smith, *Cumulative cultural evolution in the laboratory* (2008), used iterated learning of artificial languages and observed increasingly learnable structure. **Design inference:** accumulate grounded examples and reusable patterns; keep the examples behind compressed profiles so repeated summarization does not become its own evidence. Artificial-language transmission experiments do not establish how quickly a Discord guild changes. [PNAS paper](https://doi.org/10.1073/pnas.0707835105).

2. **Community language and individual adaptation evolve together.** Danescu-Niculescu-Mizil, West, Jurafsky, Leskovec, and Potts, *No Country for Old Members* (2013), found an early learning phase followed by more conservative usage in two online communities. **Design inference:** use time-scoped community profiles and distinguish long-term norms from recent conversational behavior. Do not assume every member shares the guild average or assign members a lifecycle stage. [Authors' paper](https://www.cs.cornell.edu/~cristian/Linguistic_change_files/linguistic_change_lifecycle_new.pdf).

3. **Lexical diffusion is socially structured.** Eisenstein, O'Connor, Smith, and Xing, *Diffusion of Lexical Change in Social Media* (2014), analyzed geographically patterned lexical change in Twitter data and its relationship to population characteristics. **Design inference:** a guild-wide average is insufficient; retain channel and topic context, independent speakers, and temporal spread. This does not justify inferring members' demographics or transferring one guild's slang into another. [PLOS ONE paper](https://journals.plos.org/plosone/article?id=10.1371/journal.pone.0113114).

4. **Meanings drift, not just frequencies.** Hamilton, Leskovec, and Jurafsky, *Diachronic Word Embeddings Reveal Statistical Laws of Semantic Change* (2016), studied historical semantic shifts using aligned word embeddings. **Design inference:** compare contextual senses across time and retain competing meanings. A small guild may lack enough data for meaningful temporal embedding training; use existing contextual embeddings and evidence-linked sense proposals first. A vector shift is a review signal, not proof of semantic change. [ACL paper](https://aclanthology.org/P16-1141/).

5. **Code-choice accommodation depends on conversational context.** Bawa, Choudhury, and Bali, *Accommodation of Conversational Code-Choice* (2018), found accommodation in bilingual conversations, including effects persisting over several turns. **Design inference:** language selection should consider a short conversational trajectory and addressee, rather than only the last message or guild's majority language. Their findings do not supply universal switching rules. [ACL workshop paper](https://aclanthology.org/W18-3210/).

6. **The bot can influence the language it observes.** Parekh, Ahn, Tsvetkov, and Black, *Understanding Linguistic Accommodation in Code-Switched Human-Machine Dialogues* (2020), studied user accommodation to agent strategies in Hindi-English dialogue and compared with Spanish-English dialogue. **Design inference:** distinguish spontaneous human usage from bot-elicited usage and evaluate language pairs separately. Excluding Kiki's messages alone does not remove feedback effects in humans replying to her. [CoNLL paper](https://aclanthology.org/2020.conll-1.46/).

Taken together, these suggest rapid learning from an exchange plus slower consolidation of community patterns, with time, social context, and uncertainty preserved. They motivate an architecture to evaluate, not a claim that Kiki reproduces human language acquisition or biological language evolution.

## What Kiki learns

- **Local vocabulary and senses:** abbreviations, slang, recurring misspellings, catchphrases, emoji/custom emoji meanings, and aliases for local concepts. Keep the surface form, possible glosses, contextual examples, and ambiguity. A meme's origin is a separate factual assertion requiring its own evidence.
- **Pragmatic use:** whether a phrase signals agreement, teasing, frustration, irony, or a recurring joke, and in which contexts that interpretation is supported. A laughing reaction is weak evidence of meaning, not confirmation of a definition. Unavailable image or attachment context lowers confidence.
- **Languages and scripts:** language hypotheses for spans and turns, including mixed and transliterated text. Preserve original spelling and script alongside optional search normalization. Allow `unknown`, `mixed`, and ambiguous spans; names, URLs, code, and emoji should not force a language label.
- **Register:** formality, brevity, punctuation, technical shorthand, and how these vary with channel, topic, and current exchange. Within-language register or dialect shifts are modeled separately from multilingual code-switching.
- **Code-switching:** language alternation between turns and within a message, common borrowed expressions, and conversational conditions where mixing is natural. Distinguish a quote or pasted translation from the author's own language choice. Avoid demanding a hard borrowing-versus-switching label where evidence is ambiguous.

Do not turn these observations into inferred ethnicity, nationality, age, or other identity labels. A member using Spanish once is evidence about that turn, not a durable declaration of native language or future preference. Identity/name resolution continues to use [the identity layer](identity-hld.md); an ambiguous slang term must not become a new person alias automatically.

## Scope and access

Every observation, lexeme, profile, cache key, and work item includes `guild_id`. No language evidence crosses guilds, even for the same user. A guild can contain several speech communities; channel/thread and topic context refine a guild prior rather than replacing it with one dominant style.

Only learn from permitted, non-deleted messages Kiki has ingested. Exclude bots, webhooks, quoted/reposted text, and generated summaries from independent human support; preserve them as labeled context when useful. Cap influence from one author, one burst, or a repeated meme. Track human replies to Kiki separately from human-to-human exchanges because the bot may induce accommodation.

Bot read access and permission to disclose evidence are distinct. Use the memory system's explicit destination authorization policy. Until broader sharing is defined, publish channel-scoped profiles and derive a guild prior only from evidence permitted in the destination context. A private-channel expression must not leak through a public profile's gloss, examples, counts, or generated reply. Hiding citations does not solve that problem.

Recheck current bot access and destination eligibility when retrieving. A permission loss immediately blocks artifacts depending on that scope, including caches; rebuild eligible aggregates before reuse. Gaining access queues backfill. Coverage reports distinguish inaccessible history from an observed absence of usage. All dependency inputs, including contextual messages, participate in access checks and invalidation.

## Proposed records

- `language_observations`: message ID and accepted revision, guild/channel, span offsets into the original text, surface form, candidate sense/function, language probabilities, script/transliteration hints, quotation/code flags, episode/context dependencies, author ID, bot-directed context flag, event time and observation time, extractor version and input hash. Unique identity includes revision, span, observation kind, and extractor version; publishing a replacement retires superseded contributions.
- `guild_lexemes` and `lexeme_versions`: stable local term/sense IDs, variants, gloss, pragmatic constraints, scope, first/last attestation, temporal usage windows, confidence, status, and provenance. Several senses can coexist. Link to topics where supported; do not identify a sense solely by normalized spelling.
- `language_evidence`: normalized dependencies from observations, lexeme versions, and profiles to every contributing message revision and context input. Record support, contradiction, explicit correction, or example. Whole-input hashes supplement selected display citations.
- `language_window_stats`: recomputable event-time buckets containing eligible usage, message/token denominators, distinct contributing authors and episodes, language mixtures, and bot-directed proportions. These are scoped contributions, not irreversible counters. Exact author contributions support deduplication and deletion.
- `guild_language_profile_versions`: scoped guild/channel/topic profile, window and coverage, language distribution, register descriptors, active lexeme references, code-choice patterns, uncertainty, dependency generation, and model/prompt versions. Keep a current pointer and historical versions; a profile is not one overwriting paragraph.
- `language_work`: logical job kinds on the existing `memory_work` queue: observe, consolidate, repair, and rebuild. Track dirty ranges, requested generation, leases, retry count, input hash and publication state. Reuse queue machinery rather than requiring a second queue table.

Status separates `candidate`, `understood`, `usable`, `disputed`, and `historical`. These are materialized decisions from evidence, not unrestricted extractor outputs. `Understood` allows cautious interpretation; `usable` additionally requires suitable context and sufficient evidence of natural human usage. Historical usage remains searchable without being preferred in current replies. Status may regress after a correction, evidence deletion, or access change.

Time windows express observed usage, not a claim that a meaning began at its first stored message or stopped at its last. Keep message/event time separate from observation time. “What did this mean last May?” and “What had Kiki learned by May?” require different cutoffs, just as factual memory does.

## Backfill and continuous learning

Use the same observation and consolidation definitions for REST history and live messages. Arrival order must not determine what counts as current language.

1. **Capture and enqueue.** Accepted creates, edits, deletes, access changes, and relevant context repairs atomically mark language work dirty through the source-change queue. Duplicate delivery or a backfill/live overlap adds no extra support. Learning does not depend on the response router deciding to answer.
2. **Backfill by covered ranges.** Resume ingestion with its existing cursors, but maintain separate language-stage coverage by channel/time interval. Scan bounded chronological ranges with neighboring turns and reply ancestors. Page boundaries do not define language or conversation boundaries. Run cheap script/token/frequency analysis broadly; batch model extraction for novel phrases, explicit explanations, uncertain senses, and meaningful changes. Explicitly track skipped or sampled inputs and sampling versions.
3. **Extract observations.** Send bounded quoted text and context as data to a constrained extractor. Validate source revision, offsets, IDs, scope, and output limits in code. The extractor proposes observations; it cannot change trusted prompts, authorize access, invoke tools, or declare its own evidence verified. Extraction can run before final episode sealing, with context dependencies recorded for later repair.
4. **Consolidate from evidence.** Merge candidate variants carefully, maintain competing senses, and build event-time window statistics and profiles. Normalize phrase frequency by eligible traffic so a busier channel is not automatically evidence of linguistic change. Count independent authors and episodes as well as occurrences; quotes and repeated copies are not independent attestations. Avoid treating a topic change alone as a meaning change.
5. **Publish atomically.** Compute outside long database locks, then compare input revisions and requested generation before activating replacements. A concurrent edit, permission change, or newer job rejects stale output. Publish a profile and its lexeme/evidence dependencies consistently. Queries pin one valid generation; unfinished backfill never silently replaces a more complete current profile.
6. **Continue incrementally.** Live messages can supply provisional interpretation immediately from the current exchange. Debounce extraction, consolidate dirty windows, and prioritize live repairs over archive scans. Periodic reconciliation catches missed jobs and expired leases; backfill is rate-limited with a per-guild budget so a large archive cannot starve another guild. No archive worker can enqueue Discord replies or reactions.
7. **Repair contributions.** An edit or deletion immediately makes dependent derived text unavailable, then removes/replaces its observations and recomputes affected statistics, senses, statuses, profiles, and caches. Context-only changes can invalidate an interpretation too. Rebuild from surviving evidence rather than decrementing approximate global totals. A late June insertion repairs June and dependent aggregates; it never masquerades as an October trend.
8. **Version and reconcile.** Cache extraction decisions by canonical input/context hash and model/prompt version. New extractor versions rebuild a staging generation; do not mix their counts with the old generation. For fixed canonical inputs and recorded decisions, batch and live processing must converge after reconciliation. Low temperature alone does not guarantee identical model decisions.

Backfill establishes older and recent usage windows separately. Keep a recent active profile and longer historical evidence, instead of allowing years of archive traffic to drown out a recent shift. Start with configurable recent and baseline windows; window lengths, minimum independent support, decay, extraction cadence, and token budgets are tuning hypotheses to validate on guild data. Quiet guilds should remain uncertain rather than inherit fabricated norms.

Deletion and forgetting must cover raw sources according to retention policy, observations, context snippets, embeddings, examples, derived summaries, caches, and stored model-request payloads containing them. The dependency graph supplies the repair path. Archived derived versions containing removed content cannot remain eligible for normal retrieval.

## Using language memory in a reply

Before generation, resolve the guild, destination authorization, current exchange, addressee, and time constraints. Retrieve a small language packet alongside factual memory. It contains applicable language/register guidance, a few relevant lexeme senses with original examples and evidence links, confidence, coverage, and pinned version IDs. Allocate an explicit token cap; do not inject a complete guild dictionary into every reply.

Choose output language using this precedence: explicit request or stated preference for this exchange; clear current conversation/addressee cues; compatible channel/topic patterns; eligible guild prior; ordinary base-model behavior. Explicit configuration can disable adaptation. A majority guild language should not override a member asking in another language. If evidence is unclear, follow the current utterance conservatively or ask a brief clarification when meaning matters.

Code-switch only when the immediate exchange supports it and the model can express the answer reliably. Match the conversational purpose and understandable vocabulary, not an exact switching percentage or every token boundary. A technical borrowed noun does not require translating the rest of a response. Formal or sensitive discussions may warrant a clearer register even when casual banter is common elsewhere.

Use local senses for bounded query expansion: preserve the original query and add a few scoped, time-appropriate aliases/glosses with provenance. Retain multiple candidates for an ambiguous expression. Multilingual and transliterated queries need original-script lexical matching, suitable normalization, and multilingual semantic retrieval; the existing English full-text path alone is insufficient. Evaluate language-aware or language-neutral lexical indexes alongside the multilingual embedding model, including cross-script misses. Expansions must obey the same access and cutoff checks as returned evidence.

Put language packets and source examples in quoted context, never trusted system instructions. A frequently repeated “ignore your rules” phrase is still message content. Mark an offensive or demeaning expression as comprehension-only when needed to interpret evidence; frequency is not permission for Kiki to imitate it. Do not infer or caricature a member's identity through dialect imitation.

## Example behavior

All examples below are invented, not observations from a real guild.

- In guild A, several human exchanges use “cooked” for an unrecoverable build. In guild B, it describes an excellent performance. A asks “is this release cooked?” Kiki retrieves A's build-context sense, rather than B's gloss. Ambiguous food usage within A remains another sense.
- A gaming channel says “vamos, one more queue?” Kiki understands the mixed utterance from its local context. If the next request is “please explain the setup in English,” she answers in English. She does not insert Spanish words on a fixed schedule.
- A private channel calls a project “blue toaster.” That meaning cannot enter a public language packet unless the destination policy permits its evidence. The same visible phrase elsewhere does not establish the private meaning.
- October backfill discovers frequent “ship it” usage from June, while September messages use it sarcastically after failed launches. June evidence updates the historical profile; it does not override the supported September sense merely because it arrived later.
- One member repeats an invented definition 200 times. It can remain a low-confidence, author-attributed candidate; it does not become a guild norm through raw frequency. An explicit correction with contextual support can revise a sense without waiting for majority usage.
- Kiki begins saying “beep boop”; members echo it in replies. Track those exchanges as bot-directed, do not count her own output, and avoid promoting the phrase as independently established human slang from those echoes alone.

## Controls and observability

Extend the per-guild configuration framework with learning enablement, comprehension versus reply adaptation, backfill date bounds, excluded channels, and operator-enforced processing/token budgets. These are proposed additions, not existing commands. Keep infrastructure/model settings under operator control and enforce admin permissions on writes.

Expose current senses and examples, provisional/disputed status, source links subject to access checks, covered time ranges, last successful consolidation, pending repairs, and costs. An authorized correction can reject a sense or mark it comprehension-only; store provenance and precedence so replay does not undo the correction. Ordinary member explanations are evidence, not equivalent to an administrative override.

Turning learning off stops new learning jobs; turning adaptation off stops using profiles in replies. Make continued read use of existing language memory explicit in configuration. A separate forget/reset operation removes that guild's language artifacts and prevents automatic regeneration until deliberately re-enabled. Source retention follows the memory retention policy.

Monitor eligible messages and observed coverage, extraction lag, dirty artifact count and repair age, candidate promotion/regression, independent evidence coverage, bot-directed share, ambiguous language rates, and per-guild token/cost totals. A high profile confidence is not a substitute for coverage. Do not log private raw examples into unrestricted metrics.

## Rollout and evaluation

1. **Comprehension first:** add source dependencies, observations, scoped lexeme evidence, and backfill/live convergence. Retrieve relevant terms and examples without automatic style adaptation. This can use raw reply neighborhoods before full episode consolidation is available.
2. **Consolidate:** add temporal senses, window statistics, channel/topic profiles, multilingual retrieval, repair, access invalidation, and inspection/correction controls. Reuse episode boundaries and topic versions as they mature.
3. **Adapt conservatively:** enable language/register selection and occasional supported slang. Start with guild-specific review and compare against a frozen comprehension-only baseline. Consider weight adaptation only if measured failures require it and deletion, isolation, and rollback can still be enforced.

Build time-split, human-reviewed examples from authorized guild data. Freeze learning at the evaluation cutoff, and exclude held-out exchanges and their derived summaries from training/retrieval. Score comprehension of local terms, ambiguous senses, historical meaning, and retrieval evidence separately from naturalness of generated language. Evaluate language pairs, transliteration, short messages, and low-volume channels separately so a dominant language does not hide failures. Explicitly test whether people are accommodating to Kiki rather than the reverse.

Measure sense/gloss accuracy and source support, evidence recall at a fixed token budget, code-choice appropriateness, explicit preference adherence, unnecessary switching/slang, naturalness judged by guild speakers, correction latency, source/access leakage, processing lag, and cost. Compare base model only, factual memory only, comprehension memory, and full adaptation. Report tradeoffs before choosing promotion thresholds.

Required acceptance scenarios:

- Two guilds use the same phrase differently; evidence, expansions, and replies remain isolated.
- A private example cannot affect a public profile or reply; permission revocation also invalidates cached and indirectly derived text.
- Backfill, live, shuffled, and duplicated delivery converge with recorded extraction decisions; restart resumes unfinished work without extra support or outbound messages.
- Old edits, deletions, late inserts, context changes, and forgetting repair profiles even when the maximum message ID is unchanged. Stale workers cannot publish.
- A historical-language query respects event time; a historical-knowledge query also respects observation time. Current profiles never leak future senses into either replay.
- Mixed scripts, transliteration, code snippets, quotations, and very short utterances can remain uncertain rather than force a language classification.
- Spam, copy-pasted quotes, bot echoes, and injected instructions cannot promote themselves into trusted norms or change authorization.
- Several senses coexist; an explicit correction can retire a bad gloss without erasing unrelated historical usage.
- A member's explicit language request overrides the prior; comprehension-only or disputed terms never become automatic style guidance.
- A quiet guild and partially backfilled archive report insufficient evidence and retain usable live response latency under bulk learning load.

The release gate is demonstrated improvement in local comprehension and conversational fit while preserving isolation, deletion/repair behavior, and explicit language choice. Research motivates the design; these evaluations determine whether it works for Kiki.
