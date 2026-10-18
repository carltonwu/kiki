-- Missing rows inherit OPENROUTER_CHAT_MODEL. Overrides survive process restarts.
CREATE TABLE guild_chat_models (
    guild_id BIGINT PRIMARY KEY,
    model TEXT NOT NULL CHECK (length(model) BETWEEN 3 AND 200),
    updated_by BIGINT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
