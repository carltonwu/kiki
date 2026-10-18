-- Drawings run after Kiki's reply (docs/image-generation.md): the reply queues
-- them, Rust posts its ack, then each drawing is its own host job
-- (`image:<run>:<n>`) and its own follow-up message. Rust owns this row, as it
-- owns agent_runs: a retry reuses the host's saved result instead of drawing again.
CREATE TABLE agent_image_jobs (
    id          BIGSERIAL PRIMARY KEY,
    run_id      BIGINT NOT NULL REFERENCES agent_runs (id),
    position    SMALLINT NOT NULL,          -- nth drawing the reply queued
    reply_to_id BIGINT NOT NULL,            -- the request the follow-up replies to
    -- The host request, snapshotted so retries send identical input.
    request     JSONB NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'drawing', 'delivering', 'done', 'failed')),
    error       TEXT,
    image_id    BIGINT,                     -- agent_host_images row, when drawn
    message_id  BIGINT,                     -- the follow-up Discord message
    cost_usd    DOUBLE PRECISION,           -- references, image and any failure note
    latency_ms  INTEGER,                    -- queued to posted
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    UNIQUE (run_id, position)
);
CREATE INDEX agent_image_jobs_pending_idx ON agent_image_jobs (id) WHERE status = 'pending';

-- What each drawing was made from, for auditing: the final prompt, the subjects
-- and the reference images (message and attachment IDs, or whose avatar), in the
-- order the image model saw them.
ALTER TABLE agent_host_images
    ADD COLUMN prompt           TEXT,
    ADD COLUMN model            TEXT,
    ADD COLUMN subjects         JSONB NOT NULL DEFAULT '[]',
    ADD COLUMN reference_images JSONB NOT NULL DEFAULT '[]',
    ADD COLUMN cost_usd         DOUBLE PRECISION,
    ADD COLUMN latency_ms       INTEGER;
