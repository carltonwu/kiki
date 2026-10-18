CREATE INDEX conversations_active_guild ON conversations (guild_id, last_active_at DESC)
WHERE closed_at IS NULL;
