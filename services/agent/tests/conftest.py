import os
from pathlib import Path
from uuid import uuid4

import pytest
from psycopg import AsyncConnection, sql
from psycopg.conninfo import make_conninfo

MIGRATIONS = Path(__file__).resolve().parents[2] / "ingester/migrations"


@pytest.fixture
async def database_url():
    """An isolated schema with every Rust migration applied (pgvector stays in public)."""
    base = os.getenv("TEST_DATABASE_URL")
    if not base:
        pytest.skip("set TEST_DATABASE_URL for Postgres integration tests")
    schema = f"host_test_{uuid4().hex}"
    async with await AsyncConnection.connect(base, autocommit=True) as connection:
        await connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        scoped = make_conninfo(base, options=f"-csearch_path={schema},public")
        try:
            async with await AsyncConnection.connect(scoped, autocommit=True) as setup:
                for migration in sorted(MIGRATIONS.glob("*.sql")):
                    await setup.execute(migration.read_text())
            yield scoped
        finally:
            await connection.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
            )
