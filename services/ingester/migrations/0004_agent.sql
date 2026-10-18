-- Agent: conversation metadata, the current topic, and the agent run queue.

ALTER TABLE conversations
    ADD COLUMN message_count            INTEGER NOT NULL DEFAULT 0,  -- tagged messages, Kiki's included
    ADD COLUMN last_message_id          BIGINT,
    ADD COLUMN last_reply_at            TIMESTAMPTZ,                 -- Kiki's latest reply
    ADD COLUMN topic_through_message_id BIGINT,                      -- newest message `topic` covers
    ADD COLUMN topic_updated_at         TIMESTAMPTZ,
    -- Why it closed: a new conversation of the same user, or pushed out of the
    -- active pool (MAX_ACTIVE_CONVERSATIONS) by a more recent one.
    ADD COLUMN closed_reason            TEXT CHECK (closed_reason IN ('superseded', 'evicted'));

UPDATE conversations SET closed_reason = 'superseded' WHERE closed_at IS NOT NULL;

UPDATE conversations c SET message_count = s.n, last_message_id = s.last
FROM (SELECT conversation_id, count(*) AS n, max(message_id) AS last
      FROM conversation_messages GROUP BY conversation_id) s
WHERE s.conversation_id = c.id;

-- Keep message_count and last_message_id current for every tagged message,
-- whether the router or the agent tags it.
CREATE FUNCTION count_conversation_message() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    UPDATE conversations
    SET message_count = message_count + 1,
        last_message_id = greatest(last_message_id, NEW.message_id)
    WHERE id = NEW.conversation_id;
    RETURN NULL;
END
$$;

CREATE TRIGGER conversation_messages_count
    AFTER INSERT ON conversation_messages
    FOR EACH ROW EXECUTE FUNCTION count_conversation_message();

CREATE INDEX conversations_open_idx ON conversations (last_active_at DESC) WHERE closed_at IS NULL;

-- Agent runs: a run is superseded when a newer run of the same conversation is
-- answered in its place, and records what the reply cost.
ALTER TABLE agent_runs DROP CONSTRAINT agent_runs_status_check;
ALTER TABLE agent_runs
    ADD CONSTRAINT agent_runs_status_check
        CHECK (status IN ('pending', 'running', 'done', 'failed', 'superseded')),
    ADD COLUMN reply_message_ids BIGINT[],
    ADD COLUMN model             TEXT,
    ADD COLUMN steps             INTEGER,       -- model calls in the agent loop
    ADD COLUMN prompt_tokens     INTEGER,
    ADD COLUMN completion_tokens INTEGER,
    ADD COLUMN cost_usd          DOUBLE PRECISION,
    ADD COLUMN latency_ms        INTEGER;

CREATE FUNCTION notify_agent_run_queued() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM pg_notify('agent_run_queued', NEW.conversation_id::text);
    RETURN NULL;
END
$$;

CREATE TRIGGER agent_runs_notify_queued
    AFTER INSERT ON agent_runs
    FOR EACH ROW EXECUTE FUNCTION notify_agent_run_queued();
