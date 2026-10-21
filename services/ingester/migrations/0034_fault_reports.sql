-- Unexpected failures, filed as GitHub issues by the ingester leader
-- (src/faults, docs/observability.md "Fault issues"). Both services insert
-- events; the reporter groups them by fingerprint and opens or comments on one
-- issue each. The issues are public: rows hold structured fields only, never
-- message text, exception messages or Discord IDs.
CREATE TABLE fault_events (
    id            BIGSERIAL PRIMARY KEY,
    -- First 16 hex digits of the sha256 of service, kind, error_type, stage,
    -- http_status and tool: one issue per fingerprint, whatever the model.
    fingerprint   TEXT NOT NULL,
    service       TEXT NOT NULL CHECK (service IN ('agent', 'ingester')),
    kind          TEXT NOT NULL,     -- executor_failed, tool_crashed, ...
    job_kind      TEXT,              -- reply, topic, image
    error_type    TEXT,              -- an exception class name, nothing more
    stage         TEXT,              -- reply_agent.run, draw.generate, host.response, ...
    http_status   INTEGER,
    model         TEXT,
    tool          TEXT,
    finish_reason TEXT,
    version       TEXT,              -- RAILWAY_GIT_COMMIT_SHA
    environment   TEXT,              -- RAILWAY_ENVIRONMENT_NAME
    trace_id      TEXT,
    run_id        TEXT,              -- the host job ID (reply:42, image:42:0)
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Set once the reporter has counted it into an issue (or dropped it).
    reported_at   TIMESTAMPTZ
);
CREATE INDEX fault_events_unreported_idx ON fault_events (fingerprint, id)
    WHERE reported_at IS NULL;
CREATE INDEX fault_events_reported_idx ON fault_events (reported_at)
    WHERE reported_at IS NOT NULL;

-- The issue tracking each fingerprint.
CREATE TABLE fault_issues (
    fingerprint     TEXT PRIMARY KEY,
    issue_number    INTEGER NOT NULL,
    -- When the reporter opened that issue; NULL for one it found by search.
    -- Bounds new issues an hour.
    filed_at        TIMESTAMPTZ,
    occurrences     BIGINT NOT NULL DEFAULT 0,
    -- Occurrences since the last comment (or the issue), for the next comment.
    unreported      BIGINT NOT NULL DEFAULT 0,
    last_comment_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- The latest occurrence's, for that comment.
    last_version    TEXT,
    last_trace_id   TEXT,
    last_run_id     TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE FUNCTION notify_fault_event() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM pg_notify('fault_event', NEW.kind);
    RETURN NULL;
END
$$;

CREATE TRIGGER fault_events_notify
    AFTER INSERT ON fault_events
    FOR EACH ROW EXECUTE FUNCTION notify_fault_event();
