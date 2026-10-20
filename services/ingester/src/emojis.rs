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
        crate::agent::emoji::Reaction::from(self).to_string()
    }
}

/// Replaces the guild's emoji list with `emojis` (Discord's `GET
/// guilds/{id}/emojis`, or a Gateway event's list), as read at `snapshot_ms`
/// (the database clock, `now_ms`; `None` for now). Descriptions survive renames;
/// emoji missing from the list are marked removed, not deleted.
///
/// Syncs of one guild take turns (an advisory lock), and one whose snapshot is
/// older than the last applied one is skipped (`None`): a resync's REST list,
/// read before a Gateway update was applied, would bring a deleted emoji back.
/// Records the guild as synced (`guild_emoji_syncs`), even with no emoji.
pub async fn sync(
    db: &PgPool,
    guild_id: i64,
    emojis: &[Value],
    snapshot_ms: Option<i64>,
) -> sqlx::Result<Option<Synced>> {
    let mut tx = db.begin().await?;
    sqlx::query("SELECT pg_advisory_xact_lock(hashtextextended('guild_emojis:' || $1, 0))")
        .bind(guild_id.to_string())
        .execute(&mut *tx)
        .await?;
    let snapshot: i64 = sqlx::query_scalar(
        "SELECT coalesce($1, (extract(epoch FROM clock_timestamp()) * 1000)::bigint)",
    )
    .bind(snapshot_ms)
    .fetch_one(&mut *tx)
    .await?;
    let stale: bool = sqlx::query_scalar(
        "SELECT EXISTS (SELECT 1 FROM guild_emoji_syncs \
         WHERE guild_id = $1 AND snapshot_at > to_timestamp($2::double precision / 1000))",
    )
    .bind(guild_id)
    .bind(snapshot)
    .fetch_one(&mut *tx)
    .await?;
    if stale {
        return Ok(None);
    }
    // Every CTE sees the table as it was before the statement, so `before`
    // is the previous state while `upserted` and `removed` write.
    let synced: Synced = sqlx::query_as(
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
    .fetch_one(&mut *tx)
    .await?;
    sqlx::query(
        "INSERT INTO guild_emoji_syncs (guild_id, snapshot_at) \
         VALUES ($1, to_timestamp($2::double precision / 1000)) \
         ON CONFLICT (guild_id) DO UPDATE SET snapshot_at = EXCLUDED.snapshot_at, synced_at = now()",
    )
    .bind(guild_id)
    .bind(snapshot)
    .execute(&mut *tx)
    .await?;
    tx.commit().await?;
    Ok(Some(synced))
}

/// Syncs from a Gateway event's `emojis` (`GUILD_CREATE` after every new session,
/// `GUILD_EMOJIS_UPDATE` when one is added, renamed or deleted), so Kiki's emoji
/// check (the agent host's `check_emojis`) neither rejects a new emoji nor passes
/// a deleted one. Descriptions are still `/kiki emojis resync:true`'s: an event
/// shouldn't spend on a vision model. `None` when the payload has no list (which
/// would otherwise mark every emoji removed) or a newer list was already applied.
pub async fn sync_listed(
    db: &PgPool,
    guild_id: i64,
    emojis: &Value,
) -> sqlx::Result<Option<Synced>> {
    match emojis.as_array() {
        Some(list) => sync(db, guild_id, list, None).await,
        None => Ok(None),
    }
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

/// The server's usable emoji named exactly (ignoring case) like `names`, as for
/// a `:name:` reaction; `find_emoji`'s exact match. Keyed by lowercase name.
pub async fn named(
    db: &PgPool,
    guild_id: i64,
    names: &[String],
) -> sqlx::Result<std::collections::HashMap<String, Emoji>> {
    let lower: Vec<String> = names.iter().map(|n| n.to_lowercase()).collect();
    let found: Vec<Emoji> = sqlx::query_as(
        r#"
        SELECT DISTINCT ON (lower(name)) id, name, animated, available, description
        FROM guild_emojis
        WHERE guild_id = $1 AND removed_at IS NULL AND available AND lower(name) = ANY($2)
        ORDER BY lower(name), name <> ALL($3), id
        "#,
    )
    .bind(guild_id)
    .bind(&lower)
    .bind(names)
    .fetch_all(db)
    .await?;
    Ok(found
        .into_iter()
        .map(|e| (e.name.to_lowercase(), e))
        .collect())
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
    use serde_json::json;

    use super::{Synced, list, named, sync, sync_listed};

    #[tokio::test]
    async fn sync_adds_renames_removes_and_keeps_descriptions() {
        let Some(test) = crate::test_db::migrated("emojis_test").await else {
            return;
        };
        let db = test.db.clone();

        let huh = json!({"id": "11", "name": "huh"});
        let oop = json!({"id": "12", "name": "oop", "animated": true});
        let first = sync(
            &db,
            1,
            &[huh.clone(), oop.clone(), json!({"name": "no_id"})],
            None,
        )
        .await
        .unwrap();
        assert_eq!(
            first,
            Some(Synced {
                total: 2,
                added: 2,
                renamed: 0,
                removed: 0
            })
        );
        sqlx::raw_sql(
            "UPDATE guild_emojis SET description = 'confused' WHERE id = 11; \
             UPDATE guild_emojis SET embedding = array_fill(0.1::real, ARRAY[1024])::halfvec WHERE id = 11",
        )
        .execute(&db)
        .await
        .unwrap();
        // Another server's emoji is never touched.
        sync(&db, 2, &[json!({"id": "21", "name": "theirs"})], None)
            .await
            .unwrap();

        let renamed = json!({"id": "11", "name": "huh_what"});
        let second = sync(&db, 1, &[renamed, json!({"id": "13", "name": "new"})], None)
            .await
            .unwrap()
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
        // `:name:` reactions: exact names ignoring case, this server's, not removed.
        let wanted = ["HUH_WHAT", "huh", "oop", "theirs"].map(String::from);
        let found = named(&db, 1, &wanted).await.unwrap();
        assert_eq!(found.len(), 1);
        assert_eq!(found["huh_what"].mention(), "<:huh_what:11>");

        // A removed emoji that comes back counts as added, with its description.
        let third = sync(&db, 1, &[oop], None).await.unwrap().unwrap();
        assert_eq!((third.added, third.removed), (1, 2));
        let [oop] = &list(&db, 1).await.unwrap()[..] else {
            panic!("expected one emoji");
        };
        assert_eq!(oop.mention(), "<a:oop:12>");

        test.drop().await;
    }

    #[tokio::test]
    async fn an_older_snapshot_never_overwrites_a_newer_one() {
        let Some(test) = crate::test_db::migrated("emojis_order_test").await else {
            return;
        };
        let db = test.db.clone();
        let huh = json!({"id": "11", "name": "huh"});
        let fresh = json!({"id": "12", "name": "fresh"});
        // A resync reads Discord's list (both emoji) at t=1000; meanwhile a Gateway
        // update, read at t=2000, says fresh was deleted and is applied first.
        let update = sync(&db, 1, std::slice::from_ref(&huh), Some(2000))
            .await
            .unwrap();
        assert!(update.is_some());
        let resync = sync(&db, 1, &[huh.clone(), fresh], Some(1000))
            .await
            .unwrap();
        assert_eq!(resync, None, "the older REST list is skipped");
        let names: Vec<String> = list(&db, 1)
            .await
            .unwrap()
            .into_iter()
            .map(|e| e.name)
            .collect();
        assert_eq!(names, ["huh"]);
        // Another guild's syncs are ordered on their own.
        assert!(sync(&db, 2, &[], Some(1000)).await.unwrap().is_some());
        // A synced guild with no emoji is recorded as synced.
        let synced: i64 =
            sqlx::query_scalar("SELECT count(*) FROM guild_emoji_syncs WHERE guild_id = 2")
                .fetch_one(&db)
                .await
                .unwrap();
        assert_eq!(synced, 1);
        test.drop().await;
    }

    #[tokio::test]
    async fn gateway_events_keep_the_list_current() {
        let Some(test) = crate::test_db::migrated("emojis_gateway_test").await else {
            return;
        };
        let db = test.db.clone();
        let event = |names: &[(&str, &str)]| {
            json!(
                names
                    .iter()
                    .map(|(id, name)| json!({"id": id, "name": name, "animated": false}))
                    .collect::<Vec<_>>()
            )
        };
        let first = sync_listed(&db, 1, &event(&[("11", "huh"), ("12", "oop")]))
            .await
            .unwrap();
        assert_eq!(first.map(|s| s.added), Some(2));
        // An emoji added and one deleted in Discord, as GUILD_EMOJIS_UPDATE sends them.
        let update = sync_listed(&db, 1, &event(&[("11", "huh"), ("13", "fresh")]))
            .await
            .unwrap();
        assert_eq!(update.map(|s| (s.added, s.removed)), Some((1, 1)));
        let names: Vec<String> = list(&db, 1)
            .await
            .unwrap()
            .into_iter()
            .map(|e| e.name)
            .collect();
        assert_eq!(names, ["fresh", "huh"]);
        // A payload without a list (an unavailable guild) changes nothing.
        assert_eq!(sync_listed(&db, 1, &json!(null)).await.unwrap(), None);
        assert_eq!(list(&db, 1).await.unwrap().len(), 2);
        test.drop().await;
    }
}
