-- Authors Kiki never reads: their messages are stored like any other, but are not
-- routed, embedded, searched, or shown to the agent as context. For bots whose
-- messages are noise, whether or not Discord flags them as bots.
CREATE TABLE ignored_authors (
    user_id BIGINT PRIMARY KEY,
    note    TEXT NOT NULL DEFAULT ''
);

INSERT INTO ignored_authors (user_id, note) VALUES (1358234666267578368, 'bot');

DELETE FROM message_embeddings e USING messages m, ignored_authors i
WHERE m.id = e.message_id AND m.author_id = i.user_id;

CREATE OR REPLACE FUNCTION notify_message_inserted() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.created_at > now() - interval '1 minute'
       AND NOT EXISTS (SELECT 1 FROM ignored_authors WHERE user_id = NEW.author_id) THEN
        PERFORM pg_notify('message_inserted', json_build_object(
            'id', NEW.id,
            'guild_id', NEW.guild_id,
            'channel_id', NEW.channel_id,
            'author_id', NEW.author_id,
            'author_bot', NEW.author_bot,
            'created_at_ms', (extract(epoch FROM NEW.created_at) * 1000)::bigint
        )::text);
    END IF;
    RETURN NULL;
END
$$;
