"""Message embeddings: computed lazily for the searched scope, or all at once by
`kiki-agent-host index`.

Pending messages are the ones in `message_embedding_queue`, which triggers keep
(migration 0027): finding them reads the queue, not every message."""

import asyncio
import logging

from psycopg import AsyncConnection

from .. import db
from ..openrouter.client import openrouter_client
from ..openrouter.embeddings import EMBED_BATCH, Embedder
from .scope import PEOPLES, TEXT

logger = logging.getLogger(__name__)


INDEX_LIMIT = 256

# What makes a message searchable, for messages aliased `m`.
SEARCHABLE = f"m.deleted_at IS NULL AND {PEOPLES} AND btrim({TEXT}) <> ''"

# Every condition is checked again: a queued row that no longer qualifies is
# skipped (and dropped by DROP_STALE), never embedded.
PENDING = f"""
SELECT m.id, {TEXT} AS content, md5(m.content) AS hash
FROM message_embedding_queue q JOIN messages m ON m.id = q.message_id
WHERE (%(guild)s::bigint IS NULL OR m.guild_id = %(guild)s)
  AND (%(below)s::bigint IS NULL OR q.message_id < %(below)s)
  AND {SEARCHABLE}
  AND NOT EXISTS (SELECT 1 FROM message_embeddings e WHERE e.message_id = m.id)
ORDER BY q.message_id DESC LIMIT %(limit)s
"""

# Rows nothing will embed: already embedded (by another run, or by a host from
# before the queue during a deploy) or no longer searchable (an author ignored
# since). The queue is small, so this reads little.
DROP_STALE = f"""
DELETE FROM message_embedding_queue q
WHERE EXISTS (SELECT 1 FROM message_embeddings e WHERE e.message_id = q.message_id)
   OR NOT EXISTS (SELECT 1 FROM messages m WHERE m.id = q.message_id AND {SEARCHABLE})
"""

# Writes only if the text is unchanged; FOR SHARE waits out a concurrent edit,
# whose trigger would otherwise miss this row. A saved message leaves the
# queue in the same statement; an edit after it re-queues it (its trigger).
SAVE = """
WITH saved AS (
    INSERT INTO message_embeddings (message_id, embedding)
    SELECT m.id, v.embedding
    FROM unnest(%s::bigint[], %s::text[], %s::halfvec[]) AS v(id, hash, embedding)
    JOIN messages m ON m.id = v.id AND md5(m.content) = v.hash AND m.deleted_at IS NULL
    FOR SHARE OF m
    ON CONFLICT (message_id) DO NOTHING
    RETURNING message_id
)
DELETE FROM message_embedding_queue q USING saved WHERE q.message_id = saved.message_id
"""

# `kiki-agent-host index` only: queues every searchable message without a
# vector. It reads every message, which is why replies don't do it.
ENQUEUE_ALL = f"""
INSERT INTO message_embedding_queue (message_id)
SELECT m.id FROM messages m
WHERE {SEARCHABLE}
  AND NOT EXISTS (SELECT 1 FROM message_embeddings e WHERE e.message_id = m.id)
ON CONFLICT (message_id) DO NOTHING
"""


async def index_pending(
    connection: AsyncConnection,
    embed: Embedder,
    *,
    guild_id: int | None = None,
    below: int | None = None,
    limit: int = INDEX_LIMIT,
) -> tuple[int, int | None]:
    """Embeds up to `limit` pending messages, newest first.

    Returns how many were found and, if that filled the limit, the lowest ID
    seen: more may be pending below it.
    """
    if below is None:
        await connection.execute(DROP_STALE)
    cursor = await connection.execute(PENDING, {"guild": guild_id, "below": below, "limit": limit})
    rows = await cursor.fetchall()
    batches = [rows[i : i + EMBED_BATCH] for i in range(0, len(rows), EMBED_BATCH)]
    vectors = await asyncio.gather(*(embed([r[1] for r in batch]) for batch in batches))
    for batch, embeddings in zip(batches, vectors, strict=True):
        await connection.execute(SAVE, ([r[0] for r in batch], [r[2] for r in batch], embeddings))
    return len(rows), (rows[-1][0] if len(rows) == limit else None)


async def index_all(database_url: str, api_key: str) -> None:
    """`kiki-agent-host index`: queues and embeds every pending message, then exits."""
    embed, below, total = Embedder(openrouter_client(api_key)), None, 0
    async with await db.connect(database_url) as connection:
        cursor = await connection.execute(ENQUEUE_ALL)
        logger.info("queued %d messages missing from the queue", cursor.rowcount)
        while True:
            found, below = await index_pending(connection, embed, below=below)
            total += found
            logger.info("indexed %d messages, cost $%.6f", total, embed.cost)
            if below is None:
                return
