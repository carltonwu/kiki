-- One row per paid emoji description, so `/kiki stats` counts a description in
-- the window it was made. guild_emojis.describe_cost_usd adds up every
-- description of an emoji, but described_at is only the latest: windowed on it,
-- a redescription would bring all the earlier spend into today.
CREATE TABLE emoji_description_costs (
    id         BIGSERIAL PRIMARY KEY,
    emoji_id   BIGINT NOT NULL REFERENCES guild_emojis (id),
    guild_id   BIGINT NOT NULL,
    cost_usd   DOUBLE PRECISION NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX emoji_description_costs_guild ON emoji_description_costs (guild_id, created_at);

-- The agent host adds each description's cost to describe_cost_usd; record what
-- it added, whichever version of the host wrote it.
CREATE FUNCTION emoji_description_costs_record() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    added DOUBLE PRECISION := coalesce(NEW.describe_cost_usd, 0);
BEGIN
    IF TG_OP = 'UPDATE' THEN
        added := added - coalesce(OLD.describe_cost_usd, 0);
    END IF;
    IF added > 0 THEN
        INSERT INTO emoji_description_costs (emoji_id, guild_id, cost_usd)
        VALUES (NEW.id, NEW.guild_id, added);
    END IF;
    RETURN NULL;
END $$;

CREATE TRIGGER emoji_description_costs_record
    AFTER INSERT OR UPDATE OF describe_cost_usd ON guild_emojis
    FOR EACH ROW EXECUTE FUNCTION emoji_description_costs_record();

-- Spend so far, at the latest description: when earlier ones were made isn't
-- recorded. (In prod all 79 were described in one resync, on 2026-10-18.)
INSERT INTO emoji_description_costs (emoji_id, guild_id, cost_usd, created_at)
SELECT id, guild_id, describe_cost_usd, coalesce(described_at, synced_at)
FROM guild_emojis WHERE describe_cost_usd > 0;
