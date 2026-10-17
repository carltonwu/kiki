-- State for REST catch-up (backfill on start, gap fill after reconnects).

-- Discord's last_message_id for the channel, or the newest live message seen. A
-- channel needs catch-up when this is past complete_through.
ALTER TABLE channels ADD COLUMN last_message_id BIGINT;
ALTER TABLE channels ADD COLUMN deleted_at TIMESTAMPTZ;

CREATE INDEX channels_guild_id_idx ON channels (guild_id);

CREATE TABLE guilds (
    id         BIGINT PRIMARY KEY,
    name       TEXT,
    -- Start time of the last finished thread discovery. A thread archived before
    -- this can't have messages we haven't seen: posting in an archived thread
    -- unarchives it, and re-archiving moves its archive timestamp past this.
    archived_threads_synced_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
