-- One row per topic summary job, with what it cost and when: `/kiki stats`
-- windows topic spend like every other cost. Before, the cost went only into
-- conversations.topic_cost_usd: untimestamped, and added again whenever a retry
-- got the host's saved result (same job, not billed again). That column is no
-- longer read or written; it stays so an older ingester running during a
-- deploy keeps working.
CREATE TABLE topic_summaries (
    job_id          TEXT PRIMARY KEY,                     -- the host job (topic:<conversation>:…)
    conversation_id BIGINT NOT NULL REFERENCES conversations (id),
    cost_usd        DOUBLE PRECISION NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX topic_summaries_conversation ON topic_summaries (conversation_id);
CREATE INDEX topic_summaries_created_at ON topic_summaries (created_at);

-- Recorded where the host saves a topic job's result, whichever ingester asked
-- for it: an older one still running during a deploy writes only topic_cost_usd,
-- and wouldn't ask again once its summary advanced the topic. A job completes
-- once (a retry reuses the saved row), and the job ID makes it idempotent anyway.
-- Never fails the host's write: an unknown conversation or a cost that isn't a
-- number records nothing or 0.
CREATE FUNCTION topic_summaries_record() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    conversation TEXT := NEW.request->'scope'->>'conversation_id';
BEGIN
    IF conversation ~ '^[0-9]{1,18}$' THEN
        INSERT INTO topic_summaries (job_id, conversation_id, cost_usd, created_at)
        SELECT NEW.id, c.id,
               CASE WHEN jsonb_typeof(NEW.result->'usage'->'cost') = 'number'
                    THEN (NEW.result->'usage'->>'cost')::float8 ELSE 0 END,
               NEW.updated_at
        FROM conversations c WHERE c.id = conversation::bigint
        ON CONFLICT (job_id) DO NOTHING;
    END IF;
    RETURN NULL;
END $$;

CREATE TRIGGER topic_summaries_record AFTER INSERT OR UPDATE OF status ON agent_host_jobs
    FOR EACH ROW WHEN (NEW.status = 'completed' AND NEW.request->>'kind' = 'topic')
    EXECUTE FUNCTION topic_summaries_record();

-- The topic jobs completed so far. CREATE TRIGGER holds off writes to
-- agent_host_jobs until this migration commits, so a job completes either
-- before (and is copied here) or after (and the trigger records it). In prod
-- this scans about 600 rows (4 MB), in milliseconds.
INSERT INTO topic_summaries (job_id, conversation_id, cost_usd, created_at)
SELECT j.id, c.id,
       CASE WHEN jsonb_typeof(j.result->'usage'->'cost') = 'number'
            THEN (j.result->'usage'->>'cost')::float8 ELSE 0 END,
       j.updated_at
FROM agent_host_jobs j
JOIN conversations c ON c.id::text = j.request->'scope'->>'conversation_id'
WHERE j.request->>'kind' = 'topic' AND j.status = 'completed'
ON CONFLICT (job_id) DO NOTHING;
