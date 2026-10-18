-- Message search for the agent host: full text plus pgvector embeddings.
-- Railway's Postgres image ships pgvector; the Python host computes and writes
-- embeddings, Rust only owns the schema.

CREATE EXTENSION IF NOT EXISTS vector;

-- Must match the expression in the host's queries to be used.
CREATE INDEX messages_fts_idx ON messages USING gin (to_tsvector('english', content));

-- One embedding model at a time (qwen/qwen3-embedding-8b, first 1024 dims,
-- L2-normalized). Changing the model means truncating this table and re-indexing.
-- "Pending" is simply a searchable message without a row here.
CREATE TABLE message_embeddings (
    message_id BIGINT PRIMARY KEY REFERENCES messages (id) ON DELETE CASCADE,
    embedding  halfvec(1024) NOT NULL
);

-- Edits and deletes drop the derived vector, whichever path changed the row.
CREATE FUNCTION drop_stale_embedding() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    DELETE FROM message_embeddings WHERE message_id = NEW.id;
    RETURN NULL;
END
$$;

CREATE TRIGGER messages_embedding_stale
    AFTER UPDATE OF content, deleted_at ON messages
    FOR EACH ROW
    WHEN (OLD.content IS DISTINCT FROM NEW.content OR NEW.deleted_at IS NOT NULL)
    EXECUTE FUNCTION drop_stale_embedding();
