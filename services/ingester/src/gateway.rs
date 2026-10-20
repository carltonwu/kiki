//! Discord Gateway listener: stores live message and channel events, and hands
//! slash commands to [`Commands`].
//!
//! twilight-gateway runs the protocol (identify, heartbeats, resume, reconnects);
//! dispatch payloads are handled as raw JSON so `raw` stores exactly what Discord sent.

use std::{
    collections::HashSet,
    sync::Arc,
    time::{Duration, SystemTime, UNIX_EPOCH},
};

use anyhow::bail;
use futures_util::StreamExt;
use serde_json::Value;
use sqlx::PgPool;
use tokio::sync::watch;
use tracing::{Instrument, debug, info, info_span, warn};
use twilight_gateway::{CloseFrame, ConfigBuilder, Intents, Message, Shard, ShardId};
use twilight_model::{id::Id, util::Timestamp};

use crate::{
    catch_up::{CatchUp, snowflake},
    commands::Commands,
    emojis,
    metrics::{self, Author, Reconnect},
    store, telemetry,
};

const CLOSE_TIMEOUT: Duration = Duration::from_secs(5);

pub struct Listener {
    pub db: PgPool,
    pub catch_up: Arc<CatchUp>,
    pub guild_ids: HashSet<i64>,
    /// Kiki's own messages count as `author=kiki`.
    pub bot_user_id: i64,
    pub commands: Arc<Commands>,
    /// A fake Gateway for local testing; `None` is Discord's.
    pub gateway_url: Option<String>,
}

impl Listener {
    pub async fn run(
        &self,
        token: String,
        mut shutdown: watch::Receiver<bool>,
    ) -> anyhow::Result<()> {
        // MESSAGE_CONTENT is privileged: enable it in the Discord developer portal.
        // GUILD_EMOJIS_AND_STICKERS (not privileged) sends GUILD_EMOJIS_UPDATE.
        let intents = Intents::GUILDS
            | Intents::GUILD_MESSAGES
            | Intents::MESSAGE_CONTENT
            | Intents::GUILD_EMOJIS_AND_STICKERS;
        let mut config = ConfigBuilder::new(token, intents);
        if let Some(url) = &self.gateway_url {
            config = config.proxy_url(url.clone());
        }
        let mut shard = Shard::with_config(ShardId::ONE, config.build());
        info!("connecting to the Discord Gateway");

        // Set by a reconnect request or invalid session, which the shard
        // answers by closing the connection.
        let mut dropping = None;
        loop {
            let item = tokio::select! {
                item = shard.next() => item,
                _ = shutdown.wait_for(|stop| *stop) => break,
            };
            match item {
                // The shard reconnects (resuming when it can) on its own.
                Some(Ok(Message::Close(frame))) => {
                    let reason = close_reason(dropping.take(), frame.as_ref().map(|f| f.code));
                    metrics::gateway_reconnect(reason);
                    info!(?frame, "Gateway connection closed; reconnecting")
                }
                Some(Ok(Message::Text(text))) => {
                    if let Some(reason) = self.handle(&text).await {
                        dropping = Some(reason);
                    }
                }
                Some(Err(e)) => warn!(error = %e, "Gateway receive error"),
                None => bail!(
                    "Gateway closed the connection for good: check DISCORD_BOT_TOKEN and that \
                     the Message Content intent is enabled"
                ),
            }
        }

        shard.close(CloseFrame::NORMAL);
        let closed = tokio::time::timeout(CLOSE_TIMEOUT, async {
            while let Some(item) = shard.next().await {
                match item {
                    Ok(Message::Close(_)) => break,
                    Ok(Message::Text(text)) => {
                        self.handle(&text).await;
                    }
                    Err(_) => {}
                }
            }
        });
        if closed.await.is_err() {
            warn!("Gateway close not acknowledged in time");
        }
        info!("Gateway connection closed");
        Ok(())
    }

    /// Stores a dispatch. Returns why the shard is about to drop the connection,
    /// if the payload is Discord telling it to.
    async fn handle(&self, text: &str) -> Option<Reconnect> {
        let payload: Value = match serde_json::from_str(text) {
            Ok(v) => v,
            Err(e) => {
                warn!(error = %e, "unparseable Gateway payload");
                return None;
            }
        };
        // Only dispatches (op 0) carry events; the shard handles every other opcode.
        match payload["op"].as_u64() {
            Some(0) => {}
            Some(7) => return Some(Reconnect::Requested),
            Some(9) => return Some(Reconnect::InvalidSession),
            _ => return None,
        }
        let event = payload["t"].as_str()?;
        metrics::gateway_event(event);
        if let Err(e) = self.dispatch(event, &payload["d"]).await {
            metrics::gateway_store_error();
            warn!(
                event,
                guild_id = payload["d"]["guild_id"].as_str(),
                channel_id = payload["d"]["channel_id"].as_str(),
                object_id = payload["d"]["id"].as_str(),
                sequence = payload["s"].as_u64(),
                error = format!("{e:#}"),
                "failed to store Gateway event"
            );
        }
        None
    }

    async fn dispatch(&self, event: &str, d: &Value) -> anyhow::Result<()> {
        if event == "RESUMED" {
            info!("Gateway session resumed");
            return Ok(());
        }
        if event == "READY" {
            let in_guilds: HashSet<i64> = d["guilds"]
                .as_array()
                .into_iter()
                .flatten()
                .filter_map(|g| snowflake(&g["id"]))
                .collect();
            for id in self.guild_ids.difference(&in_guilds) {
                warn!(guild_id = id, "the bot is not in an enabled guild");
            }
            info!(guilds = in_guilds.len(), "Gateway session ready");
            return Ok(());
        }

        let guild_id = if event == "GUILD_CREATE" {
            &d["id"]
        } else {
            &d["guild_id"]
        };
        let Some(guild_id) = snowflake(guild_id) else {
            return Ok(());
        };
        if !self.guild_ids.contains(&guild_id) {
            debug!(
                event,
                guild_id, "ignoring event from a guild that isn't enabled"
            );
            return Ok(());
        }

        match event {
            "INTERACTION_CREATE" => self.commands.spawn_handle(d.clone()),
            "MESSAGE_CREATE" => {
                let span = info_span!(
                    "message_create",
                    guild_id,
                    channel_id = d["channel_id"].as_str(),
                    message_id = d["id"].as_str(),
                    author_id = d["author"]["id"].as_str(),
                );
                self.store_message(guild_id, d).instrument(span).await?;
            }
            "MESSAGE_UPDATE" => {
                // Unknown message (sent while we were offline): store it if the
                // update is a full object, otherwise catch-up will fetch it.
                let full = d.get("author").is_some() && d.get("timestamp").is_some();
                if !store::update_message(&self.db, d).await? && full {
                    store::upsert_messages(&self.db, guild_id, std::slice::from_ref(d)).await?;
                }
            }
            "MESSAGE_DELETE" => {
                if let Some(id) = snowflake(&d["id"]) {
                    store::delete_messages(&self.db, &[id]).await?;
                }
            }
            "MESSAGE_DELETE_BULK" => {
                let ids: Vec<i64> = d["ids"]
                    .as_array()
                    .into_iter()
                    .flatten()
                    .filter_map(snowflake)
                    .collect();
                store::delete_messages(&self.db, &ids).await?;
            }
            "CHANNEL_CREATE" | "CHANNEL_UPDATE" | "THREAD_CREATE" | "THREAD_UPDATE" => {
                store::upsert_channels(&self.db, guild_id, std::slice::from_ref(d)).await?;
            }
            "CHANNEL_DELETE" | "THREAD_DELETE" => {
                if let Some(id) = snowflake(&d["id"]) {
                    store::delete_channel(&self.db, id).await?;
                }
            }
            "GUILD_EMOJIS_UPDATE" => self.spawn_emoji_sync(guild_id, &d["emojis"]),
            "THREAD_LIST_SYNC" => {
                let threads = d["threads"].as_array().cloned().unwrap_or_default();
                store::upsert_channels(&self.db, guild_id, &threads).await?;
            }
            "GUILD_CREATE" => {
                // Sent after every new session (not after a resume) and when an
                // unavailable guild comes back: anything sent since we last
                // caught up may be missing, so catch up again in the background.
                if d["unavailable"].as_bool() == Some(true) {
                    return Ok(());
                }
                store::upsert_guild(&self.db, guild_id, d["name"].as_str()).await?;
                let mut channels = d["channels"].as_array().cloned().unwrap_or_default();
                channels.extend(d["threads"].as_array().cloned().unwrap_or_default());
                store::upsert_channels(&self.db, guild_id, &channels).await?;
                info!(
                    guild_id,
                    name = d["name"].as_str(),
                    "guild available; catching up"
                );
                let catch_up = self.catch_up.clone();
                tokio::spawn(async move { catch_up.run_logged(Id::new(guild_id as u64)).await });
                self.spawn_emoji_sync(guild_id, &d["emojis"]);
            }
            _ => {}
        }
        Ok(())
    }

    /// Syncs the server's emoji list from `GUILD_CREATE` or `GUILD_EMOJIS_UPDATE`
    /// in the background, best effort: it never holds up catch-up or the next
    /// event. Its snapshot is when the event arrived, so a sync that runs late
    /// can't overwrite a newer one.
    fn spawn_emoji_sync(&self, guild_id: i64, emojis: &Value) {
        let snapshot_us = emojis::now_us();
        tokio::spawn(emojis::sync_logged(
            self.db.clone(),
            guild_id,
            emojis.clone(),
            snapshot_us,
        ));
    }

    /// A live message: the start of its trace, which routing continues.
    async fn store_message(&self, guild_id: i64, d: &Value) -> anyhow::Result<()> {
        if let Some(lag) = lag(d, SystemTime::now()) {
            metrics::gateway_lag(lag);
        }
        metrics::message_ingested(match snowflake(&d["author"]["id"]) {
            Some(id) if id == self.bot_user_id => Author::Kiki,
            _ if d["author"]["bot"].as_bool() == Some(true) => Author::Bot,
            _ => Author::Human,
        });
        let id = snowflake(&d["id"]);
        // Before the insert, whose notification the router may handle at once.
        if let Some(id) = id {
            telemetry::remember_message(id);
        }
        store::upsert_messages(&self.db, guild_id, std::slice::from_ref(d)).await?;
        if let (Some(channel), Some(id)) = (snowflake(&d["channel_id"]), id) {
            store::bump_last_message(&self.db, channel, id).await?;
        }
        Ok(())
    }
}

/// Why a closed connection dropped: what Discord said before, else its close code.
fn close_reason(said: Option<Reconnect>, code: Option<u16>) -> Reconnect {
    match (said, code) {
        (Some(reason), _) => reason,
        (None, None | Some(1006)) => Reconnect::Transport,
        (None, Some(_)) => Reconnect::Closed,
    }
}

/// How long after Discord created the message it arrived.
fn lag(d: &Value, now: SystemTime) -> Option<Duration> {
    let created = Timestamp::parse(d["timestamp"].as_str()?).ok()?.as_micros();
    let now = now.duration_since(UNIX_EPOCH).ok()?.as_micros() as i64;
    Some(Duration::from_micros(
        now.saturating_sub(created).max(0) as u64
    ))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn close_reasons() {
        let requested = Some(Reconnect::Requested);
        assert_eq!(close_reason(requested, Some(4000)), Reconnect::Requested);
        assert_eq!(close_reason(None, Some(1006)), Reconnect::Transport);
        assert_eq!(close_reason(None, None), Reconnect::Transport);
        assert_eq!(close_reason(None, Some(4009)), Reconnect::Closed);
    }

    #[test]
    fn lag_is_receipt_minus_the_message_timestamp() {
        let d = serde_json::json!({"timestamp": "2026-10-19T12:00:00.000000+00:00"});
        let sent = UNIX_EPOCH + Duration::from_secs(1_792_411_200);
        assert_eq!(
            lag(&d, sent + Duration::from_millis(1500)),
            Some(Duration::from_millis(1500))
        );
        // Clock skew never makes it negative.
        assert_eq!(lag(&d, sent - Duration::from_secs(1)), Some(Duration::ZERO));
        assert_eq!(lag(&serde_json::json!({}), sent), None);
    }
}
