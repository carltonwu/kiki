-- W3C traceparent of the span that queued the row (docs/observability.md), so
-- the task that picks it up from Postgres continues the same trace. NULL when
-- telemetry export is off; ignored when malformed.
ALTER TABLE agent_runs ADD COLUMN trace_context TEXT;
ALTER TABLE agent_image_jobs ADD COLUMN trace_context TEXT;
