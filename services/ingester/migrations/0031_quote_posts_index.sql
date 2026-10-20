-- no-transaction
-- A guild's quote posts (0030): about 1,400 in prod, where finding them without
-- this reads every message (2.4 s). Built concurrently so message writes go on
-- meanwhile; that can't run in a transaction, so it's its own migration and its
-- only statement. If it fails, it leaves an INVALID index: drop it and redeploy.
CREATE INDEX CONCURRENTLY messages_quote_posts_idx ON messages (guild_id, quoted_message_id(raw))
    WHERE author_bot AND quoted_message_id(raw) IS NOT NULL
