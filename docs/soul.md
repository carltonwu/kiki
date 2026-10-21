# Per-server SOUL

Status: implemented.

After [OpenClaw's SOUL.md](https://docs.openclaw.ai/concepts/soul): a short, editable
statement of who Kiki is in a server, separate from her operating instructions. Before
this, the friend group's persona ("most people here are gay… campy… mostly lowercase")
was built into the system prompt, so every server got it.

## What goes where

- **SOUL (per server):** the room she's in, voice, register, stance, humor, bluntness.
  Short beats long; rules like "be professional" do nothing.
- **Fixed prompt (every server):** who she is in general (`You are Kiki, an AI member…`),
  honesty about being an AI, conversational judgment (`agent/conversation.md`), tools,
  people and identity handling, memory, delivery.

The system prompt shows the SOUL right after the opening line, in `<soul>` tags, framed as
shaping voice and stance but not the guidance on tools and people. It's admin-written text
in the system prompt, like `guild_lingo`; people's chat messages still never go there.

## Storage

`guild_soul_revisions` (migration 0035): one row per edit, newest current; a `NULL` soul
means the built-in default (`services/ingester/src/soul.md`). Earlier versions stay, so a
bad edit can be restored from SQL. The migration seeds the friend group's server with the
persona text that used to be in the prompt, so its behavior doesn't change.

The SOUL is read with the rest of a run's context. It's in the system prompt, so changing it
invalidates the provider's prompt cache for that server once; retries reuse the stored
`host_request`.

## Editing

`/kiki soul` shows the current text and whether it's the default or who set it.
`edit:true` opens a Discord modal with the current text (a modal must be the first
response, so it's sent before the usual ephemeral deferral); submitting it saves a new
revision. `reset:true` adds a `NULL` revision. Edits and submissions check Manage Server
each time. Text is trimmed and limited to 4000 characters, Discord's modal limit.

## Not done

OpenClaw lets the agent rewrite its own SOUL.md and tell the user. In a group chat, anyone
could talk Kiki into that, so only admins edit it here. A self-edit proposal that an admin
approves would be the safe version.
