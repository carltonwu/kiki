//! Persistent chat-model overrides, resolved when a new host request is built.

use sqlx::PgPool;

pub async fn effective(db: &PgPool, guild_id: i64, default: &str) -> sqlx::Result<String> {
    Ok(
        sqlx::query_scalar::<_, String>("SELECT model FROM guild_chat_models WHERE guild_id = $1")
            .bind(guild_id)
            .fetch_optional(db)
            .await?
            .unwrap_or_else(|| default.to_owned()),
    )
}

pub async fn set(
    db: &PgPool,
    guild_id: i64,
    model: Option<&str>,
    updated_by: i64,
) -> sqlx::Result<()> {
    match model {
        Some(model) => {
            sqlx::query(
                "INSERT INTO guild_chat_models (guild_id, model, updated_by) VALUES ($1, $2, $3) \
                 ON CONFLICT (guild_id) DO UPDATE \
                 SET model = EXCLUDED.model, updated_by = EXCLUDED.updated_by, updated_at = now()",
            )
            .bind(guild_id)
            .bind(model)
            .bind(updated_by)
            .execute(db)
            .await?;
        }
        None => {
            sqlx::query("DELETE FROM guild_chat_models WHERE guild_id = $1")
                .bind(guild_id)
                .execute(db)
                .await?;
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use sqlx::{Executor, postgres::PgConnectOptions};
    use std::time::{SystemTime, UNIX_EPOCH};

    #[tokio::test]
    async fn overrides_are_persistent_and_isolated_and_reset_to_current_default() {
        let Ok(url) = std::env::var("TEST_DATABASE_URL") else {
            return;
        };
        let schema = format!(
            "guild_models_test_{}",
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        );
        let admin = PgPool::connect(&url).await.unwrap();
        admin
            .execute(format!("CREATE SCHEMA {schema}").as_str())
            .await
            .unwrap();
        let options: PgConnectOptions = url.parse().unwrap();
        let options = options.options([("search_path", format!("{schema},public"))]);
        let db = PgPool::connect_with(options.clone()).await.unwrap();
        crate::MIGRATOR.run(&db).await.unwrap();
        assert_eq!(
            effective(&db, 1, "test/default").await.unwrap(),
            "test/default"
        );
        set(&db, 1, Some("test/first"), 10).await.unwrap();
        set(&db, 2, Some("test/other"), 20).await.unwrap();
        set(&db, 1, Some("test/second"), 11).await.unwrap();
        db.close().await;
        let db = PgPool::connect_with(options).await.unwrap();
        assert_eq!(
            effective(&db, 1, "test/default").await.unwrap(),
            "test/second"
        );
        let actor: i64 =
            sqlx::query_scalar("SELECT updated_by FROM guild_chat_models WHERE guild_id = 1")
                .fetch_one(&db)
                .await
                .unwrap();
        assert_eq!(actor, 11);
        set(&db, 1, None, 11).await.unwrap();
        assert_eq!(
            effective(&db, 1, "test/new-default").await.unwrap(),
            "test/new-default"
        );
        assert_eq!(
            effective(&db, 2, "test/default").await.unwrap(),
            "test/other"
        );
        db.close().await;
        admin
            .execute(format!("DROP SCHEMA {schema} CASCADE").as_str())
            .await
            .unwrap();
    }
}
