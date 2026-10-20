//! The poller's Postgres queries: server statistics, and the queues and
//! backlogs Kiki keeps in her own tables.
//!
//! Each runs in a transaction with a short `statement_timeout`, so the
//! server cancels a slow one, and a `lock_timeout`, so a size lookup never waits
//! behind a migration's lock.

use sqlx::{FromRow, PgPool, Postgres, Transaction};

/// Tables whose size and dead tuples are reported: a fixed allowlist, so the
/// `table` attribute stays bounded.
pub const TABLES: [&str; 7] = [
    "messages",
    "message_embeddings",
    "agent_host_images",
    "agent_runs",
    "conversations",
    "classifications",
    "agent_host_jobs",
];

/// `pg_stat_activity` states as the `state` attribute. Anything else (a NULL
/// state, `disabled`, `fastpath function call`) counts as `other`.
pub const STATES: [&str; 5] = [
    "active",
    "idle",
    "idle_in_transaction",
    "idle_in_transaction_aborted",
    "other",
];

/// A run still `running` or `delivering` this long after it started is stuck.
/// A run's host requests give up after `AGENT_HOST_TIMEOUT_MS` (180 s by
/// default, wake-up retries included) and delivery takes seconds, so 10 minutes
/// is well past anything healthy.
pub const STUCK_AFTER_SECS: i64 = 600;

/// The embedding backlog counts messages from this many days back. Indexing
/// works newest first (each reply embeds its guild's 256 newest pending
/// messages), so this is where a backlog builds up; history is filled in by
/// `kiki-agent-host index`. The window keeps the count a short primary-key
/// range scan instead of reading the whole `messages` table every minute.
pub const BACKLOG_DAYS: i64 = 7;

#[derive(Debug, Clone, Default, PartialEq)]
pub struct Server {
    /// Client connections per [`STATES`] entry, in that order.
    pub connections: [u64; STATES.len()],
    pub oldest_transaction_secs: f64,
    pub database_bytes: u64,
    pub commits: u64,
    pub rollbacks: u64,
    pub blocks_hit: u64,
    pub blocks_read: u64,
    pub deadlocks: u64,
    pub temp_bytes: u64,
    /// (table, total size in bytes, dead tuples), for the [`TABLES`] that exist.
    pub tables: Vec<(&'static str, u64, u64)>,
}

#[derive(Debug, Clone, Default, PartialEq, FromRow)]
pub struct Queues {
    pub runs_pending: i64,
    pub runs_oldest_secs: f64,
    pub drawings_pending: i64,
    pub drawings_oldest_secs: f64,
    pub runs_stuck: i64,
    pub conversations_open: i64,
    pub embeddings_backlog: i64,
}

async fn begin(db: &PgPool) -> sqlx::Result<Transaction<'static, Postgres>> {
    let mut tx = db.begin().await?;
    // Local to the transaction, so the pooled connection keeps its defaults.
    sqlx::query(
        "SELECT set_config('statement_timeout', '3s', true), \
                set_config('lock_timeout', '1s', true)",
    )
    .execute(&mut *tx)
    .await?;
    Ok(tx)
}

pub async fn server(db: &PgPool) -> sqlx::Result<Server> {
    #[derive(FromRow)]
    struct Activity {
        state: String,
        n: i64,
    }
    #[derive(FromRow)]
    struct Database {
        oldest_transaction_secs: f64,
        database_bytes: i64,
        commits: i64,
        rollbacks: i64,
        blocks_hit: i64,
        blocks_read: i64,
        deadlocks: i64,
        temp_bytes: i64,
    }
    #[derive(FromRow)]
    struct Table {
        name: String,
        bytes: i64,
        dead: i64,
    }
    let mut tx = begin(db).await?;
    // Client connections across the server: max_connections is per server.
    // Our own connection, mid-poll, isn't counted.
    let activity: Vec<Activity> = sqlx::query_as(
        r#"
        SELECT CASE state WHEN 'active' THEN 'active'
                          WHEN 'idle' THEN 'idle'
                          WHEN 'idle in transaction' THEN 'idle_in_transaction'
                          WHEN 'idle in transaction (aborted)' THEN 'idle_in_transaction_aborted'
                          ELSE 'other' END AS state,
               count(*) AS n
        FROM pg_stat_activity
        WHERE backend_type = 'client backend' AND pid <> pg_backend_pid()
        GROUP BY 1
        "#,
    )
    .fetch_all(&mut *tx)
    .await?;
    let database: Database = sqlx::query_as(
        r#"
        SELECT
            coalesce((SELECT extract(epoch FROM max(now() - xact_start))
                      FROM pg_stat_activity
                      WHERE backend_type = 'client backend' AND pid <> pg_backend_pid()
                        AND xact_start IS NOT NULL), 0)::float8 AS oldest_transaction_secs,
            pg_database_size(current_database()) AS database_bytes,
            xact_commit AS commits,
            xact_rollback AS rollbacks,
            blks_hit AS blocks_hit,
            blks_read AS blocks_read,
            deadlocks::bigint AS deadlocks,
            temp_bytes::bigint AS temp_bytes
        FROM pg_stat_database WHERE datname = current_database()
        "#,
    )
    .fetch_one(&mut *tx)
    .await?;
    // to_regclass resolves through the search path, as the app's queries do.
    let tables: Vec<Table> = sqlx::query_as(
        r#"
        SELECT t.name, pg_total_relation_size(c.oid) AS bytes,
               coalesce(s.n_dead_tup, 0) AS dead
        FROM unnest($1::text[]) AS t(name)
        CROSS JOIN LATERAL (SELECT to_regclass(t.name) AS oid) c
        LEFT JOIN pg_stat_user_tables s ON s.relid = c.oid
        WHERE c.oid IS NOT NULL
        "#,
    )
    .bind(TABLES)
    .fetch_all(&mut *tx)
    .await?;
    tx.commit().await?;

    let mut connections = [0; STATES.len()];
    for row in activity {
        let i = STATES
            .iter()
            .position(|s| *s == row.state)
            .unwrap_or(STATES.len() - 1);
        connections[i] += row.n.max(0) as u64;
    }
    let count = |n: i64| n.max(0) as u64;
    Ok(Server {
        connections,
        oldest_transaction_secs: database.oldest_transaction_secs.max(0.0),
        database_bytes: count(database.database_bytes),
        commits: count(database.commits),
        rollbacks: count(database.rollbacks),
        blocks_hit: count(database.blocks_hit),
        blocks_read: count(database.blocks_read),
        deadlocks: count(database.deadlocks),
        temp_bytes: count(database.temp_bytes),
        tables: tables
            .into_iter()
            .filter_map(|t| {
                let name = TABLES.into_iter().find(|name| *name == t.name)?;
                Some((name, count(t.bytes), count(t.dead)))
            })
            .collect(),
    })
}

pub async fn queues(db: &PgPool) -> sqlx::Result<Queues> {
    let mut tx = begin(db).await?;
    // Every count reads a partial index or a short range, never a whole table,
    // except `stuck`, which scans agent_runs' heap (narrow rows: the request
    // snapshot is TOASTed).
    //
    // The backlog matches the host's pending query (chat/indexing.py and
    // chat/scope.py: people's undeleted, non-empty messages, a link fixer's
    // reposts included, ignored authors' excluded) over messages whose
    // snowflake ID is newer than BACKLOG_DAYS: ID = (ms since 2015 << 22).
    let queues: Queues = sqlx::query_as(
        r#"
        SELECT
            (SELECT count(*) FROM agent_runs WHERE status = 'pending') AS runs_pending,
            coalesce((SELECT extract(epoch FROM now() - min(created_at))
                      FROM agent_runs WHERE status = 'pending'), 0)::float8 AS runs_oldest_secs,
            (SELECT count(*) FROM agent_image_jobs WHERE status = 'pending') AS drawings_pending,
            coalesce((SELECT extract(epoch FROM now() - min(created_at))
                      FROM agent_image_jobs WHERE status = 'pending'), 0)::float8
                AS drawings_oldest_secs,
            (SELECT count(*) FROM agent_runs
             WHERE status IN ('running', 'delivering')
               AND started_at < now() - make_interval(secs => $1)) AS runs_stuck,
            (SELECT count(*) FROM conversations WHERE closed_at IS NULL) AS conversations_open,
            (SELECT count(*) FROM messages m
             WHERE m.id >= ((extract(epoch FROM now() - make_interval(days => $2::int)) * 1000)::bigint
                            - 1420070400000) << 22
               AND m.deleted_at IS NULL
               AND (NOT m.author_bot OR m.relayed_for IS NOT NULL)
               AND (m.author_id <> ALL (ARRAY(SELECT user_id FROM ignored_authors))
                    OR m.relayed_for IS NOT NULL)
               AND btrim(coalesce(m.relayed_text, m.content)) <> ''
               AND NOT EXISTS (SELECT 1 FROM message_embeddings e WHERE e.message_id = m.id))
                AS embeddings_backlog
        "#,
    )
    .bind(STUCK_AFTER_SECS as f64)
    .bind(BACKLOG_DAYS)
    .fetch_one(&mut *tx)
    .await?;
    tx.commit().await?;
    Ok(Queues {
        runs_oldest_secs: queues.runs_oldest_secs.max(0.0),
        drawings_oldest_secs: queues.drawings_oldest_secs.max(0.0),
        ..queues
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Discord's snowflake for a Unix time in ms, as the backlog query computes it.
    fn snowflake(unix_ms: i64) -> i64 {
        (unix_ms - 1_420_070_400_000) << 22
    }

    #[tokio::test]
    async fn server_stats_cover_the_allowlist() {
        let Some(test) = crate::test_db::migrated("platform_server_test").await else {
            return;
        };
        let stats = server(&test.db).await.unwrap();
        let names: Vec<_> = stats.tables.iter().map(|t| t.0).collect();
        assert_eq!(names.len(), TABLES.len(), "{names:?}");
        for table in TABLES {
            assert!(names.contains(&table), "{table} missing");
        }
        // Every table has at least its primary key index.
        assert!(stats.tables.iter().all(|t| t.1 > 0));
        assert!(stats.database_bytes > 0);
        assert!(stats.commits > 0);
        // The test pool's other connections, at least the admin one.
        assert!(stats.connections.iter().sum::<u64>() >= 1);
        assert!(stats.oldest_transaction_secs >= 0.0);
        test.drop().await;
    }

    #[tokio::test]
    async fn queues_count_pending_stuck_open_and_unembedded() {
        let Some(test) = crate::test_db::migrated("platform_queues_test").await else {
            return;
        };
        let db = test.db.clone();
        assert_eq!(queues(&db).await.unwrap(), Queues::default());

        let now_ms = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_millis() as i64;
        let recent = snowflake(now_ms - 60_000);
        let old = snowflake(now_ms - (BACKLOG_DAYS + 1) * 86_400_000);
        sqlx::raw_sql(&format!(
            r#"
            INSERT INTO conversations (id, guild_id, channel_id, started_by, closed_at)
            VALUES (1, 1, 1, 1, NULL), (2, 1, 1, 2, now());
            INSERT INTO agent_runs (id, conversation_id, trigger_message_ids, status, created_at, started_at)
            VALUES (1, 1, '{{}}', 'pending', now() - interval '90 seconds', NULL),
                   (2, 1, '{{}}', 'pending', now() - interval '10 seconds', NULL),
                   (3, 1, '{{}}', 'running', now() - interval '1 hour', now() - interval '20 minutes'),
                   (4, 1, '{{}}', 'delivering', now() - interval '1 hour', now() - interval '15 minutes'),
                   (5, 1, '{{}}', 'running', now(), now() - interval '1 minute'),
                   (6, 1, '{{}}', 'done', now() - interval '1 hour', now() - interval '1 hour');
            INSERT INTO agent_image_jobs (run_id, position, reply_to_id, request, status, created_at)
            VALUES (6, 0, 1, '{{}}', 'pending', now() - interval '30 seconds'),
                   (6, 1, 1, '{{}}', 'done', now() - interval '1 hour');
            INSERT INTO ignored_authors (user_id) VALUES (99);
            INSERT INTO messages (id, guild_id, channel_id, author_id, author_bot, content, created_at, deleted_at, raw)
            VALUES ({recent} + 1, 1, 1, 1, false, 'pending', now(), NULL, '{{}}'),
                   ({recent} + 2, 1, 1, 1, false, 'embedded', now(), NULL, '{{}}'),
                   ({recent} + 3, 1, 1, 1, false, '  ', now(), NULL, '{{}}'),
                   ({recent} + 4, 1, 1, 1, false, 'deleted', now(), now(), '{{}}'),
                   ({recent} + 5, 1, 1, 2, true, 'a bot', now(), NULL, '{{}}'),
                   ({recent} + 6, 1, 1, 99, false, 'ignored', now(), NULL, '{{}}'),
                   ({recent} + 7, 1, 1, 99, true,
                    E'<@5>:\nhttps://fxtwitter.com/x\n\n-# The original message sent by <@5> was deleted so the bot could fix the broken embed links for easier viewing.',
                    now(), NULL, '{{}}'),
                   ({old}, 1, 1, 1, false, 'old and pending', now(), NULL, '{{}}');
            INSERT INTO message_embeddings (message_id, embedding)
            VALUES ({recent} + 2, array_fill(0.1::real, ARRAY[1024])::halfvec);
            "#
        ))
        .execute(&db)
        .await
        .unwrap();

        let q = queues(&db).await.unwrap();
        assert_eq!(q.runs_pending, 2);
        assert!((85.0..120.0).contains(&q.runs_oldest_secs), "{q:?}");
        assert_eq!(q.drawings_pending, 1);
        assert!((25.0..60.0).contains(&q.drawings_oldest_secs), "{q:?}");
        assert_eq!(q.runs_stuck, 2);
        assert_eq!(q.conversations_open, 1);
        // The plain pending message and the link fixer's repost.
        assert_eq!(q.embeddings_backlog, 2);
        test.drop().await;
    }
}
