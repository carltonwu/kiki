-- A forwarded message has no content, attachments or embeds of its own: Discord
-- puts the forwarded one's in `message_snapshots`. Everything that reads a
-- message's text and images (the prompt's image markers, read_image, drawing
-- references, search) reads the top level, so a forwarded photo was invisible:
-- in prod Kiki answered one with "i can't even peek at what you sent". Storing
-- lifts the snapshot's content, attachments and embeds to the top level where
-- the message has none; `message_snapshots` stays as Discord sent it.
CREATE FUNCTION lift_forward(m jsonb) RETURNS jsonb
LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE WHEN jsonb_typeof(s) = 'object' THEN
        m
        || CASE WHEN coalesce(m->>'content', '') = '' AND s ? 'content'
                THEN jsonb_build_object('content', s->'content') ELSE '{}' END
        || CASE WHEN coalesce(m->'attachments', '[]') = '[]' AND s ? 'attachments'
                THEN jsonb_build_object('attachments', s->'attachments') ELSE '{}' END
        || CASE WHEN coalesce(m->'embeds', '[]') = '[]' AND s ? 'embeds'
                THEN jsonb_build_object('embeds', s->'embeds') ELSE '{}' END
    ELSE m END
    FROM (SELECT m->'message_snapshots'->0->'message' AS s) snapshot
$$;

UPDATE messages SET raw = lift_forward(raw), content = coalesce(lift_forward(raw)->>'content', '')
WHERE raw ? 'message_snapshots';
