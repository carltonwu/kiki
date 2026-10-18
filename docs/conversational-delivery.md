# Conversational delivery

One request producing one immediate, referenced Discord reply makes Kiki feel
mechanical. A useful answer may have several natural beats, with a pause between
them; sometimes leaving room for other people is the best contribution. Pending
reactions also expose the machinery. Kiki now considers what to say and how to
deliver it in the same model turn, without exposing a planning monologue or
adding an inference call.

Server settings are editable through `/kiki config key:<name> value:<value>`:

- `chat_min_messages=1`, `chat_max_messages=3` (1–8, minimum ≤ maximum): preferred
  minimum and maximum planned messages. Extra beats are merged; Discord's 1900
  character safety splits may exceed the maximum. The minimum is prompt guidance,
  so Kiki need not pad a short answer with filler.
- `chat_max_delay=3000` (milliseconds, 0–10000): cap each pause. Total pauses are
  capped at eight seconds, the first message has no added delay, and shutdown
  skips remaining pauses. Set `0` for immediate delivery.
- `chat_allow_silence=true`: let Kiki sit out when a reply would add little,
  including substantive human-to-human exchanges in an active chat.
  Set `false` to instruct Kiki to answer each routed turn. A model that violates
  this instruction fails the run rather than posting the silence marker.
- `chat_reply_reference=false`: attach and ping a Discord reply on the first
  message when enabled; otherwise use ordinary channel messages.
- `chat_reactions=true`: let Kiki react to the newest message with emoji, alone
  or with her messages (see [Emoji reactions](#emoji-reactions)). Set `false` to
  remove the instructions; stray markers are then dropped, not posted.
- `status_reactions=true`: enable pending routing reactions. Disabling this
  reduces Discord-visible observability; logs and run records remain available.
  Global `ROUTE_PENDING_EMOJI=""` still disables them. Cleanup remains enabled for
  reactions added before the setting changed. This toggle does not ban emoji in
  Kiki's message text.

## Files and behavior

- `services/ingester/src/guild_settings.rs`: settings, boolean parsing, min/max
  validation and recovery of invalid persisted overrides. Existing JSON settings
  storage needs no migration.
- `services/ingester/src/agent/context.rs`: instruct the model to choose content,
  natural message breaks and pauses, and condition silence rules on the flag.
  Separate messages with a standalone `---`, or `--- 1.5s` for a chosen pause.
  Bare separators use a pause of up to one second; `--- 500ms` and `---2 s` are
  also accepted, and an unparseable pause still separates so markers never leak.
  Code fences (but not one-line ```code``` spans) preserve separators. With a
  maximum of one message the prompt asks for a single message instead.
  Instructions are saved in `host_request.messages`, keeping inference retries
  stable without changing the Python host API or stored request comparisons.
- `services/ingester/src/agent/delivery.rs`: parse those boundaries, clean names
  and emoji, merge excess beats, cap pauses and split overlong messages. Plain text
  from older saved jobs still works. Delivery uses the current settings snapshot.
- `services/ingester/src/agent/discord.rs` and `mod.rs`: paced delivery, optional
  references, shutdown handling, and internal attribution of whom Kiki answered.
  Sends remain serialized per conversation and persist each posted message,
  appending it to the run's `reply_message_ids` in the same transaction so a
  reply to the first message during a pause is attributed correctly.
- `services/ingester/migrations/0015_agent_reply_target.sql`: add and backfill
  internal reply targets so follow-up routing works without Discord references,
  plus a GIN index for "which run posted this Kiki message" lookups.
- `services/ingester/src/router/{mod,classify}.rs`: gate reaction additions and
  read internal attribution for direct follow-ups and Decide's last-answer signal.
- `services/ingester/src/commands.rs`: free-text config keys and a compact full
  listing keep configuration within Discord's 25-choice and 25-field limits.
- Rust unit tests and `tools/sim/scenarios/conversational-delivery.yaml`: verify
  parsing, bounds, silence, references, reactions, and multi-message delivery.
  Existing follow-up scenarios exercise the new default of ordinary channel posts.

## Emoji reactions

Sometimes a reaction is the whole answer; sometimes it goes with a reply. The
model writes a line containing only `react:` and emoji, anywhere outside code:

    react: 😂 <:huh:814672652526420008>
    no WAY
    ---
    congrats

- `services/ingester/src/agent/emoji.rs` parses the line: Unicode emoji (split by
  grapheme, so skin tones, ZWJ sequences, flags and keycaps stay whole) and custom
  emoji as Discord writes them (`<:name:id>`, `<a:name:id>`, and the `<:name:id:>`
  models often write). A line with anything else, like `react: idk`, stays text.
  A shortcode like `:huh:` is looked up by exact name (ignoring case) among the
  server's usable custom emoji in #7's catalog (`guild_emojis`, filled by
  `/kiki emojis resync:true`); Unicode shortcodes like `:skull:` aren't resolved
  but never leak. The prompt points Kiki at `search_emojis`/`find_emoji` for
  custom emoji written as `<:name:id>`.
- `delivery.rs` collects reactions from every line, deduplicated, at most three.
  Reactions alone (or with `NO_REPLY`) are a reaction-only answer, which counts as
  sitting out: with `chat_allow_silence=false` it fails the run like silence does.
- `discord.rs` adds the reactions to the newest trigger message (the same message
  as the reply target) before sending any message, like a person reacting before
  typing. Adding a reaction is idempotent and a rejected emoji (Discord's Unknown
  Emoji) is only logged, so the reply still goes out.
- `agent_runs.reactions` (migration 0019) stores the emoji Discord accepted, with
  `reply_to_id` as their target. Later prompts show them on that message as
  `[you reacted: 💀]`, so Kiki knows a react-only turn already answered it.
- If Kiki reacts with the pending emoji (👀), cleanup leaves it on that message.

`tools/sim/scenarios/emoji-reactions.yaml` covers react-only, react with reply,
custom emoji, a rejected emoji, the pending emoji, `chat_reactions=false` and
react-only with silence disabled, and catalog shortcodes. Fakecord rejects
custom emoji that aren't in the fixture's `emojis`.

## Review decisions

Opus 5.5 reviewed the initial proposal at medium effort. Its findings led to
internal reply attribution, fixing config limits, bounded shutdown-aware pauses,
and keeping the host contract unchanged. A typed host delivery API would add
migration and retry compatibility work without being necessary for this version.
Malformed plans preserve content where possible; mixed silence markers are errors,
including a `NO_REPLY` line (outside code) next to other text.

Pauses increase turn latency. Crashes or send failures can leave a partial reply;
interrupted delivery is failed rather than replayed and duplicated, as before.

Final Opus pass: per-message attribution persistence, indexed attribution
queries, tolerant separator forms, inline triple-backtick spans not treated as
fences, and prompt wording adjusted for one-message and zero-pause settings.

## Validation

Before the final Opus review: 34 Rust tests (35 after it), 15 Python tests, Rust/Python lint and
format checks passed. Simulator: **23 passed, 0 failed, 1 skipped**. A live model
check in fake Discord produced two messages per turn without visible separators.
For example, after a moving update, Kiki sent “oh no, back destroyed from moving
is the gayest injury there is…” followed by “plant, chair, or a lil grill for the
summer? gotta plan it right”. This was judged by reading the transcript; it is a
tone sample, not a deterministic behavior guarantee.
