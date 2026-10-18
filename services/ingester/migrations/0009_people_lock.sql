-- Serialize record_people() across transactions.
--
-- Concurrent catch-up pages each upsert many `people` rows, in message order. Two
-- pages sharing users lock those rows in different orders and deadlock, failing the
-- whole page. A transaction-scoped advisory lock taken before touching `people`
-- gives every writer the same order. Row triggers run at the end of the INSERT,
-- after its `messages` rows are written, so the hold is brief: the rest of the
-- trigger plus the commit.
CREATE OR REPLACE FUNCTION record_people() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    -- "kikipeop"; distinct from the Rust-side leader and pool locks.
    PERFORM pg_advisory_xact_lock(0x6b69_6b69_7065_6f70);

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
