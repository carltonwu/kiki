//! REST catch-up: fetches every message we don't have yet, per channel, paging
//! forward from the channel's `complete_through` cursor (same algorithm as
//! `tools/backfill/backfill.py`). The first run is the full backfill; later runs
//! only fetch what was sent while the ingester wasn't connected.

use std::{
    collections::HashMap,
    sync::Arc,
    time::{SystemTime, UNIX_EPOCH},
};

use anyhow::{Context, bail};
use futures_util::{StreamExt, stream};
use serde_json::Value;
use sqlx::PgPool;
use tokio::sync::Mutex;
use tracing::{info, warn};
use twilight_http::Client;
use twilight_model::{
    id::{Id, marker::GuildMarker},
    util::Timestamp,
};

use crate::{rest::get_json, store};

/// Channel types that hold messages: text, voice, announcement, stage, and the
/// three thread types. https://discord.com/developers/docs/resources/channel#channel-object-channel-types
const MESSAGE_CHANNEL_TYPES: &[i16] = &[0, 2, 5, 13, 10, 11, 12];
/// Channel types that can have threads: text, announcement, forum, media.
const THREAD_PARENT_TYPES: &[i64] = &[0, 5, 15, 16];
/// Discord's maximum page size for message history.
const PAGE_SIZE: u16 = 100;
/// Channels fetched in parallel. Each channel has its own rate limit bucket.
const CHANNEL_CONCURRENCY: usize = 4;

pub struct CatchUp {
    http: Arc<Client>,
    db: PgPool,
    /// One catch-up per guild at a time; a second request waits, then runs a
    /// (cheap) pass of its own.
    running: HashMap<Id<GuildMarker>, Mutex<()>>,
}

impl CatchUp {
    pub fn new(http: Arc<Client>, db: PgPool, guild_ids: &[Id<GuildMarker>]) -> Self {
        Self {
            http,
            db,
            running: guild_ids.iter().map(|&id| (id, Mutex::new(()))).collect(),
        }
    }

    pub async fn run_logged(&self, guild_id: Id<GuildMarker>) {
        if let Err(e) = self.run(guild_id).await {
            warn!(%guild_id, error = format!("{e:#}"), "catch-up failed; the next one will retry");
        }
    }

    pub async fn run(&self, guild_id: Id<GuildMarker>) -> anyhow::Result<()> {
        let Some(lock) = self.running.get(&guild_id) else {
            bail!("guild {guild_id} is not enabled");
        };
        let _guard = lock.lock().await;
        let gid = guild_id.get() as i64;
        let started = SystemTime::now();

        self.discover(guild_id).await?;

        let behind = store::channels_behind(&self.db, gid, MESSAGE_CHANNEL_TYPES).await?;
        if behind.is_empty() {
            info!(%guild_id, "catch-up: already up to date");
            return Ok(());
        }
        info!(%guild_id, channels = behind.len(), "catch-up: fetching missed messages");

        let results: Vec<anyhow::Result<usize>> = stream::iter(behind)
            .map(|ch| self.fetch_channel(gid, ch))
            .buffer_unordered(CHANNEL_CONCURRENCY)
            .collect()
            .await;
        let mut fetched = 0;
        let mut failed = 0;
        for result in results {
            match result {
                Ok(n) => fetched += n,
                Err(e) => {
                    failed += 1;
                    warn!(%guild_id, error = format!("{e:#}"), "catch-up: channel failed");
                }
            }
        }
        info!(
            %guild_id,
            fetched,
            failed,
            secs = started.elapsed().unwrap_or_default().as_secs(),
            "catch-up finished"
        );
        Ok(())
    }

    /// Refresh the guild's channels and threads, including archived threads that
    /// may have messages we haven't seen.
    async fn discover(&self, guild_id: Id<GuildMarker>) -> anyhow::Result<()> {
        let gid = guild_id.get() as i64;
        let started = unix_micros();
        let http = &*self.http;

        let guild = get_json("guild", || http.guild(guild_id))
            .await?
            .with_context(|| format!("can't access guild {guild_id}: is the bot in it?"))?;
        store::upsert_guild(&self.db, gid, guild["name"].as_str()).await?;

        let channels = get_json("guild channels", || http.guild_channels(guild_id))
            .await?
            .and_then(into_array)
            .with_context(|| format!("can't list channels of guild {guild_id}"))?;
        store::upsert_channels(&self.db, gid, &channels).await?;

        if let Some(active) = get_json("active threads", || http.active_threads(guild_id)).await? {
            let threads = into_array(active["threads"].clone()).unwrap_or_default();
            store::upsert_channels(&self.db, gid, &threads).await?;
        }

        // Archived threads, newest archived first. Stop at the previous discovery:
        // threads archived before it were complete then and can't have changed.
        let cutoff = store::archived_threads_synced_at(&self.db, gid).await?;
        for ch in &channels {
            let Some(kind) = ch["type"]
                .as_i64()
                .filter(|t| THREAD_PARENT_TYPES.contains(t))
            else {
                continue;
            };
            let channel_id = Id::new(snowflake(&ch["id"]).context("channel without id")? as u64);
            // Private threads only exist in text channels (and need Manage Threads).
            for private in [false, true] {
                if private && kind != 0 {
                    continue;
                }
                let mut before: Option<String> = None;
                loop {
                    let b = before.clone();
                    let page = if private {
                        get_json("private archived threads", || {
                            let req = http.private_archived_threads(channel_id).limit(100);
                            match &b {
                                Some(b) => req.before(b),
                                None => req,
                            }
                        })
                        .await?
                    } else {
                        get_json("public archived threads", || {
                            let req = http.public_archived_threads(channel_id).limit(100);
                            match &b {
                                Some(b) => req.before(b),
                                None => req,
                            }
                        })
                        .await?
                    };
                    let Some(page) = page else { break };
                    let threads = into_array(page["threads"].clone()).unwrap_or_default();
                    store::upsert_channels(&self.db, gid, &threads).await?;

                    let Some(oldest) = threads
                        .last()
                        .and_then(|t| t["thread_metadata"]["archive_timestamp"].as_str())
                    else {
                        break;
                    };
                    let reached_cutoff = cutoff.is_some_and(|cutoff| {
                        Timestamp::parse(oldest).is_ok_and(|ts| ts.as_micros() < cutoff)
                    });
                    if reached_cutoff || !page["has_more"].as_bool().unwrap_or(false) {
                        break;
                    }
                    before = Some(oldest.to_owned());
                }
            }
        }
        store::set_archived_threads_synced_at(&self.db, gid, started).await?;
        Ok(())
    }

    /// Pages forward through one channel's history. Returns the number of messages fetched.
    async fn fetch_channel(&self, gid: i64, ch: store::Behind) -> anyhow::Result<usize> {
        let http = &*self.http;
        let channel_id = Id::new(ch.id as u64);
        let mut cursor = ch.complete_through;
        let mut fetched = 0;
        loop {
            // Snowflake ids are never 0, so `after = 1` means "from the start".
            let after = Id::new(cursor.max(1) as u64);
            let page = get_json("channel messages", || {
                http.channel_messages(channel_id)
                    .after(after)
                    .limit(PAGE_SIZE)
            })
            .await?;
            let Some(messages) = page.and_then(into_array) else {
                warn!(
                    channel = ch.name,
                    channel_id = ch.id,
                    "catch-up: no access to channel; skipping"
                );
                return Ok(fetched);
            };
            fetched += messages.len();
            let newest = messages
                .iter()
                .filter_map(|m| snowflake(&m["id"]))
                .max()
                .unwrap_or(cursor);
            if messages.len() < PAGE_SIZE as usize {
                // Reached the end. Everything up to the channel's last message id is
                // now stored, even if that message itself was deleted.
                let complete = newest.max(ch.last_message_id);
                store::store_page(&self.db, gid, ch.id, &messages, complete).await?;
                if fetched > 0 {
                    info!(
                        channel = ch.name,
                        channel_id = ch.id,
                        fetched,
                        "catch-up: channel done"
                    );
                }
                return Ok(fetched);
            }
            store::store_page(&self.db, gid, ch.id, &messages, newest).await?;
            cursor = newest;
            if fetched % 10_000 < PAGE_SIZE as usize {
                info!(
                    channel = ch.name,
                    channel_id = ch.id,
                    fetched,
                    "catch-up: in progress"
                );
            }
        }
    }
}

/// Discord sends snowflake ids as strings.
pub fn snowflake(v: &Value) -> Option<i64> {
    v.as_str()?.parse().ok()
}

fn into_array(v: Value) -> Option<Vec<Value>> {
    match v {
        Value::Array(items) => Some(items),
        _ => None,
    }
}

fn unix_micros() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_micros() as i64
}
