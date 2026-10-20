-- Runs in flight, by start time: the platform poller counts runs running or
-- delivering for over 10 minutes (kiki.agent_runs.stuck) every minute. Without
-- this it scans every run ever made. Few rows match, so the index stays tiny.
CREATE INDEX agent_runs_active_idx ON agent_runs (started_at)
    WHERE status IN ('running', 'delivering');
