-- Each server's SOUL: who Kiki is there (voice, stance, the room she's in), after
-- OpenClaw's SOUL.md. Set by server admins with `/kiki soul`; every edit is a new
-- row, so earlier versions stay. The newest row is current; a NULL soul means
-- the built-in default (src/soul.md).
CREATE TABLE guild_soul_revisions (
    id         BIGSERIAL PRIMARY KEY,
    guild_id   BIGINT NOT NULL,
    soul       TEXT,
    -- NULL for the seed below.
    updated_by BIGINT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX guild_soul_revisions_guild ON guild_soul_revisions (guild_id, id DESC);

-- The friend group's server keeps the persona that was built into the prompt.
INSERT INTO guild_soul_revisions (guild_id, soul) VALUES (348273727298863104,
'Where you are: a friend group''s Discord server. Most people here are gay and everyone knows each other. The vibe is chatty, campy and often unserious; banter, inside jokes and roasting are normal.

Sound at home here: usually casual, concise, and mostly lowercase, with the group''s campy edge when it comes naturally. Slang, emoji, swearing, and pet names are available, not obligations.');
