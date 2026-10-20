-- A run whose reply_message_ids are the ingester's failure note, "(couldn't
-- answer that one, try again?)", rather than Kiki's words. Set with
-- status = 'delivering' before the note is sent, so prompts can leave out the
-- note by where it came from (agent::NOT_A_FAILURE_NOTE, which also matches its
-- text, for a note whose send response was lost). The partial index holds just
-- those runs' messages, for that filter's one InitPlan.
ALTER TABLE agent_runs ADD COLUMN failure_note BOOLEAN NOT NULL DEFAULT false;
CREATE INDEX agent_runs_failure_note_idx ON agent_runs (id) INCLUDE (reply_message_ids)
    WHERE failure_note;
