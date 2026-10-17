-- Core ingestion tables. Discord snowflake ids fit in BIGINT (they are < 2^63).

CREATE TABLE channels (
    id               BIGINT PRIMARY KEY,
    guild_id         BIGINT NOT NULL,
    parent_id        BIGINT,               -- category, or parent channel for threads
    name             TEXT,
    type             SMALLINT NOT NULL,    -- Discord channel type
    -- Newest message id up to which we know we have every message. Only REST
    -- catch-up (or an unbroken Gateway session since the last catch-up) advances
    -- this; live events never do, so gaps before them still get filled.
    complete_through BIGINT NOT NULL DEFAULT 0,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE messages (
    id          BIGINT PRIMARY KEY,
    guild_id    BIGINT NOT NULL,
    channel_id  BIGINT NOT NULL,           -- channel or thread
    author_id   BIGINT NOT NULL,
    author_name TEXT,
    author_bot  BOOLEAN NOT NULL DEFAULT false,
    content     TEXT NOT NULL DEFAULT '',
    type        SMALLINT NOT NULL DEFAULT 0,
    reply_to_id BIGINT,
    created_at  TIMESTAMPTZ NOT NULL,
    edited_at   TIMESTAMPTZ,
    deleted_at  TIMESTAMPTZ,               -- soft delete; Discord's API can't return deleted messages
    raw         JSONB NOT NULL,            -- full message object as last seen
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX messages_channel_id_idx ON messages (channel_id, id DESC);
CREATE INDEX messages_author_idx ON messages (author_id, id DESC);
CREATE INDEX messages_reply_to_idx ON messages (reply_to_id) WHERE reply_to_id IS NOT NULL;

-- Gateway session state, saved so a restarted instance can RESUME instead of
-- re-identifying (Discord replays missed events on resume).
CREATE TABLE gateway_sessions (
    shard_id    INTEGER PRIMARY KEY,
    session_id  TEXT NOT NULL,
    resume_url  TEXT NOT NULL,
    sequence    BIGINT NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
