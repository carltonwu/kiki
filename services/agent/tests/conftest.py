import os
from pathlib import Path
from uuid import uuid4

import pytest
from psycopg import AsyncConnection, sql
from psycopg.conninfo import make_conninfo

MIGRATIONS = Path(__file__).resolve().parents[2] / "ingester/migrations"
# pgvector in public, set up once under a lock shared with the Rust tests
# (services/ingester/src/test_db.rs): migration 0007 would otherwise install it
# into the test schema, where it's dropped with the schema.
VECTOR = """
BEGIN;
SELECT pg_advisory_xact_lock(hashtext('kiki-tests-vector-extension'));
CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;
DO $$ BEGIN
    IF (SELECT n.nspname FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace
        WHERE e.extname = 'vector') <> 'public' THEN
        ALTER EXTENSION vector SET SCHEMA public;
    END IF;
END $$;
COMMIT;
"""


@pytest.fixture
async def database_url():
    """An isolated schema with every Rust migration applied (pgvector stays in public)."""
    base = os.getenv("TEST_DATABASE_URL")
    if not base:
        pytest.skip("set TEST_DATABASE_URL for Postgres integration tests")
    schema = f"host_test_{uuid4().hex}"
    async with await AsyncConnection.connect(base, autocommit=True) as connection:
        await connection.execute(VECTOR)
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
