-- Images drawn by the host's generate_image tool. The job result lists their IDs
-- and Rust attaches the bytes to the reply, so result JSON stays small.
-- Deleting a job (future retention) deletes its images.
CREATE TABLE agent_host_images (
    id          BIGSERIAL PRIMARY KEY,
    job_id      TEXT NOT NULL REFERENCES agent_host_jobs (id) ON DELETE CASCADE,
    mime_type   TEXT NOT NULL,
    data        BYTEA NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX agent_host_images_job_id ON agent_host_images (job_id);
