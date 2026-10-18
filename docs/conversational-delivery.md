# Conversational delivery

Status: design proposal; the delivery plan and settings below are not implemented.

Kiki currently produces one text answer per run, attaches it to a Discord message,
and splits only at Discord's length limit. Human conversation has natural beats:
one thought may take several messages, a pause can separate thoughts, and a laugh
or acknowledgment may need no answer. Status reactions also expose the machinery.

Have the reply model consider what is useful to say, then return a delivery plan
containing ordered message bodies and a delay before each message. Do not request,
store, or display private reasoning. Rust validates and delivers the plan; the
Python host retains ownership of inference and its durable result cache.

Proposed server settings, editable through `/kiki config`:

- `chat_min_messages=1`, `chat_max_messages=3`: bounds for a non-silent turn
  (1–8, minimum must not exceed maximum). Natural message breaks, not random counts.
- `chat_max_delay_ms=3000`: cap on each model-chosen pause (0–10000 ms).
- `chat_allow_silence=true`: model may choose no response; disabling it requires
  an answer even for acknowledgment-only messages.
- `chat_reply_reference=false`: whether the first message attaches a Discord
  reply reference and pings the author. Ordinary channel posts are the default.
- `status_reactions=true`: enable pending/explicit routing reactions; disabling
  reduces Discord-visible observability while keeping logs and run records.

Implementation map:

- `services/ingester/src/guild_settings.rs`: settings, boolean parsing,
  cross-field validation and invalid-override recovery; existing JSON storage
  needs no migration. Check `/kiki config` presentation for Discord field limits.
- `services/ingester/src/agent/context.rs`: replace conflicting NO_REPLY rules
  with a final trusted delivery instruction based on the settings snapshot.
- `services/ingester/src/agent/host.rs` and `mod.rs`: persist delivery policy with
  the job request so retries keep the same constraints; validate structured
  results, silence and count limits before marking delivery. Old jobs remain valid.
- `services/agent/src/kiki_agent_host/contracts.py` and `agent.py`: optional policy
  and typed plan output for new reply jobs; plain text remains for topics and old
  jobs. Keep one inference loop with search tools, not an extra planning call.
- `services/ingester/src/agent/discord.rs`: bounded asynchronous pauses, optional
  first reply reference, existing per-message persistence and failure behavior.
  Reject overlong planned messages rather than silently exceeding the count cap.
- `services/ingester/src/router/`: gate reaction additions on server settings;
  allow cleanup of reactions already added when settings change mid-run.
- Python/Rust tests and simulator scenarios: multi-message delivery, delay caps,
  optional silence, reply reference/reaction toggles, invalid plans, old jobs and
  partial delivery failures. Run CI checks and the local simulator.

Risks: extra pauses increase turn latency; bounds prevent unbounded delays and
spam. A crash during delivery still fails the run instead of replaying already
posted messages. Later runs remain serialized per conversation. Keep generated
message content out of routine logs. Deploy the backward-compatible Python host
before Rust starts sending the optional policy field.
