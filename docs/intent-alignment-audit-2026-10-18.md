# Kiki intent alignment audit, October 18, 2026

## Scope and evidence

Reviewed the latest 25 nondeleted ingested Discord messages at the initial read,
from **12:08:23 to 12:25:46 US Eastern**, in `#clean-slate`: 13 human messages and
12 Kiki messages. The fixed endpoint is message `1561415003922370704`; newer
messages arrived during analysis and are outside this sample. This is 25
individual messages, not 25 user/assistant pairs.

Evidence: read-only production queries of `messages`, `conversation_messages`,
`agent_runs`, saved `host_request` prompts, `agent_host_jobs` results, and bounded
Railway agent logs. Read the preceding user message to interpret the first Kiki
reply. Model: `deepseek/deepseek-v4.1-flash-1017`.

Grades are subjective assessments of intent satisfaction, not benchmark results.
10 means the response satisfies the apparent request; 5 means useful but misses a
material part; 0 means wholly misses or contradicts it. Multiple delivered
messages from one run count as one response episode. Appropriate silence counts
as successful behavior. Social intent is less certain than explicit instructions.

**Overall: 6.7/10**, the equal-weight mean of the 11 scored episodes below.
This includes easy social exchanges and silence; it should not obscure the
**3/10 initial song** or **2/10 explicit wording correction**.

## All 25 messages, in order

1. Kiki jokes about making Nougie fanart. **8/10** for the response to the preceding
   praise/tool discussion: appropriate banter. It makes itself the topic, which
   becomes excessive later. Run 99.
2. Ethan: “NOUGIE FANART.” Enthusiastic reaction, not an actual image request.
3. Ethan: “????” Ambiguous reaction while the prior response is in flight.
4. Kiki imagines Nougie in a beret and at the beach. **9/10** for continuing the
   joke without claiming to have generated an image. Run 100. The separate run
   for “????” returns `NO_REPLY`: **9/10**, avoiding a redundant message. Run 101.
5. Ethan: “ok this is scary.” Probably playful surprise in context.
6. Kiki: “scary how?” followed by dog/wardrobe banter. **7/10**: plausible tone,
   though another question prolongs a joke that may already be complete. Run 102.
7. Ethan explicitly says not to respond. Run 103 returns `NO_REPLY` and sends
   nothing: **10/10**. The router still invokes the model; the output decision
   is correct.
8. Poot: “LOL.” Social reaction, no clear request.
9. Poot: “It’s sentient.” Social reaction, no clear request.
10. Poot praises the “Gen Z voice pack.” No Kiki reply is visible between these
    reactions and the song request; no additional grade assigned.
11. Carlton requests a song about the Discord server, hyperpop themed, for Ethan
    to sing in a relatively high voice. The brief has subject, genre, performer,
    and vocal delivery constraints.
12. Kiki announces it is writing a pitched-up, glitchy song.
13. Kiki delivers “YAPPLEBEE’S (clean slate mix).”
14. Kiki adds singing encouragement and claims it responded before reading the
    earlier request for silence. **3/10 across messages 12–14.** It provides a
    lyric structure and hyperpop cues, but its subject is largely its own recent
    conversations: identity confusion, fake context, not responding, and Nougie
    fanart. It does little to establish a singable high-register melody or
    consistent phrasing. The claim about having responded conflicts with run
    103's saved `NO_REPLY` and empty reply list. Run 104.
15. Carlton explicitly rejects the conversational bias: the subject was the
    server, not the current conversation.
16. Kiki acknowledges the mistake and rewrites with more server references.
    **6/10**: meaningful improvement and good acceptance of feedback, but still
    largely a collage of labels, names, and jokes. Lyrics such as “a government
    that never once convened” are unsupported by evidence visible in this audit;
    they should not be presented as verified history here. Hyperpop annotations
    remain more developed than singable phrasing. Run 105.
17. Poot praises Kiki's enthusiasm for Nougie and asks it to continue.
18. Kiki says Nougie is the only one with a clean record.
19. Kiki adds that it cannot @ him. **9/10 across messages 18–19**: directly
    accepts the invitation and makes an understandable dog joke. Run 106.
20. Poot asks what “clean record” means.
21. Kiki explains the posting joke, then adds dog-history details. **6/10**:
    the first line answers the question; the added history is unnecessary and
    weakens the punchline. Specific recalled incidents have no source links,
    despite the current prompt requiring them. Their truth is unverified here.
    Run 107.
22. Poot: “He’s a dog, dude.” Could be banter, but also a cue to stop explaining
    or treating the dog like a poster.
23. Kiki: “yeah and that’s the whole point,” followed by more of the same joke.
    **5/10**: coherent, but sounds defensive and fails to adjust to the possible
    correction. A short concession would fit either reading better. Run 108.
24. Poot: “Always say lowkirk not lowk, okay?” A clear wording preference.
25. Kiki says it will try, invents “lowkirkenuinely,” and ends with “no promises,
    the lowk is muscle memory.” **2/10**: persona overrides a harmless explicit
    instruction and the unwanted term immediately returns. A sufficient answer
    would be “lowkirk. got it.” Run 109.

## What the execution evidence establishes

- Initial song, run 104: **1 model request, 0 tool calls**. This is a failure to
  seek server-wide evidence, not a search ranking failure on that run.
- Rewrite, run 105: **3 model requests, 4 history searches**, each returning 10
  hits. Three searches report full semantic coverage; one reports semantic off.
  The rewrite did search. Search alone did not ensure representative selection
  or good synthesis. Logs omit query arguments and returned message bodies, so
  this audit cannot establish their channel/time distribution or corroborate
  every lyric.
- “Wdym clean record,” run 107: **2 searches**, with 10 hits each, for a question
  that could be answered by explaining Kiki's own wording.
- “Always say lowkirk,” run 109: **2 searches**, with 10 hits each. An instruction
  was treated partly as a historical lookup instead of simply being followed.
- Silence request, run 103: saved result is `NO_REPLY`, reply IDs are empty.
  The later account of having responded is fabricated conversational history.

## Likely causes in the implementation

1. **Retrieval policy covers people more explicitly than broad creative tasks.**
   `services/ingester/src/agent/context.rs:297–298` explicitly tells Kiki to search
   for person lookups. It permits server-wide search but does not require an
   evidence pass for a song about the server. “About this Discord” was resolved
   using whichever anecdotes were already available.
2. **The prompt emphasizes personality over task success.** Short messages,
   teasing, opinions, slang, and literal language are prominent. There is no
   clear rule that a user's requested genre, format, scope, and corrections
   govern the answer. The instruction to say things literally instead of using
   metaphors can also conflict with lyric writing.
3. **Stale topic and recent context compete with the new task.** Run 104 still
   labels the topic as Nougie fanart. Other active conversation summaries and
   channel background are placed in the final user turn. Later dog exchanges
   receive both complete song messages as background. A message-count limit
   does not prevent long old outputs from dominating the supplied text.
4. **There is a secondary search recency preference.**
   `services/agent/src/kiki_agent_host/retrieval.py:34–37,256–257,313–314` adds a
   0.15 top-vote recency bonus with a 30-day half-life unless a date filter is
   supplied. Lexical score ties also prefer newer message IDs. That can affect
   historical coverage, but it cannot explain a run that never searched.
5. **Preference persistence is absent from this path.** The phrase list is
   manually curated and still includes `lowk`; the run adds no conflicting
   lowk nudge, but the base glossary remains. Acknowledging a correction does not
   update a durable preference. This sample proves immediate noncompliance,
   not whether a hypothetical later preference would be remembered.
6. **Saved results omit the tool transcript.** The executor saves final output
   and aggregate usage. Logs identify tools, hit counts, and semantic status,
   but cannot reconstruct what evidence produced a particular claim.

## Recommended changes, in order

### 1. Make intent and corrections outrank persona

Add an explicit instruction near the start of the prompt:

> Satisfy the latest request before adding personality. Identify the requested
> action, subject, scope, format, and constraints. Recent chat helps interpret
> the request; it does not automatically define its subject. User corrections
> govern your next answer. Follow harmless wording preferences directly. Do not
> resist them for a joke or claim habits prevent compliance. Apply your usual
> chat style to commentary; let the requested genre govern creative work.

Also instruct Kiki not to revisit messages explicitly addressed away from it,
and not to invent explanations for what it previously did.

### 2. Choose context by the task's scope

Use a small structured decision, with no user-visible planning, such as:

`action=write_song; subject=server; scope=server_history; genre=hyperpop;
performer=Ethan; delivery=high_register; evidence_needed=true`.

The immediate win can be a prompt rule. If reliability remains poor, use a
validated task brief to choose retrieval and context assembly. Keep quoted
history separate from trusted application metadata and the active request.
Treat generated topic summaries as fallible background.

For broad server requests, gather evidence before drafting. For local banter or
simple preferences, avoid searches unless resolving a real ambiguity requires
them. The observed allocation is backwards: zero searches for the broad song,
two for a simple word substitution.

### 3. Retrieve representative history, then select motifs

Add a broad-history mode that removes the recency bonus and promotes diversity
across time periods, channels, and people. Disabling recency alone is
insufficient: highly repeated subjects can still monopolize the results.

As an initial heuristic, select 3–5 supported motifs from several channels and
time periods where available. Avoid letting a single current conversation
dominate, unless the user asks for it. Validate these heuristics against human
judgments; do not hardcode them as universal measures of quality.

Turn source anecdotes into a coherent premise and chorus rather than listing
proper nouns. Keep evidence IDs attached internally; for songs, offer a compact
source note outside the lyrics instead of inserting links into sung lines.
Facts introduced during casual banter still need grounding or should be omitted.

### 4. Make context smaller and corrections durable

Put the active request and relevant constraints in a clearly delimited block.
Supply background only when relevant and cap it by tokens as well as message
count. Summarize or omit previous long artifacts in unrelated exchanges.
Surface explicit preferences separately from the rolling conversation window.

Persist preferences with author ID, guild, scope, source message, and timestamp.
“Always say lowkirk” should take effect immediately in that conversation. A
single member's preference should not silently rewrite the entire server's
glossary; server-wide changes need the intended scope and applicable authority.

### 5. Evaluate intent satisfaction before changing models

Create human-reviewed replay cases for:

- Server-wide song surrounded by distracting recent banter: uses broader,
  supported motifs and preserves genre and vocal constraints.
- Song explicitly about the last few minutes: uses the recent exchange.
- Correction that the first answer was too narrow: changes evidence selection
  and output content, rather than only apologizing or changing the title.
- High-register lyrics: human review of stress, phrase length, vowel shapes,
  repeated hooks, and whether the lines can actually be sung. Text cues alone
  cannot establish musical quality or a safe range for a particular singer.
- Wording correction: adopts `lowkirk`, without resistance or unnecessary search.
- Explicit request for silence: no delivered response and no later invented
  claim of having responded.
- Clarification of Kiki's own joke: concise explanation without a history search.
- Dog correction: responds naturally without insisting the user missed the joke.

Score scope, explicit constraints, factual support, correction uptake, creative
quality, and tone separately. Do not let a good tone score compensate for
contradicting an instruction. Compare repeated runs under fixed settings, using
the same cases for prompt/context changes and any later model comparison.

Retain a bounded, access-controlled record of tool arguments, evidence IDs,
semantic fallback status, and final constraints for debugging. Existing saved
prompts already contain private content; avoid expanding retention casually.

## Implementation status

This is an analysis and proposal. No prompts, service code, production settings,
or deployments were changed. The full private extraction remains outside the
repository; this document includes only the excerpts needed for the audit.
