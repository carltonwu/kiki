-- Snapshot requests so wake-up retries and ingester restarts use identical input.
ALTER TABLE agent_runs ADD COLUMN host_request JSONB;
-- Legacy in-process runs may already have posted; never replay them on upgrade.
UPDATE agent_runs SET status = 'failed', error = 'interrupted during agent host migration',
    finished_at = now() WHERE status = 'running';
ALTER TABLE agent_runs DROP CONSTRAINT agent_runs_status_check;
ALTER TABLE agent_runs ADD CONSTRAINT agent_runs_status_check
    CHECK (status IN ('pending', 'running', 'delivering', 'done', 'failed', 'superseded'));

-- Owned by the Python host. No polling or persistent connection is needed.
-- Both replies and topic summaries use stable IDs and cache their result here.
CREATE TABLE agent_host_jobs (
    id          TEXT PRIMARY KEY,
    request     JSONB NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed')),
    result      JSONB,
    error       TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
