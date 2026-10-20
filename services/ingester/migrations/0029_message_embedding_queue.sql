-- Messages waiting for an embedding. "Pending" used to mean a searchable
-- message with no row in message_embeddings, found by an anti-join from the
-- newest message down. With nothing pending, that walks every message: in
-- production (236k messages, 270 MB heap, 384 MB Postgres) it took 5 to 7 s at
-- the start of every reply, read the whole heap through a 32 MB buffer pool, and
-- held the reply's connection, so search_messages and channel_history timed out
-- (10 s) behind it. The queue makes finding pending messages O(pending).
--
-- Triggers keep it: a new searchable message is queued, an edit re-queues it
-- (its vector is dropped, migration 0007), a delete or an edit to nothing
-- unqueues it. The host removes a row once its vector is saved. The host's
-- query still checks every condition, so a stale row is skipped, never
-- embedded, and the host deletes it. Nothing re-queues a message when its
-- author is taken off ignored_authors: run `kiki-agent-host index` after that.
-- It re-queues anything searchable without a vector before draining.
--
-- The searchability rule is the host's (chat/scope.py PEOPLES and TEXT,
-- chat/indexing.py SEARCHABLE), repeated here; test_retrieval.py checks that
-- they agree.
--
-- Locks: CREATE TRIGGER takes SHARE ROW EXCLUSIVE on messages until the
-- migration commits, so ingestion waits out the backfill below (about 7 to 10 s
-- on production at 236k messages). Messages are stored late; one whose write
-- gives up is fetched again by catch-up (live events never advance it).
CREATE TABLE message_embedding_queue (
    message_id BIGINT PRIMARY KEY REFERENCES messages (id) ON DELETE CASCADE
);

-- Searchable as the host defines it (chat/scope.py PEOPLES and TEXT): people's
-- messages and link fixers' reposts of them, not ignored authors', not deleted,
-- with text. relayed_for and relayed_text are virtual, so the row is read back
-- rather than trusting NEW for them.
CREATE FUNCTION queue_message_embedding(BIGINT) RETURNS void LANGUAGE sql AS $$
    DELETE FROM message_embedding_queue WHERE message_embedding_queue.message_id = $1;
    INSERT INTO message_embedding_queue (message_id)
    SELECT m.id FROM messages m
    WHERE m.id = $1 AND m.deleted_at IS NULL
      AND (NOT m.author_bot OR m.relayed_for IS NOT NULL)
      AND (m.author_id <> ALL (ARRAY(SELECT user_id FROM ignored_authors))
           OR m.relayed_for IS NOT NULL)
      AND btrim(coalesce(m.relayed_text, m.content)) <> ''
      AND NOT EXISTS (SELECT 1 FROM message_embeddings e WHERE e.message_id = m.id)
    -- A row another transaction queued and hasn't committed yet (`kiki-agent-host
    -- index` queuing everything) is invisible to the DELETE above. Without this
    -- the insert would fail on it, and so would the ingester's write of the
    -- message, losing an edit.
    ON CONFLICT (message_id) DO NOTHING;
$$;

CREATE FUNCTION queue_inserted_message() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM queue_message_embedding(NEW.id);
    RETURN NULL;
END
$$;

CREATE TRIGGER messages_queue_embedding
    AFTER INSERT ON messages
    FOR EACH ROW
    WHEN (NEW.deleted_at IS NULL)
    EXECUTE FUNCTION queue_inserted_message();

-- Same trigger and condition as before (0007), now re-queuing after the drop.
CREATE OR REPLACE FUNCTION drop_stale_embedding() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    DELETE FROM message_embeddings WHERE message_id = NEW.id;
    PERFORM queue_message_embedding(NEW.id);
    RETURN NULL;
END
$$;

-- What's pending today (the old definition), once.
INSERT INTO message_embedding_queue (message_id)
SELECT m.id FROM messages m
WHERE m.deleted_at IS NULL
  AND (NOT m.author_bot OR m.relayed_for IS NOT NULL)
  AND (m.author_id <> ALL (ARRAY(SELECT user_id FROM ignored_authors))
       OR m.relayed_for IS NOT NULL)
  AND btrim(coalesce(m.relayed_text, m.content)) <> ''
  AND NOT EXISTS (SELECT 1 FROM message_embeddings e WHERE e.message_id = m.id);
