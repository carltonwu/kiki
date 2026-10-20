"""Database connections, opened only while a job needs one."""

import math

from psycopg import AsyncConnection


async def connect(database_url: str, *, timeout: float = 10) -> AsyncConnection:
    # libpq takes whole seconds; callers with a deadline also bound the wait.
    return await AsyncConnection.connect(
        database_url, autocommit=True, connect_timeout=max(1, math.ceil(timeout))
    )
