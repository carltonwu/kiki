-- A server's own phrases and emoji, so Kiki can talk like the group. Listed in
-- her prompt; each reply may also nudge her to use one (setting `lingo_nudge`).
-- Curated by hand for now; continuous learning is a separate design
-- (docs/guild-language-learning-hld.md).
CREATE TABLE guild_lingo (
    guild_id BIGINT NOT NULL,
    phrase   TEXT NOT NULL,
    meaning  TEXT NOT NULL,
    PRIMARY KEY (guild_id, phrase)
);

-- Mined from the friend group's server (2024–2026 messages, distinctive words
-- and frequent short messages), slurs and self-harm shorthand left out.
INSERT INTO guild_lingo (guild_id, phrase, meaning) VALUES
    (348273727298863104, 'r8 da eats', 'asking someone (usually Ethan) to rate a food pic'),
    (348273727298863104, 'im gagged', 'shocked or amazed, campy'),
    (348273727298863104, 'chills', 'that was amazing; also "holy chills"'),
    (348273727298863104, 'im crying', 'that''s hilarious; also "im sick"'),
    (348273727298863104, 'helpppp', 'reacting to something absurd or embarrassing'),
    (348273727298863104, 'how it feels to …', 'caption-style setup for a relatable situation'),
    (348273727298863104, 'lowk', 'lowkey'),
    (348273727298863104, 'deadass', 'seriously, no joke'),
    (348273727298863104, 'truly', 'one-word agreement; also "so true", "real", "real shit"'),
    (348273727298863104, '+2', 'comedy points for a funny message (also +1, -1, -2), usually as a reply'),
    (348273727298863104, 'bby', 'affectionate address, like "sis" or "girl"'),
    (348273727298863104, 'goated', 'the best'),
    (348273727298863104, 'lmfaooo', 'laughing (more o''s, more laughing)'),
    (348273727298863104, 'i-', 'speechless; also "im-"'),
    (348273727298863104, 'this is so …', 'reaction opener: "this is so real", "this is so fucking funny"'),
    (348273727298863104, 'oh hell yeah', 'excited agreement'),
    (348273727298863104, 'nougie', 'Austin''s dog (with Piper), a server favorite'),
    (348273727298863104, '<:monkeded:832036455609270326>', 'cheeky or guilty grin after saying something a little embarrassing'),
    (348273727298863104, '<:huh:1085017182301339648>', 'confused, "what?"'),
    (348273727298863104, '<:tt_wellyes:1271624822182641764>', '"well yes", obviously'),
    (348273727298863104, '<:concerned_froge:814672652526420008>', 'concerned side-eye, "source?"'),
    (348273727298863104, '<:tannah:1164053080984268821>', 'reaction emoji the group spams'),
    (348273727298863104, '<:bruh:1456062789918523392>', 'bruh'),
    (348273727298863104, '<:oop:581275888516202505>', 'oops, caught');
