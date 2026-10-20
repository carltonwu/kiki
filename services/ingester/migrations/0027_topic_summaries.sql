-- One row per topic summary job, with what it cost and when: `/kiki stats`
-- windows topic spend like every other cost, and a retry the host answers from
-- its saved result (same job ID, not billed again) adds nothing. Before, the
-- cost went only into conversations.topic_cost_usd: untimestamped, and added
-- again on every such retry. That column is no longer read or written; it stays
-- so an older ingester running during a deploy keeps working.
CREATE TABLE topic_summaries (
    job_id          TEXT PRIMARY KEY,                     -- the host job (topic:<conversation>:…)
    conversation_id BIGINT NOT NULL REFERENCES conversations (id),
    cost_usd        DOUBLE PRECISION NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX topic_summaries_conversation ON topic_summaries (conversation_id);
CREATE INDEX topic_summaries_created_at ON topic_summaries (created_at);

-- The topic jobs so far, from the host's saved results.
INSERT INTO topic_summaries (job_id, conversation_id, cost_usd, created_at)
SELECT j.id, c.id, coalesce((j.result->'usage'->>'cost')::float8, 0), j.updated_at
FROM agent_host_jobs j
JOIN conversations c ON c.id::text = j.request->'scope'->>'conversation_id'
WHERE j.request->>'kind' = 'topic' AND j.status = 'completed'
ON CONFLICT (job_id) DO NOTHING;
