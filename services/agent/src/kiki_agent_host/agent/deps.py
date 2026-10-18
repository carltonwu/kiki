"""What one reply's tools share."""

import asyncio
import contextlib
from collections import Counter
from dataclasses import dataclass, field

from psycopg import AsyncConnection

from .. import db
from ..chat.scope import Roster, Scope
from ..chat.search import Semantic
from ..contracts import Drawing
from ..media.vision import Vision
from ..openrouter.embeddings import Embedder


@dataclass
class MemorySession:
    """One run's memory state. Its own connection: memory transactions must not
    interleave with background indexing on the run's shared one. The lock covers
    parallel tool calls."""

    database_url: str
    connection: AsyncConnection | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def connect(self) -> AsyncConnection:
        if self.connection is None:
            self.connection = await db.connect(self.database_url)
        return self.connection

    async def close(self) -> None:
        if self.connection is not None:
            connection, self.connection = self.connection, None
            with contextlib.suppress(Exception):
                await connection.close()


@dataclass
class Deps:
    """Trusted per-run context: tool arguments can only narrow it."""

    connection: AsyncConnection
    scope: Scope
    embed: Embedder
    roster: Roster
    vision: Vision
    memory: MemorySession
    job_id: str = ""
    # The server's image model, when it has drawing on; drawings run after the reply.
    image_model: str | None = None
    # Calls made per budget (`guard.Budget.name`).
    spent: Counter[str] = field(default_factory=Counter)
    indexing: asyncio.Task[Semantic] | None = None
    drawings: list[Drawing] = field(default_factory=list)
