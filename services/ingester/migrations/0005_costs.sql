-- Per-conversation LLM cost: chat runs (agent_runs.cost_usd), Decide
-- (classifications.jev_cost_usd) and topic summaries, recorded here.

ALTER TABLE conversations ADD COLUMN topic_cost_usd DOUBLE PRECISION NOT NULL DEFAULT 0;

CREATE INDEX agent_runs_conversation_idx ON agent_runs (conversation_id);
CREATE INDEX classifications_conversation_idx ON classifications (conversation_id)
    WHERE conversation_id IS NOT NULL;
