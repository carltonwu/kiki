-- Quote bots' posts: the server's saved quotes, and which message each one saved.
--
-- Seen in prod: asked for someone's "most fucked up quotes" from #quotes, Kiki
-- found only the `.quote 194` commands people type there. The quotes are bots'
-- posts with no `content`. UB3R-B0T's is an embed:
--
--   author.name: "#99"
--   description: "<the quote>\n- <@226…> [(Jump)](https://discordapp.com/channels/<guild>/<channel>/<message>)"
--
-- PilzBrots' (an ignored author) is a Components V2 container of text displays,
-- sometimes after a title ("## :newspaper: Quote of the Day"):
--
--   "**Quote #417**", "<the quote>", …, "Authored by <@159…> at 9/19/26, 7:01 AM. … [View Original Message](https://discord.com/channels/…)", …
--
-- Each format is parsed by its own structure: the attribution is UB3R-B0T's last
-- description line, or PilzBrots' last text display starting "(Authored )by"
-- and linking it (who: a mention, or in older posts a name or ID). A link in the quote itself is never taken for the saved message. Case
-- and spacing vary (UB3R-B0T's uwu mode writes "[(jump)]", older PilzBrots posts
-- "** Quote #25**"). Kiki reads the saved message itself (find_quotes), so the
-- text and author are the original's, not the bot's copy.
--
-- These run in an index expression on every bot message's insert: no input may
-- make them raise, or the insert (and the ingester's whole batch) would fail.
-- Anything unexpected is "not a quote post".

-- A quote post's attribution (its line naming who said it and linking the saved
-- message) and the quoted text as the bot copied it. Both NULL for anything else.
CREATE FUNCTION quote_parts(raw JSONB, OUT attribution TEXT, OUT body TEXT)
LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE
    link CONSTANT TEXT := '\[[^]]*(?:jump|view original message)[^]]*\]\(https://(?:(?:ptb|canary)\.)?discord(?:app)?\.com/channels/[0-9]+/[0-9]+/[0-9]+\)';
    embed JSONB;
    displays TEXT[];
    head INT;
    last INT;
BEGIN
    IF jsonb_typeof(raw) <> 'object' THEN
        RETURN;
    END IF;
    embed := raw->'embeds'->0;
    IF jsonb_typeof(embed) = 'object' AND (embed->'author'->>'name') ~ '^#[0-9]+$' THEN
        -- UB3R-B0T: the quote, then "- <@id> [(Jump)](link)" as the last line
        -- (older posts name them instead: "- szajee [(Jump)](link)").
        attribution := substring(embed->>'description' FROM '\n([^\n]*)$');
        IF attribution ~* ('^-\s*\S.*' || link || '\s*$') THEN
            body := substring(embed->>'description' FROM '^(.*)\n[^\n]*$');
        ELSE
            attribution := NULL;
        END IF;
        RETURN;
    END IF;
    IF jsonb_typeof(raw->'components'->0->'components') = 'array' THEN
        -- PilzBrots: header, the quote, the attribution, then extras (score, timing).
        SELECT array_agg(c->>'content' ORDER BY i) INTO displays
        FROM jsonb_array_elements(raw->'components'->0->'components') WITH ORDINALITY AS t(c, i)
        WHERE jsonb_typeof(c) = 'object' AND c->>'type' = '10'
          AND jsonb_typeof(c->'content') = 'string';
        -- The header may follow a title ("## :newspaper: Quote of the Day").
        SELECT min(n) INTO head FROM generate_subscripts(displays, 1) n
        WHERE displays[n] ~* '^\*\*\s*quote #[0-9]+\s*\*\*$';
        IF head IS NOT NULL THEN
            FOR last IN REVERSE coalesce(array_length(displays, 1), 0) .. head + 1 LOOP
                IF displays[last] ~* ('^(authored )?by \S.*' || link) THEN
                    attribution := displays[last];
                    body := array_to_string(displays[head + 1:last - 1], E'\n');
                    RETURN;
                END IF;
            END LOOP;
        END IF;
    END IF;
EXCEPTION WHEN OTHERS THEN
    -- Not QUERY_CANCELED: a cancel still cancels.
    attribution := NULL;
    body := NULL;
END;
$$;

-- A Discord ID (snowflake) as BIGINT, or NULL if it isn't one: at most 19 digits
-- and within BIGINT. Checked before the cast, so it can't overflow.
CREATE FUNCTION snowflake(digits TEXT) RETURNS BIGINT
LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
RETURN CASE WHEN digits ~ '^[0-9]{1,19}$' AND digits::NUMERIC <= 9223372036854775807
            THEN digits::BIGINT END;

-- The message a quote post saved: its attribution's link. NULL for anything else,
-- and for a link with an ID that isn't a snowflake (the whole link is malformed).
CREATE FUNCTION quoted_message_id(raw JSONB) RETURNS BIGINT
LANGUAGE sql IMMUTABLE
RETURN (SELECT CASE WHEN snowflake(ids[1]) IS NOT NULL AND snowflake(ids[2]) IS NOT NULL
                    THEN snowflake(ids[3]) END
        FROM (SELECT regexp_match((quote_parts(raw)).attribution,
                  '\]\(https://(?:(?:ptb|canary)\.)?discord(?:app)?\.com/channels/([0-9]+)/([0-9]+)/([0-9]+)\)',
                  'i') AS ids) link);

-- A guild's quote posts: about 1,400 in prod, where finding them without this
-- reads every message (2.4 s). A plain build: it blocks message writes for the
-- 2-3 s it takes to read the table at deploy (the ingester's writes wait, then
-- go on). A concurrent build would avoid that, but in its own non-transactional
-- migration it can fail half way and leave an invalid index sqlx can't recover.
CREATE INDEX messages_quote_posts_idx ON messages (guild_id, quoted_message_id(raw))
    WHERE author_bot AND quoted_message_id(raw) IS NOT NULL;
