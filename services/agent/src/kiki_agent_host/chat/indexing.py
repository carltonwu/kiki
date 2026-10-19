"""Message embeddings: computed lazily for the searched scope, or all at once by
`kiki-agent-host index`."""

import asyncio
import logging

from psycopg import AsyncConnection

from .. import db
from ..openrouter.client import openrouter_client
from ..openrouter.embeddings import EMBED_BATCH, Embedder
from .scope import PEOPLES, TEXT

logger = logging.getLogger(__name__)


INDEX_LIMIT = 256


PENDING = f"""
SELECT m.id, {TEXT} AS content, md5(m.content) AS hash FROM messages m
WHERE (%(guild)s::bigint IS NULL OR m.guild_id = %(guild)s)
  AND (%(below)s::bigint IS NULL OR m.id < %(below)s)
  AND m.deleted_at IS NULL AND {PEOPLES} AND btrim({TEXT}) <> ''
  AND NOT EXISTS (SELECT 1 FROM message_embeddings e WHERE e.message_id = m.id)
ORDER BY m.id DESC LIMIT %(limit)s
"""


# Writes only if the text is unchanged; FOR SHARE waits out a concurrent edit,
# whose trigger would otherwise miss this row.
SAVE = """
INSERT INTO message_embeddings (message_id, embedding)
SELECT m.id, v.embedding
FROM unnest(%s::bigint[], %s::text[], %s::halfvec[]) AS v(id, hash, embedding)
JOIN messages m ON m.id = v.id AND md5(m.content) = v.hash AND m.deleted_at IS NULL
FOR SHARE OF m
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
    cursor = await connection.execute(PENDING, {"guild": guild_id, "below": below, "limit": limit})
    rows = await cursor.fetchall()
    batches = [rows[i : i + EMBED_BATCH] for i in range(0, len(rows), EMBED_BATCH)]
    vectors = await asyncio.gather(*(embed([r[1] for r in batch]) for batch in batches))
    for batch, embeddings in zip(batches, vectors, strict=True):
        await connection.execute(SAVE, ([r[0] for r in batch], [r[2] for r in batch], embeddings))
    return len(rows), (rows[-1][0] if len(rows) == limit else None)


async def index_all(database_url: str, api_key: str) -> None:
    """`kiki-agent-host index`: embeds every pending message, then exits."""
    embed, below, total = Embedder(openrouter_client(api_key)), None, 0
    async with await db.connect(database_url) as connection:
        while True:
            found, below = await index_pending(connection, embed, below=below)
            total += found
            logger.info("indexed %d messages, cost $%.6f", total, embed.cost)
            if below is None:
                return
