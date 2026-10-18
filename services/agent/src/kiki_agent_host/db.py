"""Database connections, opened only while a job needs one."""

from psycopg import AsyncConnection


async def connect(database_url: str) -> AsyncConnection:
    return await AsyncConnection.connect(database_url, autocommit=True, connect_timeout=10)
