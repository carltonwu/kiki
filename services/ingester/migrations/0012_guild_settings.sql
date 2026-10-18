-- Only overrides are stored; missing keys inherit current process defaults.
CREATE TABLE guild_settings (
    guild_id BIGINT NOT NULL,
    key TEXT NOT NULL,
    value JSONB NOT NULL,
    updated_by BIGINT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (guild_id, key)
);

-- Retain the old table while the previous leader may still be serving.
INSERT INTO guild_settings (guild_id, key, value, updated_by, updated_at)
SELECT guild_id, 'chat_model', to_jsonb(model), updated_by, updated_at FROM guild_chat_models;
