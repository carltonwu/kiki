//! Discord Gateway listener: stores live message and channel events.
//!
//! twilight-gateway runs the protocol (identify, heartbeats, resume, reconnects);
//! dispatch payloads are handled as raw JSON so `raw` stores exactly what Discord sent.

use std::{collections::HashSet, sync::Arc, time::Duration};

use anyhow::bail;
use futures_util::StreamExt;
use serde_json::Value;
use sqlx::PgPool;
use tokio::sync::watch;
use tracing::{debug, info, warn};
use twilight_gateway::{CloseFrame, Intents, Message, Shard, ShardId};
use twilight_model::id::Id;

use crate::{
    catch_up::{CatchUp, snowflake},
    store,
};

/// How long to wait for Discord to acknowledge a close on shutdown.
const CLOSE_TIMEOUT: Duration = Duration::from_secs(5);

pub struct Listener {
    pub db: PgPool,
    pub catch_up: Arc<CatchUp>,
    pub guild_ids: HashSet<i64>,
}

impl Listener {
    pub async fn run(
        &self,
        token: String,
        mut shutdown: watch::Receiver<bool>,
    ) -> anyhow::Result<()> {
        // MESSAGE_CONTENT is privileged: enable it in the Discord developer portal.
        let intents = Intents::GUILDS | Intents::GUILD_MESSAGES | Intents::MESSAGE_CONTENT;
        let mut shard = Shard::new(ShardId::ONE, token, intents);
        info!("connecting to the Discord Gateway");

        loop {
            let item = tokio::select! {
                item = shard.next() => item,
                _ = shutdown.wait_for(|stop| *stop) => break,
            };
            match item {
                // The shard reconnects (resuming when it can) on its own.
                Some(Ok(Message::Close(frame))) => {
                    info!(?frame, "Gateway connection closed; reconnecting")
                }
                Some(Ok(Message::Text(text))) => self.handle(&text).await,
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
                    Ok(Message::Text(text)) => self.handle(&text).await,
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

    async fn handle(&self, text: &str) {
        let payload: Value = match serde_json::from_str(text) {
            Ok(v) => v,
            Err(e) => return warn!(error = %e, "unparseable Gateway payload"),
        };
        // Only dispatches (op 0) carry events; the shard handles every other opcode.
        if payload["op"].as_u64() != Some(0) {
            return;
        }
        let Some(event) = payload["t"].as_str() else {
            return;
        };
        if let Err(e) = self.dispatch(event, &payload["d"]).await {
            // Losing one event isn't fatal: the next catch-up refetches messages.
            warn!(
                event,
                error = format!("{e:#}"),
                "failed to store Gateway event"
            );
        }
    }

    async fn dispatch(&self, event: &str, d: &Value) -> anyhow::Result<()> {
        if event == "READY" {
            let session = d["session_id"].as_str().unwrap_or_default();
            let in_guilds: HashSet<i64> = d["guilds"]
                .as_array()
                .into_iter()
                .flatten()
                .filter_map(|g| snowflake(&g["id"]))
                .collect();
            for id in self.guild_ids.difference(&in_guilds) {
                warn!(guild_id = id, "the bot is not in an enabled guild");
            }
            info!(session, "Gateway session ready");
            return Ok(());
        }

        // Everything else is ingested only for enabled guilds.
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
            "MESSAGE_CREATE" => {
                store::upsert_messages(&self.db, guild_id, std::slice::from_ref(d)).await?;
                if let (Some(channel), Some(id)) =
                    (snowflake(&d["channel_id"]), snowflake(&d["id"]))
                {
                    store::bump_last_message(&self.db, channel, id).await?;
                }
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
            }
            _ => {}
        }
        Ok(())
    }
}
