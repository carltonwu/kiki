//! A server's custom emoji (`guild_emojis`, migration 0017), for Kiki's emoji
//! tools. `/kiki emojis resync:true` copies the list from Discord here, then the
//! agent host describes new ones with a vision model; the host owns the
//! descriptions, embeddings and the `search_emojis`/`find_emoji` tools.

use serde_json::Value;
use sqlx::{FromRow, PgPool, types::Json};

/// What a sync changed.
#[derive(Debug, Default, FromRow, PartialEq)]
pub struct Synced {
    /// Emoji the server has now.
    pub total: i64,
    /// New, or back after being removed.
    pub added: i64,
    pub renamed: i64,
    pub removed: i64,
}

#[derive(FromRow)]
pub struct Emoji {
    pub id: i64,
    pub name: String,
    pub animated: bool,
    pub available: bool,
    pub description: Option<String>,
}

impl Emoji {
    /// How the emoji is written in a message.
    pub fn mention(&self) -> String {
        format!(
            "<{}:{}:{}>",
            if self.animated { "a" } else { "" },
            self.name,
            self.id
        )
    }
}

/// Replaces the guild's emoji list with `emojis` (Discord's `GET
/// guilds/{id}/emojis`). Descriptions survive renames; emoji missing from the
/// list are marked removed, not deleted.
pub async fn sync(db: &PgPool, guild_id: i64, emojis: &[Value]) -> sqlx::Result<Synced> {
    // Every CTE sees the table as it was before the statement, so `before`
    // is the previous state while `upserted` and `removed` write.
    sqlx::query_as(
        r#"
        WITH incoming AS (
            SELECT DISTINCT ON ((e->>'id')::bigint)
                   (e->>'id')::bigint AS id,
                   e->>'name' AS name,
                   coalesce((e->>'animated')::boolean, false) AS animated,
                   coalesce((e->>'available')::boolean, true) AS available,
                   e AS raw
            FROM jsonb_array_elements($2) AS e
            WHERE e->>'id' ~ '^[0-9]+$' AND coalesce(e->>'name', '') <> ''
        ),
        before AS (
            SELECT id, name FROM guild_emojis WHERE guild_id = $1 AND removed_at IS NULL
        ),
        upserted AS (
            INSERT INTO guild_emojis (id, guild_id, name, animated, available, raw)
            SELECT id, $1, name, animated, available, raw FROM incoming
            ON CONFLICT (id) DO UPDATE SET
                name = EXCLUDED.name,
                animated = EXCLUDED.animated,
                available = EXCLUDED.available,
                raw = EXCLUDED.raw,
                synced_at = now(),
                removed_at = NULL
            WHERE guild_emojis.guild_id = $1
            RETURNING id
        ),
        removed AS (
            UPDATE guild_emojis SET removed_at = now()
            WHERE guild_id = $1 AND removed_at IS NULL AND id NOT IN (SELECT id FROM incoming)
            RETURNING id
        )
        SELECT (SELECT count(*) FROM upserted) AS total,
               (SELECT count(*) FROM upserted u WHERE u.id NOT IN (SELECT id FROM before))
                   AS added,
               (SELECT count(*) FROM incoming i JOIN before b ON b.id = i.id
                WHERE b.name <> i.name) AS renamed,
               (SELECT count(*) FROM removed) AS removed
        "#,
    )
    .bind(guild_id)
    .bind(Json(emojis))
    .fetch_one(db)
    .await
}

/// The server's current emoji, by name.
pub async fn list(db: &PgPool, guild_id: i64) -> sqlx::Result<Vec<Emoji>> {
    sqlx::query_as(
        r#"
        SELECT id, name, animated, available, description FROM guild_emojis
        WHERE guild_id = $1 AND removed_at IS NULL
        ORDER BY lower(name), id
        "#,
    )
    .bind(guild_id)
    .fetch_all(db)
    .await
}

/// The database clock in Unix milliseconds: describe calls compare it with
/// their own `now()`, so one resync tries each emoji at most once.
pub async fn now_ms(db: &PgPool) -> sqlx::Result<i64> {
    sqlx::query_scalar("SELECT (extract(epoch FROM now()) * 1000)::bigint")
        .fetch_one(db)
        .await
}

#[cfg(test)]
mod tests {
    use std::time::{SystemTime, UNIX_EPOCH};

    use serde_json::json;
    use sqlx::{Executor, PgPool, postgres::PgConnectOptions};

    use super::{Synced, list, sync};

    #[tokio::test]
    async fn sync_adds_renames_removes_and_keeps_descriptions() {
        let Ok(url) = std::env::var("TEST_DATABASE_URL") else {
            return;
        };
        let schema = format!(
            "emojis_test_{}",
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
        let db =
            PgPool::connect_with(options.options([("search_path", format!("{schema},public"))]))
                .await
                .unwrap();
        // In public, like the migrations; retried once in case a concurrent test
        // is creating it too.
        for attempt in 0..2 {
            let created = admin
                .execute("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public")
                .await;
            if created.is_ok() || attempt == 1 {
                created.unwrap();
                break;
            }
        }
        sqlx::raw_sql(include_str!("../migrations/0017_guild_emojis.sql"))
            .execute(&db)
            .await
            .unwrap();

        let huh = json!({"id": "11", "name": "huh"});
        let oop = json!({"id": "12", "name": "oop", "animated": true});
        let first = sync(
            &db,
            1,
            &[huh.clone(), oop.clone(), json!({"name": "no_id"})],
        )
        .await
        .unwrap();
        assert_eq!(
            first,
            Synced {
                total: 2,
                added: 2,
                renamed: 0,
                removed: 0
            }
        );
        sqlx::raw_sql(
            "UPDATE guild_emojis SET description = 'confused' WHERE id = 11; \
             UPDATE guild_emojis SET embedding = array_fill(0.1::real, ARRAY[1024])::halfvec WHERE id = 11",
        )
        .execute(&db)
        .await
        .unwrap();
        // Another server's emoji is never touched.
        sync(&db, 2, &[json!({"id": "21", "name": "theirs"})])
            .await
            .unwrap();

        let renamed = json!({"id": "11", "name": "huh_what"});
        let second = sync(&db, 1, &[renamed, json!({"id": "13", "name": "new"})])
            .await
            .unwrap();
        assert_eq!(
            second,
            Synced {
                total: 2,
                added: 1,
                renamed: 1,
                removed: 1
            }
        );
        let (description, embedded): (Option<String>, bool) = sqlx::query_as(
            "SELECT description, embedding IS NOT NULL FROM guild_emojis WHERE id = 11",
        )
        .fetch_one(&db)
        .await
        .unwrap();
        assert_eq!(description.as_deref(), Some("confused"));
        assert!(!embedded, "a rename invalidates the embedding");
        let names: Vec<String> = list(&db, 1)
            .await
            .unwrap()
            .into_iter()
            .map(|e| e.name)
            .collect();
        assert_eq!(names, ["huh_what", "new"]);
        assert_eq!(list(&db, 2).await.unwrap().len(), 1);

        // A removed emoji that comes back counts as added, with its description.
        let third = sync(&db, 1, &[oop]).await.unwrap();
        assert_eq!((third.added, third.removed), (1, 2));
        let [oop] = &list(&db, 1).await.unwrap()[..] else {
            panic!("expected one emoji");
        };
        assert_eq!(oop.mention(), "<a:oop:12>");

        db.close().await;
        admin
            .execute(format!("DROP SCHEMA {schema} CASCADE").as_str())
            .await
            .unwrap();
    }
}
