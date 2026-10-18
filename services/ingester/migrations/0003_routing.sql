-- Message routing: classification results, conversations, and the agent queue.

-- Tell the router about each newly stored message. INSERT only: edits arrive as
-- ON CONFLICT DO UPDATE and must not re-route. Messages older than a minute (REST
-- backfill and catch-up) are never routed, so they don't notify at all; the router
-- applies the precise age limit itself.
CREATE FUNCTION notify_message_inserted() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.created_at > now() - interval '1 minute' THEN
        PERFORM pg_notify('message_inserted', json_build_object(
            'id', NEW.id,
            'guild_id', NEW.guild_id,
            'channel_id', NEW.channel_id,
            'author_id', NEW.author_id,
            'author_bot', NEW.author_bot,
            'created_at_ms', (extract(epoch FROM NEW.created_at) * 1000)::bigint
        )::text);
    END IF;
    RETURN NULL;
END
$$;

CREATE TRIGGER messages_notify_inserted
    AFTER INSERT ON messages
    FOR EACH ROW EXECUTE FUNCTION notify_message_inserted();

CREATE TABLE conversations (
    id             BIGSERIAL PRIMARY KEY,
    guild_id       BIGINT NOT NULL,
    channel_id     BIGINT NOT NULL,
    started_by     BIGINT NOT NULL,        -- user who started it
    topic          TEXT,                   -- one-line summary, set by the agent later
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_active_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    closed_at      TIMESTAMPTZ             -- superseded by a new conversation of the same user
);

CREATE INDEX conversations_channel_idx ON conversations (channel_id, last_active_at DESC)
    WHERE closed_at IS NULL;

-- Messages that are part of a conversation (the "Tag Message" step).
CREATE TABLE conversation_messages (
    message_id      BIGINT PRIMARY KEY REFERENCES messages (id),
    conversation_id BIGINT NOT NULL REFERENCES conversations (id),
    -- How it joined: reply to Kiki, @Kiki mention, Jev decision, or Kiki's own reply.
    via             TEXT NOT NULL CHECK (via IN ('reply', 'mention', 'decide', 'agent')),
    tagged_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX conversation_messages_conversation_idx ON conversation_messages (conversation_id);

-- Every message that went through the classification pipeline, including the
-- ones that got no response (kept for tuning the checks and Jev).
CREATE TABLE classifications (
    message_id      BIGINT PRIMARY KEY REFERENCES messages (id),
    pool_id         BIGINT NOT NULL,       -- newest message id of the pool it was classified in
    classified_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- First check that matched: 1 reply/mention, 2 keyword, 3 active conversation.
    check_hit       TEXT NOT NULL CHECK (check_hit IN ('reply', 'mention', 'keyword', 'active_conversation', 'none')),
    jev_model       TEXT,                  -- Jev version that answered (via OpenRouter)
    jev_choice      TEXT,                  -- NULL when Jev wasn't asked
    jev_confidence  REAL,
    jev_cost_usd    DOUBLE PRECISION,
    jev_latency_ms  INTEGER,
    jev_error       TEXT,
    outcome         TEXT NOT NULL CHECK (outcome IN ('continue', 'new', 'no')),
    conversation_id BIGINT REFERENCES conversations (id)
);

CREATE INDEX classifications_pool_idx ON classifications (pool_id);

-- Work for the agent ("Pass to Agent"). One row per routed pool.
CREATE TABLE agent_runs (
    id                  BIGSERIAL PRIMARY KEY,
    conversation_id     BIGINT NOT NULL REFERENCES conversations (id),
    trigger_message_ids BIGINT[] NOT NULL,
    status              TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'running', 'done', 'failed')),
    error               TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at          TIMESTAMPTZ,
    finished_at         TIMESTAMPTZ
);

CREATE INDEX agent_runs_pending_idx ON agent_runs (created_at) WHERE status = 'pending';
