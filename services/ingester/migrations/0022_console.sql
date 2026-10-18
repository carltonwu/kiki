-- The web console (docs/console-hld.md): editable facts and prompt parts per
-- server, console sessions, chat threads and an audit log of console writes.

-- Background notes listed in the system prompt; read per reply, no cache.
CREATE TABLE guild_facts (
    id         BIGSERIAL PRIMARY KEY,
    guild_id   BIGINT NOT NULL,
    title      TEXT NOT NULL CHECK (length(btrim(title)) BETWEEN 1 AND 120),
    body       TEXT NOT NULL CHECK (length(btrim(body)) BETWEEN 1 AND 2000),
    enabled    BOOLEAN NOT NULL DEFAULT true,
    updated_by BIGINT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX guild_facts_guild_idx ON guild_facts (guild_id, updated_at DESC);

-- Overrides of the editable system prompt parts; a missing row uses the built-in text.
CREATE TABLE guild_prompts (
    guild_id   BIGINT NOT NULL,
    part       TEXT NOT NULL CHECK (part IN ('persona', 'guidance')),
    body       TEXT NOT NULL CHECK (length(btrim(body)) BETWEEN 1 AND 8000),
    updated_by BIGINT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (guild_id, part)
);

-- The cookie holds a random token; only its hash is stored.
CREATE TABLE console_sessions (
    token_hash   BYTEA PRIMARY KEY,
    user_id      BIGINT NOT NULL,
    user_name    TEXT NOT NULL,
    operator     BOOLEAN NOT NULL,
    guild_ids    BIGINT[] NOT NULL,       -- servers this person may manage
    access_token TEXT NOT NULL,           -- Discord OAuth (identify guilds), for re-checks
    csrf         TEXT NOT NULL,
    checked_at   TIMESTAMPTZ NOT NULL,    -- last permission check against Discord
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at   TIMESTAMPTZ NOT NULL
);

CREATE INDEX console_sessions_expiry_idx ON console_sessions (expires_at);

-- Chat with Kiki outside Discord. Never in `messages`, so never routed.
CREATE TABLE console_chat_messages (
    id                BIGSERIAL PRIMARY KEY,
    thread_id         UUID NOT NULL,
    guild_id          BIGINT NOT NULL,
    user_id           BIGINT NOT NULL,
    role              TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content           TEXT NOT NULL,
    model             TEXT,
    prompt_tokens     INTEGER,
    completion_tokens INTEGER,
    cost_usd          DOUBLE PRECISION,
    latency_ms        INTEGER,
    error             TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX console_chat_thread_idx ON console_chat_messages (thread_id, id);
CREATE INDEX console_chat_guild_idx ON console_chat_messages (guild_id, created_at);

CREATE TABLE console_audit (
    id       BIGSERIAL PRIMARY KEY,
    at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor_id BIGINT NOT NULL,
    guild_id BIGINT,
    action   TEXT NOT NULL,
    detail   JSONB NOT NULL DEFAULT '{}'
);

CREATE INDEX console_audit_guild_idx ON console_audit (guild_id, at DESC);

-- Any writer (console via the ingester, a second instance, manual SQL) reaches
-- every ingester's settings cache.
CREATE FUNCTION notify_guild_settings_changed() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM pg_notify('guild_settings_changed',
                      (CASE WHEN TG_OP = 'DELETE' THEN OLD.guild_id ELSE NEW.guild_id END)::text);
    RETURN NULL;
END
$$;

CREATE TRIGGER guild_settings_notify
    AFTER INSERT OR UPDATE OR DELETE ON guild_settings
    FOR EACH ROW EXECUTE FUNCTION notify_guild_settings_changed();
