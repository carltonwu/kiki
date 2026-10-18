-- Attribution is independent of the optional Discord reply reference.
ALTER TABLE agent_runs ADD COLUMN reply_to_id BIGINT;
UPDATE agent_runs ar SET reply_to_id = m.reply_to_id
FROM messages m WHERE m.id = ar.reply_message_ids[1];
-- Finds the run that posted a given Kiki message.
CREATE INDEX agent_runs_reply_message_ids_idx ON agent_runs USING GIN (reply_message_ids);
