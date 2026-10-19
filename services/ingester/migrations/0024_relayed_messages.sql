-- A link fixer's repost stands for its author's message. PilzBrots (an ignored
-- author: it's a chatbot too) deletes a post whose link preview is broken and
-- reposts it as
--
--   <@author>:
--   https://fxtwitter.com/...
--
--   -# The original message sent by <@author> was deleted so the bot could fix the broken embed links for easier viewing.
--
-- so the repost, with the working preview, is the only copy left. Kiki reads it
-- as the author's message (`relayed_for`), with the text they wrote
-- (`relayed_text`): in the channel context, search and the drawings' shared
-- images. Seen in prod: "the most recent twitter link posted" was invisible to her.
--
-- Virtual, so adding them doesn't rewrite the table.
ALTER TABLE messages
    ADD COLUMN relayed_for BIGINT GENERATED ALWAYS AS (
        CASE WHEN author_bot THEN substring(content FROM
            '\n-# The original message sent by <@([0-9]+)> was deleted so the bot could fix')::bigint
        END
    ) VIRTUAL,
    ADD COLUMN relayed_text TEXT GENERATED ALWAYS AS (
        CASE WHEN author_bot AND content ~
            '\n-# The original message sent by <@[0-9]+> was deleted so the bot could fix'
        THEN btrim(regexp_replace(content,
            '^<@[0-9]+>:\s*|\s*\n-# The original message sent by <@[0-9]+> was deleted so the bot could fix[^\n]*$',
            '', 'g'))
        END
    ) VIRTUAL;
