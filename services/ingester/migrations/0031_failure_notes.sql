-- A run whose reply_message_ids are the ingester's failure note, "(couldn't
-- answer that one, try again?)", rather than Kiki's words. Set with
-- status = 'delivering' before the note is sent, so the note is known by where
-- it came from, not by its text, and left out of every prompt.
ALTER TABLE agent_runs ADD COLUMN failure_note BOOLEAN NOT NULL DEFAULT false;
CREATE INDEX agent_runs_failure_note_idx ON agent_runs (id) WHERE failure_note;
