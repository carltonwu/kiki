//! Postgres for tests: each test gets its own schema with every migration
//! applied, or `None` without `TEST_DATABASE_URL` (as in the Python tests).
//!
//! pgvector must live in `public`, set up once. Migration 0007's
//! `CREATE EXTENSION IF NOT EXISTS vector` would otherwise install it into the
//! first test schema on the search path: invisible to tests running alongside,
//! dropped with that schema, and racing concurrent creators.

use std::time::{SystemTime, UNIX_EPOCH};

use sqlx::{
    Executor, PgPool,
    postgres::{PgConnectOptions, PgPoolOptions},
};

pub struct TestDb {
    admin: PgPool,
    pub db: PgPool,
    schema: String,
}

/// A fresh, migrated schema named after `prefix`.
pub async fn migrated(prefix: &str) -> Option<TestDb> {
    migrated_through(prefix, i64::MAX).await
}

/// A fresh schema with the migrations up to `version` only, to test an
/// upgrade: seed it, then `migrate` for the rest.
pub async fn migrated_through(prefix: &str, version: i64) -> Option<TestDb> {
    let url = std::env::var("TEST_DATABASE_URL").ok()?;
    let admin = PgPool::connect(&url).await.unwrap();
    ensure_vector(&admin).await;
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    let schema = format!("{prefix}_{nanos}");
    admin
        .execute(format!("CREATE SCHEMA {schema}").as_str())
        .await
        .unwrap();
    let options: PgConnectOptions = url.parse().unwrap();
    let options = options.options([("search_path", format!("{schema},public"))]);
    let db = PgPoolOptions::new().connect_with(options).await.unwrap();
    run_through(&db, version).await;
    Some(TestDb { admin, db, schema })
}

/// The rest of the migrations, after `migrated_through` and seeding.
pub async fn migrate(db: &PgPool) {
    run_through(db, i64::MAX).await;
}

async fn run_through(db: &PgPool, version: i64) {
    crate::migrate::run(db, &migrator_through(version))
        .await
        .unwrap();
}

/// The migrations up to `version`, without sqlx's lock.
pub fn migrator_through(version: i64) -> sqlx::migrate::Migrator {
    let mut migrator = sqlx::migrate::Migrator::DEFAULT;
    migrator.migrations = crate::MIGRATOR
        .iter()
        .filter(|m| m.version <= version)
        .cloned()
        .collect::<Vec<_>>()
        .into();
    // sqlx's lock is per database, so it would serialize unrelated schemas, and
    // deadlock them: a CREATE INDEX CONCURRENTLY (0031) waits out every open
    // transaction, including another test's migrator waiting for the lock it holds.
    // Each schema has its own migrations table; production has one migrator.
    migrator.set_locking(false);
    migrator
}

impl TestDb {
    pub async fn drop(self) {
        self.db.close().await;
        self.admin
            .execute(format!("DROP SCHEMA {} CASCADE", self.schema).as_str())
            .await
            .unwrap();
    }
}

/// Creates pgvector in `public`, or moves it there if an older run left it in
/// a test schema. The advisory lock serializes concurrent tests (and test
/// processes): the waiter then sees the extension and does nothing.
async fn ensure_vector(admin: &PgPool) {
    sqlx::raw_sql(
        r#"
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
        "#,
    )
    .execute(admin)
    .await
    .unwrap();
}
