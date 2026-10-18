-- Emoji Kiki reacted with, on the run's reply_to_id message.
ALTER TABLE agent_runs ADD COLUMN reactions TEXT[] NOT NULL DEFAULT '{}';
-- Shows her reactions on messages in later prompts.
CREATE INDEX agent_runs_reactions_idx ON agent_runs (reply_to_id) WHERE reactions <> '{}';
