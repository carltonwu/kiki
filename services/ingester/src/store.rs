//! Postgres writes for messages and channels. Every write is idempotent, so REST
//! catch-up and live Gateway events can deliver the same object in any order.
//!
//! Discord objects are passed through as JSON and mapped to columns in SQL, so the
//! `raw` column keeps exactly what Discord sent.

use serde_json::Value;
use sqlx::{PgExecutor, PgPool, types::Json};

/// Insert or refresh full message objects (REST pages, `MESSAGE_CREATE`).
///
/// An existing row is only overwritten by a version at least as new (by edit time),
/// so a stale REST page can't undo a live edit. `deleted_at` is never touched: REST
/// can't return deleted messages, and a live delete must stick.
pub async fn upsert_messages(
    db: impl PgExecutor<'_>,
    guild_id: i64,
    messages: &[Value],
) -> sqlx::Result<()> {
    sqlx::query(
        r#"
        INSERT INTO messages (id, guild_id, channel_id, author_id, author_name, author_bot,
                              content, type, reply_to_id, created_at, edited_at, raw)
        SELECT (m->>'id')::bigint,
               $1,
               (m->>'channel_id')::bigint,
               (m->'author'->>'id')::bigint,
               m->'author'->>'username',
               coalesce((m->'author'->>'bot')::boolean, false),
               coalesce(m->>'content', ''),
               coalesce((m->>'type')::smallint, 0),
               -- Type 19 is a reply; other types also use message_reference
               -- (forwards, pins, thread starters).
               CASE WHEN (m->>'type')::int = 19
                    THEN (m->'message_reference'->>'message_id')::bigint END,
               (m->>'timestamp')::timestamptz,
               (m->>'edited_timestamp')::timestamptz,
               m
        FROM jsonb_array_elements($2) AS m
        ON CONFLICT (id) DO UPDATE SET
            author_name = EXCLUDED.author_name,
            content     = EXCLUDED.content,
            type        = EXCLUDED.type,
            reply_to_id = EXCLUDED.reply_to_id,
            edited_at   = EXCLUDED.edited_at,
            -- Merge: Gateway objects carry fields REST ones don't (guild_id, member).
            raw         = messages.raw || EXCLUDED.raw
        WHERE coalesce(EXCLUDED.edited_at, '-infinity') >= coalesce(messages.edited_at, '-infinity')
        "#,
    )
    .bind(guild_id)
    .bind(Json(messages))
    .execute(db)
    .await?;
    Ok(())
}

/// Apply a `MESSAGE_UPDATE`, which may be partial: only the fields present change.
/// Returns false if the message isn't stored yet.
pub async fn update_message(db: &PgPool, update: &Value) -> sqlx::Result<bool> {
    let result = sqlx::query(
        r#"
        UPDATE messages SET
            content   = coalesce($1->>'content', content),
            edited_at = coalesce(($1->>'edited_timestamp')::timestamptz, edited_at),
            raw       = raw || $1
        WHERE id = ($1->>'id')::bigint
        "#,
    )
    .bind(Json(update))
    .execute(db)
    .await?;
    Ok(result.rows_affected() > 0)
}

pub async fn delete_messages(db: &PgPool, ids: &[i64]) -> sqlx::Result<()> {
    sqlx::query("UPDATE messages SET deleted_at = now() WHERE id = ANY($1) AND deleted_at IS NULL")
        .bind(ids)
        .execute(db)
        .await?;
    Ok(())
}

/// Insert or refresh channel and thread objects. Gateway `GUILD_CREATE` channels
/// omit `guild_id`, so it's passed separately.
pub async fn upsert_channels(db: &PgPool, guild_id: i64, channels: &[Value]) -> sqlx::Result<()> {
    sqlx::query(
        r#"
        INSERT INTO channels (id, guild_id, parent_id, name, type, last_message_id)
        SELECT (c->>'id')::bigint,
               $1,
               (c->>'parent_id')::bigint,
               c->>'name',
               (c->>'type')::smallint,
               (c->>'last_message_id')::bigint
        FROM jsonb_array_elements($2) AS c
        ON CONFLICT (id) DO UPDATE SET
            parent_id       = EXCLUDED.parent_id,
            name            = EXCLUDED.name,
            type            = EXCLUDED.type,
            last_message_id = greatest(channels.last_message_id, EXCLUDED.last_message_id),
            deleted_at      = NULL,
            updated_at      = now()
        "#,
    )
    .bind(guild_id)
    .bind(Json(channels))
    .execute(db)
    .await?;
    Ok(())
}

pub async fn delete_channel(db: &PgPool, id: i64) -> sqlx::Result<()> {
    sqlx::query("UPDATE channels SET deleted_at = now(), updated_at = now() WHERE id = $1")
        .bind(id)
        .execute(db)
        .await?;
    Ok(())
}

/// Record a live message as the channel's newest. Doesn't touch `complete_through`.
pub async fn bump_last_message(db: &PgPool, channel_id: i64, message_id: i64) -> sqlx::Result<()> {
    sqlx::query(
        "UPDATE channels SET last_message_id = greatest(last_message_id, $2) WHERE id = $1",
    )
    .bind(channel_id)
    .bind(message_id)
    .execute(db)
    .await?;
    Ok(())
}

/// Store one page of REST history and advance the channel's cursor atomically, so
/// an interrupted catch-up resumes exactly where it stopped.
pub async fn store_page(
    db: &PgPool,
    guild_id: i64,
    channel_id: i64,
    messages: &[Value],
    complete_through: i64,
) -> sqlx::Result<()> {
    let mut tx = db.begin().await?;
    if !messages.is_empty() {
        upsert_messages(&mut *tx, guild_id, messages).await?;
    }
    sqlx::query(
        "UPDATE channels SET complete_through = greatest(complete_through, $2) WHERE id = $1",
    )
    .bind(channel_id)
    .bind(complete_through)
    .execute(&mut *tx)
    .await?;
    tx.commit().await
}

pub struct Behind {
    pub id: i64,
    pub name: Option<String>,
    pub complete_through: i64,
    pub last_message_id: i64,
}

pub async fn channels_behind(
    db: &PgPool,
    guild_id: i64,
    types: &[i16],
) -> sqlx::Result<Vec<Behind>> {
    let rows: Vec<(i64, Option<String>, i64, i64)> = sqlx::query_as(
        r#"
        SELECT id, name, complete_through, last_message_id FROM channels
        WHERE guild_id = $1 AND deleted_at IS NULL AND type = ANY($2)
          AND last_message_id > complete_through
        ORDER BY last_message_id DESC
        "#,
    )
    .bind(guild_id)
    .bind(types)
    .fetch_all(db)
    .await?;
    Ok(rows
        .into_iter()
        .map(|(id, name, complete_through, last_message_id)| Behind {
            id,
            name,
            complete_through,
            last_message_id,
        })
        .collect())
}

pub async fn upsert_guild(db: &PgPool, id: i64, name: Option<&str>) -> sqlx::Result<()> {
    sqlx::query(
        r#"
        INSERT INTO guilds (id, name) VALUES ($1, $2)
        ON CONFLICT (id) DO UPDATE SET name = coalesce(EXCLUDED.name, guilds.name), updated_at = now()
        "#,
    )
    .bind(id)
    .bind(name)
    .execute(db)
    .await?;
    Ok(())
}

/// Start of the last finished thread discovery, in Unix microseconds.
pub async fn archived_threads_synced_at(db: &PgPool, guild_id: i64) -> sqlx::Result<Option<i64>> {
    sqlx::query_scalar(
        "SELECT (extract(epoch FROM archived_threads_synced_at) * 1e6)::bigint FROM guilds WHERE id = $1",
    )
    .bind(guild_id)
    .fetch_optional(db)
    .await
    .map(Option::flatten)
}

pub async fn set_archived_threads_synced_at(
    db: &PgPool,
    guild_id: i64,
    unix_micros: i64,
) -> sqlx::Result<()> {
    sqlx::query(
        "UPDATE guilds SET archived_threads_synced_at = to_timestamp($2 / 1e6) WHERE id = $1",
    )
    .bind(guild_id)
    .bind(unix_micros as f64)
    .execute(db)
    .await?;
    Ok(())
}
