//! Kiki's conversations: the active pool and each conversation's metadata.
//!
//! The pool is every open conversation (`closed_at IS NULL`). It holds at most
//! `MAX_ACTIVE_CONVERSATIONS`: when the router opens or revives one past that,
//! the least recently active are closed as `evicted` (see [`evict_beyond`]).

use serde::Deserialize;
use sqlx::{FromRow, PgExecutor};

#[derive(FromRow)]
pub struct Conversation {
    pub id: i64,
    pub guild_id: i64,
    pub channel_id: i64,
    pub channel_name: Option<String>,
    pub started_by: i64,
    pub topic: Option<String>,
    pub topic_through_message_id: Option<i64>,
    /// Tagged messages, Kiki's included.
    pub message_count: i32,
    pub created_at_ms: i64,
    pub last_active_at_ms: i64,
    /// USD across chat runs, Decide and topic summaries.
    pub cost_usd: f64,
    /// Everyone but Kiki, most messages first.
    #[sqlx(json)]
    pub participants: Vec<Participant>,
}

#[derive(Deserialize)]
pub struct Participant {
    pub id: i64,
    pub name: Option<String>,
    pub messages: i64,
}

const SELECT: &str = r#"
    SELECT c.id, c.guild_id, c.channel_id, ch.name AS channel_name, c.started_by, c.topic,
           c.topic_through_message_id, c.message_count,
           (extract(epoch FROM c.created_at) * 1000)::bigint AS created_at_ms,
           (extract(epoch FROM c.last_active_at) * 1000)::bigint AS last_active_at_ms,
           c.topic_cost_usd
               + coalesce((SELECT sum(r.cost_usd) FROM agent_runs r WHERE r.conversation_id = c.id), 0)
               -- Decide cost is stored on every message of a pool; count it once.
               + coalesce((SELECT sum(cl.jev_cost_usd) FROM classifications cl
                           WHERE cl.conversation_id = c.id AND cl.message_id = cl.pool_id), 0)
                   AS cost_usd,
           coalesce((
               SELECT jsonb_agg(jsonb_build_object('id', p.author_id, 'name', p.name, 'messages', p.n)
                                ORDER BY p.n DESC, p.author_id)
               FROM (SELECT m.author_id, (array_agg(m.author_name ORDER BY m.id DESC))[1] AS name,
                            count(*) AS n
                     FROM conversation_messages cm JOIN messages m ON m.id = cm.message_id
                     WHERE cm.conversation_id = c.id AND m.author_id <> $1 AND m.deleted_at IS NULL
                     GROUP BY m.author_id) p
           ), '[]') AS participants
    FROM conversations c
    LEFT JOIN channels ch ON ch.id = c.channel_id
"#;

pub async fn active(db: impl PgExecutor<'_>, bot_user_id: i64) -> sqlx::Result<Vec<Conversation>> {
    sqlx::query_as(&format!(
        "{SELECT} WHERE c.closed_at IS NULL ORDER BY c.last_active_at DESC"
    ))
    .bind(bot_user_id)
    .fetch_all(db)
    .await
}

pub async fn get(
    db: impl PgExecutor<'_>,
    bot_user_id: i64,
    id: i64,
) -> sqlx::Result<Option<Conversation>> {
    sqlx::query_as(&format!("{SELECT} WHERE c.id = $2"))
        .bind(bot_user_id)
        .bind(id)
        .fetch_optional(db)
        .await
}

/// Serializes changes to the pool until the transaction ends, so concurrent
/// classifications in different channels can't both keep one past the limit.
/// Take it before touching any `conversations` row in the transaction.
pub async fn lock_pool(tx: &mut sqlx::PgConnection) -> sqlx::Result<()> {
    sqlx::query("SELECT pg_advisory_xact_lock($1)")
        .bind(POOL_LOCK)
        .execute(tx)
        .await?;
    Ok(())
}

/// Closes the least recently active open conversations beyond `max`. Call with
/// [`lock_pool`] held.
pub async fn evict_beyond(tx: &mut sqlx::PgConnection, max: i64) -> sqlx::Result<Vec<i64>> {
    sqlx::query_scalar(
        r#"
        UPDATE conversations SET closed_at = now(), closed_reason = 'evicted'
        WHERE closed_at IS NULL AND id NOT IN (
            SELECT id FROM conversations WHERE closed_at IS NULL
            ORDER BY last_active_at DESC, id DESC LIMIT $1)
        RETURNING id
        "#,
    )
    .bind(max)
    .fetch_all(tx)
    .await
}

/// Advisory lock key for the pool ("kikipool"; the leader lock is "kiki").
const POOL_LOCK: i64 = 0x6b69_6b69_706f_6f6c;
