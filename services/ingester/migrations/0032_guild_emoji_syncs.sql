-- When each guild's emoji list was last replaced, and as of when that list was
-- read from Discord. A synced guild with no emoji differs from one never synced:
-- the agent host's emoji check rejects invented emoji only once a guild is synced.
-- A sync applies only if its snapshot is at least as new as the last one, so a
-- resync's REST list read before a Gateway update can't bring a deleted emoji back.
CREATE TABLE guild_emoji_syncs (
    guild_id    BIGINT PRIMARY KEY,
    snapshot_at TIMESTAMPTZ NOT NULL,
    synced_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Guilds synced before this table: as of their newest sync.
INSERT INTO guild_emoji_syncs (guild_id, snapshot_at, synced_at)
SELECT guild_id, max(synced_at), max(synced_at) FROM guild_emojis GROUP BY guild_id;
