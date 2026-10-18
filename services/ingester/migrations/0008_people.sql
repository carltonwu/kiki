-- People: every name Discord shows for a user, keyed by user ID (docs/identity-hld.md).
-- Filled from stored messages by trigger, for authors and mentioned users alike.

-- For exact comparison only: "Zoë 🎭" → "zoe". ICU pins the character classes;
-- under a C locale, [:alnum:] would drop every non-ASCII letter.
CREATE FUNCTION name_norm(name TEXT) RETURNS TEXT
LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE AS $$
    SELECT lower(regexp_replace(normalize(name, NFKD) COLLATE "und-x-icu",
                                '[^[:alnum:]]', '', 'g') COLLATE "und-x-icu")
$$;

-- Each user in a message object: the author and every mention. A null name is
-- only news if Discord sent the field: `has_global_name`, and `has_member`
-- (Gateway only) for `nick`. A missing field says nothing.
CREATE FUNCTION message_people(raw JSONB)
RETURNS TABLE (user_id BIGINT, username TEXT, global_name TEXT, nick TEXT,
               has_global_name BOOLEAN, has_member BOOLEAN, joined_at TIMESTAMPTZ, bot BOOLEAN)
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT (u->>'id')::bigint, nullif(btrim(u->>'username'), ''),
           nullif(btrim(u->>'global_name'), ''), nullif(btrim(member->>'nick'), ''),
           u ? 'global_name', jsonb_typeof(member) = 'object', (member->>'joined_at')::timestamptz,
           coalesce((u->>'bot')::boolean, false)
    FROM (SELECT raw->'author' AS u, raw->'member' AS member
          UNION ALL
          SELECT x, x->'member' FROM jsonb_array_elements(
              CASE WHEN jsonb_typeof(raw->'mentions') = 'array' THEN raw->'mentions' ELSE '[]' END) x) p
    WHERE u->>'id' ~ '^[0-9]+$'
$$;

-- Current names, as of the newest message that showed them.
CREATE TABLE people (
    guild_id     BIGINT NOT NULL,
    user_id      BIGINT NOT NULL,
    username     TEXT,
    global_name  TEXT,                 -- display name
    nick         TEXT,                 -- server nickname
    bot          BOOLEAN NOT NULL DEFAULT false,
    joined_at    TIMESTAMPTZ,          -- joined the guild, when a Gateway event said so
    seen_at      TIMESTAMPTZ NOT NULL, -- message that set username/global_name
    nick_seen_at TIMESTAMPTZ,          -- message that set nick (only Gateway ones carry it)
    PRIMARY KEY (guild_id, user_id)
);

-- Every name ever seen, so old names still resolve to the person.
CREATE TABLE user_names (
    guild_id   BIGINT NOT NULL,
    user_id    BIGINT NOT NULL,
    kind       TEXT NOT NULL CHECK (kind IN ('username', 'global_name', 'nick')),
    name       TEXT NOT NULL,
    name_norm  TEXT NOT NULL,
    first_seen TIMESTAMPTZ NOT NULL,
    last_seen  TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (guild_id, user_id, kind, name)
);

CREATE INDEX user_names_norm_idx ON user_names (guild_id, name_norm);

CREATE FUNCTION record_people() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    -- Out-of-order arrivals (backfill, catch-up) don't overwrite newer names.
    INSERT INTO people AS p (guild_id, user_id, username, global_name, nick, bot, joined_at,
                             seen_at, nick_seen_at)
    SELECT DISTINCT ON (x.user_id) NEW.guild_id, x.user_id,
           coalesce(x.username, known.username),
           CASE WHEN x.has_global_name THEN x.global_name ELSE known.global_name END,
           x.nick, x.bot, x.joined_at, NEW.created_at, CASE WHEN x.has_member THEN NEW.created_at END
    FROM message_people(NEW.raw) x
    LEFT JOIN people known ON known.guild_id = NEW.guild_id AND known.user_id = x.user_id
    ORDER BY x.user_id, x.has_member DESC, x.has_global_name DESC
    ON CONFLICT (guild_id, user_id) DO UPDATE SET
        username    = CASE WHEN EXCLUDED.seen_at >= p.seen_at THEN EXCLUDED.username ELSE p.username END,
        global_name = CASE WHEN EXCLUDED.seen_at >= p.seen_at THEN EXCLUDED.global_name ELSE p.global_name END,
        bot         = EXCLUDED.bot OR p.bot,
        seen_at     = greatest(p.seen_at, EXCLUDED.seen_at),
        nick        = CASE WHEN EXCLUDED.nick_seen_at >= coalesce(p.nick_seen_at, '-infinity')
                           THEN EXCLUDED.nick ELSE p.nick END,
        nick_seen_at = greatest(p.nick_seen_at, EXCLUDED.nick_seen_at),
        joined_at   = coalesce(EXCLUDED.joined_at, p.joined_at);

    INSERT INTO user_names AS n (guild_id, user_id, kind, name, name_norm, first_seen, last_seen)
    SELECT DISTINCT NEW.guild_id, x.user_id, k.kind, k.name, name_norm(k.name),
           NEW.created_at, NEW.created_at
    FROM message_people(NEW.raw) x,
         LATERAL (VALUES ('username', x.username), ('global_name', x.global_name),
                         ('nick', x.nick)) k(kind, name)
    WHERE k.name IS NOT NULL
    ON CONFLICT (guild_id, user_id, kind, name) DO UPDATE SET
        first_seen = least(n.first_seen, EXCLUDED.first_seen),
        last_seen  = greatest(n.last_seen, EXCLUDED.last_seen);
    RETURN NULL;
END
$$;

CREATE TRIGGER messages_record_people
    AFTER INSERT ON messages
    FOR EACH ROW EXECUTE FUNCTION record_people();

-- Catch-up re-upserts pages it already stored; only real changes need a look.
CREATE TRIGGER messages_record_people_on_update
    AFTER UPDATE OF raw ON messages
    FOR EACH ROW WHEN (OLD.raw IS DISTINCT FROM NEW.raw)
    EXECUTE FUNCTION record_people();

-- Backfill from what's already stored.
INSERT INTO people (guild_id, user_id, username, bot, seen_at)
SELECT DISTINCT ON (m.guild_id, x.user_id) m.guild_id, x.user_id, x.username, x.bot, m.created_at
FROM messages m, message_people(m.raw) x
ORDER BY m.guild_id, x.user_id, (x.username IS NOT NULL) DESC, m.created_at DESC;

UPDATE people p SET global_name = s.global_name
FROM (SELECT DISTINCT ON (m.guild_id, x.user_id) m.guild_id, x.user_id, x.global_name
      FROM messages m, message_people(m.raw) x
      WHERE x.has_global_name
      ORDER BY m.guild_id, x.user_id, m.created_at DESC) s
WHERE p.guild_id = s.guild_id AND p.user_id = s.user_id;

UPDATE people p SET nick = s.nick, nick_seen_at = s.created_at, joined_at = s.joined_at
FROM (SELECT DISTINCT ON (m.guild_id, x.user_id) m.guild_id, x.user_id, x.nick, x.joined_at,
             m.created_at
      FROM messages m, message_people(m.raw) x
      WHERE x.has_member
      ORDER BY m.guild_id, x.user_id, m.created_at DESC) s
WHERE p.guild_id = s.guild_id AND p.user_id = s.user_id;

INSERT INTO user_names (guild_id, user_id, kind, name, name_norm, first_seen, last_seen)
SELECT m.guild_id, x.user_id, k.kind, k.name, name_norm(k.name), min(m.created_at), max(m.created_at)
FROM messages m, message_people(m.raw) x,
     LATERAL (VALUES ('username', x.username), ('global_name', x.global_name),
                     ('nick', x.nick)) k(kind, name)
WHERE k.name IS NOT NULL
GROUP BY m.guild_id, x.user_id, k.kind, k.name;
