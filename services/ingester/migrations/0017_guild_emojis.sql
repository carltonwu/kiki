-- A server's custom emoji, described by a vision model so Kiki can find them by
-- meaning (`search_emojis`) or name (`find_emoji`). `/kiki emojis resync:true`
-- copies the list from Discord (Rust) and asks the agent host to describe new
-- ones (Python). An emoji's image never changes, so a description stays valid
-- across renames; emoji gone from Discord are kept with `removed_at` so an old
-- message's `<:name:id>` can still be explained.
CREATE TABLE guild_emojis (
    id                    BIGINT PRIMARY KEY,           -- Discord emoji id
    guild_id              BIGINT NOT NULL,
    name                  TEXT NOT NULL,
    animated              BOOLEAN NOT NULL DEFAULT false,
    -- False when the server lost the boost level the emoji needs.
    available             BOOLEAN NOT NULL DEFAULT true,
    raw                   JSONB NOT NULL,
    synced_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    removed_at            TIMESTAMPTZ,
    description           TEXT,
    described_at          TIMESTAMPTZ,
    describe_model        TEXT,
    describe_cost_usd     DOUBLE PRECISION,
    -- Claimed by a describe call; each resync tries an emoji at most once.
    describe_attempted_at TIMESTAMPTZ,
    -- `:name: description`, embedded like messages (agent-tools-hld.md).
    embedding             halfvec(1024)
);

CREATE INDEX guild_emojis_guild ON guild_emojis (guild_id) WHERE removed_at IS NULL;

-- A rename or new description changes the embedded text.
CREATE FUNCTION guild_emojis_stale_embedding() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.name IS DISTINCT FROM OLD.name OR NEW.description IS DISTINCT FROM OLD.description THEN
        NEW.embedding := NULL;
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER guild_emojis_stale_embedding BEFORE UPDATE OF name, description ON guild_emojis
    FOR EACH ROW EXECUTE FUNCTION guild_emojis_stale_embedding();
