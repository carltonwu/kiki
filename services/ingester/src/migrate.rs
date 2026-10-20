//! `ingester migrate`: the migrations, after clearing what a failed concurrent
//! index build left behind.
//!
//! A `-- no-transaction` migration that runs `CREATE INDEX CONCURRENTLY` (0031)
//! and fails or is cancelled leaves the index INVALID, and sqlx doesn't record
//! the migration, so the next deploy reruns it and fails on "already exists".
//! It can't heal itself: sqlx sends the file as one query string, which Postgres
//! runs as an implicit transaction block, and `DROP INDEX CONCURRENTLY` can't run
//! in one. So before migrating, an INVALID index that a pending migration builds
//! concurrently is dropped (concurrently, outside any transaction).

use anyhow::Context;
use sqlx::{PgPool, migrate::Migrator};
use tracing::warn;

pub async fn run(pool: &PgPool, migrator: &Migrator) -> anyhow::Result<()> {
    let applied: Vec<i64> = if table_exists(pool, "_sqlx_migrations").await? {
        sqlx::query_scalar("SELECT version FROM _sqlx_migrations WHERE success")
            .fetch_all(pool)
            .await?
    } else {
        Vec::new()
    };
    let pending = migrator
        .iter()
        .filter(|m| m.no_tx && !applied.contains(&m.version));
    for migration in pending {
        for index in concurrent_indexes(&migration.sql) {
            let invalid: Option<bool> = sqlx::query_scalar(
                "SELECT NOT indisvalid FROM pg_index WHERE indexrelid = to_regclass($1)",
            )
            .bind(&index)
            .fetch_optional(pool)
            .await?;
            if invalid == Some(true) {
                warn!(
                    index,
                    version = migration.version,
                    "dropping an invalid index a failed concurrent build left"
                );
                // `index` is a bare identifier (concurrent_indexes checks).
                sqlx::raw_sql(&format!("DROP INDEX CONCURRENTLY IF EXISTS {index}"))
                    .execute(pool)
                    .await
                    .with_context(|| format!("dropping invalid index {index}"))?;
            }
        }
    }
    migrator.run(pool).await.context("running migrations")
}

async fn table_exists(pool: &PgPool, name: &str) -> sqlx::Result<bool> {
    sqlx::query_scalar("SELECT to_regclass($1) IS NOT NULL")
        .bind(name)
        .fetch_one(pool)
        .await
}

/// Indexes the SQL builds with `CREATE [UNIQUE] INDEX CONCURRENTLY [IF NOT
/// EXISTS] name`, when the name is a bare lowercase identifier.
fn concurrent_indexes(sql: &str) -> Vec<String> {
    let words: Vec<String> = sql
        .lines()
        .map(|line| line.split("--").next().unwrap_or(""))
        .flat_map(str::split_whitespace)
        .map(str::to_ascii_lowercase)
        .collect();
    let mut found = Vec::new();
    for (i, word) in words.iter().enumerate() {
        if word != "concurrently" || i < 2 {
            continue;
        }
        let create = words[i - 1] == "index"
            && (words[i - 2] == "create"
                || (words[i - 2] == "unique" && i >= 3 && words[i - 3] == "create"));
        if !create {
            continue;
        }
        let mut rest = words[i + 1..].iter();
        let mut name = rest.next();
        if name.map(String::as_str) == Some("if") {
            name = rest.nth(2); // if not exists <name>
        }
        if let Some(name) = name
            && !name.is_empty()
            && name
                .chars()
                .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '_')
        {
            found.push(name.clone());
        }
    }
    found
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn finds_concurrently_built_indexes() {
        let sql = "-- no-transaction\n-- CREATE INDEX CONCURRENTLY not_this ON t (a)\n\
                   CREATE INDEX CONCURRENTLY messages_quote_posts_idx ON messages (guild_id)\n\
                   create unique index concurrently if not exists u_idx on t (a);\n\
                   CREATE INDEX plain_idx ON t (a); CREATE INDEX CONCURRENTLY \"Quoted\" ON t (a)";
        assert_eq!(
            concurrent_indexes(sql),
            ["messages_quote_posts_idx", "u_idx"]
        );
    }

    #[tokio::test]
    async fn a_rerun_heals_an_invalid_index_a_failed_build_left() {
        // Migrated through 0030; the quote index (0031) failed half way, as a
        // cancelled or failed CREATE INDEX CONCURRENTLY does, leaving it INVALID.
        let Some(test) = crate::test_db::migrated_through("heal_index_test", 30).await else {
            return;
        };
        let db = test.db.clone();
        sqlx::raw_sql(
            "INSERT INTO messages (id, guild_id, channel_id, author_id, author_name, author_bot,
                                   content, created_at, raw)
             VALUES (1, 9, 2, 3, 'a', false, 'x', now(), '{}'),
                    (2, 9, 2, 3, 'a', false, 'y', now(), '{}')",
        )
        .execute(&db)
        .await
        .unwrap();
        let failed = sqlx::raw_sql(
            "CREATE UNIQUE INDEX CONCURRENTLY messages_quote_posts_idx ON messages (guild_id)",
        )
        .execute(&db)
        .await;
        assert!(failed.is_err(), "duplicate guild_ids fail the unique build");
        let valid = "SELECT indisvalid FROM pg_index \
                     WHERE indexrelid = to_regclass('messages_quote_posts_idx')";
        let left: bool = sqlx::query_scalar(valid).fetch_one(&db).await.unwrap();
        assert!(!left, "the failed build leaves an invalid index");

        // Without the repair, the rerun fails on the leftover.
        let plain = crate::test_db::migrator_through(i64::MAX);
        let error = plain.run(&db).await.unwrap_err().to_string();
        assert!(error.contains("already exists"), "{error}");

        run(&db, &plain).await.unwrap();
        let healed: bool = sqlx::query_scalar(valid).fetch_one(&db).await.unwrap();
        assert!(healed);
        let definition: String =
            sqlx::query_scalar("SELECT pg_get_indexdef(to_regclass('messages_quote_posts_idx'))")
                .fetch_one(&db)
                .await
                .unwrap();
        assert!(
            definition.contains("quoted_message_id(raw)"),
            "{definition}"
        );
        // A valid index is left alone on the next run.
        run(&db, &plain).await.unwrap();
        test.drop().await;
    }
}
