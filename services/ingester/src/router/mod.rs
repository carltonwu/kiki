//! Message routing: decides which new messages Kiki should respond to.
//!
//! ```text
//! messages INSERT ─NOTIFY→ pooler (per channel+user) → classify (serialized per channel) → tag → agent_runs
//! ```
//!
//! The router is driven by Postgres: a trigger on `messages` notifies
//! `message_inserted` for each newly stored message, so it sees exactly what was
//! saved, and edits (upserts) never re-route. Old messages (REST backfill and
//! catch-up) are never classified.

mod classify;

use std::{
    collections::{HashMap, HashSet},
    sync::{Arc, Mutex},
    time::{Duration, SystemTime, UNIX_EPOCH},
};

use anyhow::Context;
use serde::Deserialize;
use sqlx::{PgPool, postgres::PgListener};
use tokio::{sync::watch, time::Instant};
use tracing::{debug, info, warn};
use twilight_http::{Client, request::channel::reaction::RequestReactionType};
use twilight_model::id::{Id, marker::GuildMarker};

use crate::{catch_up::snowflake, config::RouterConfig, jev::Jev, rest::get_json};

const NOTIFY_CHANNEL: &str = "message_inserted";

pub struct Router {
    db: PgPool,
    http: Arc<Client>,
    config: RouterConfig,
    settings: Arc<crate::guild_settings::Store>,
    jev: Option<Jev>,
    guild_ids: HashSet<i64>,
    /// Kiki's user id, and her managed role in each guild: mentioning either is an @Kiki.
    bot_user_id: i64,
    bot_role_ids: HashSet<i64>,
    /// Classification runs one pool at a time per channel, so two pools can't both
    /// decide to start a conversation.
    channel_locks: Mutex<HashMap<i64, Arc<tokio::sync::Mutex<()>>>>,
}

#[derive(Deserialize)]
struct Inserted {
    id: i64,
    guild_id: i64,
    channel_id: i64,
    author_id: i64,
    author_bot: bool,
    created_at_ms: i64,
}

struct Pool {
    guild_id: i64,
    settings: Arc<crate::guild_settings::Resolved>,
    message_ids: Vec<i64>,
    first_at: Instant,
    deadline: Instant,
}

#[derive(Clone, Copy, PartialEq, Eq, Hash)]
struct PoolKey {
    channel_id: i64,
    user_id: i64,
}

impl Router {
    pub async fn new(
        db: PgPool,
        http: Arc<Client>,
        guild_ids: &[Id<GuildMarker>],
        config: RouterConfig,
        settings: Arc<crate::guild_settings::Store>,
    ) -> anyhow::Result<Self> {
        let me = get_json("current user", || http.current_user())
            .await?
            .context("can't read the bot's own user")?;
        let bot_user_id = snowflake(&me["id"]).context("bot user has no id")?;

        let mut bot_role_ids = HashSet::new();
        for &guild_id in guild_ids {
            let roles = get_json("guild roles", || http.roles(guild_id)).await?;
            for role in roles
                .as_ref()
                .and_then(|r| r.as_array())
                .into_iter()
                .flatten()
            {
                if snowflake(&role["tags"]["bot_id"]) == Some(bot_user_id) {
                    bot_role_ids.extend(snowflake(&role["id"]));
                }
            }
        }

        let jev = match &config.jev_api_key {
            Some(key) => Some(Jev::new(
                key.clone(),
                config.jev_model.clone(),
                config.jev_timeout,
            )?),
            None => {
                warn!(
                    "OPENROUTER_DECISION_API_KEY is not set: Decide answers \"no\" to every keyword or active-conversation match"
                );
                None
            }
        };
        info!(
            bot_user_id,
            bot_roles = bot_role_ids.len(),
            decide_model = config.jev_model,
            "router ready"
        );
        Ok(Self {
            db,
            http,
            config,
            settings,
            jev,
            guild_ids: guild_ids.iter().map(|id| id.get() as i64).collect(),
            bot_user_id,
            bot_role_ids,
            channel_locks: Mutex::default(),
        })
    }

    pub fn bot_user_id(&self) -> i64 {
        self.bot_user_id
    }

    /// Listens for new messages until shutdown. Pools not yet classified at
    /// shutdown are dropped; losing a few seconds of routing on restart is acceptable.
    pub async fn run(self: Arc<Self>, mut shutdown: watch::Receiver<bool>) -> anyhow::Result<()> {
        let mut listener = PgListener::connect_with(&self.db).await?;
        listener.listen(NOTIFY_CHANNEL).await?;
        info!("router listening for new messages");

        let mut pools: HashMap<PoolKey, Pool> = HashMap::new();
        loop {
            let next_deadline = pools.values().map(|p| p.deadline).min();
            tokio::select! {
                notification = listener.recv() => match notification {
                    Ok(n) => {
                        if let Some(full) = self.admit(n.payload(), &mut pools) {
                            self.clone().spawn_classify(full);
                        }
                    }
                    // PgListener reconnects on the next recv; notifications sent
                    // meanwhile are lost, and would be too old to classify anyway.
                    Err(e) => {
                        warn!(error = %e, "router lost its Postgres listener; reconnecting");
                        tokio::time::sleep(Duration::from_secs(1)).await;
                    }
                },
                () = sleep_until(next_deadline) => {
                    let now = Instant::now();
                    let due: Vec<PoolKey> =
                        pools.iter().filter(|(_, p)| p.deadline <= now).map(|(k, _)| *k).collect();
                    for key in due {
                        let pool = pools.remove(&key).expect("key from the map");
                        self.clone().spawn_classify((key, pool));
                    }
                }
                _ = async { shutdown.wait_for(|stop| *stop).await.map(drop) } => break,
            }
        }
        info!(dropped_pools = pools.len(), "router stopped");
        Ok(())
    }

    fn admit(
        self: &Arc<Self>,
        payload: &str,
        pools: &mut HashMap<PoolKey, Pool>,
    ) -> Option<(PoolKey, Pool)> {
        let msg: Inserted = match serde_json::from_str(payload) {
            Ok(m) => m,
            Err(e) => {
                warn!(error = %e, payload, "unparseable message notification");
                return None;
            }
        };
        // Kiki's own replies are tagged by the agent that sends them; other bots are ignored.
        if msg.author_bot || !self.guild_ids.contains(&msg.guild_id) {
            return None;
        }
        let age_ms = unix_millis() - msg.created_at_ms;
        if age_ms > self.config.max_message_age.as_millis() as i64 {
            debug!(message_id = msg.id, age_ms, "not routing an old message");
            return None;
        }
        // Stored and read as context, but Kiki has nowhere to reply from here.
        let settings = self.settings.snapshot(msg.guild_id);
        if crate::guild_settings::reply_channel(&settings, msg.channel_id).is_none() {
            debug!(
                message_id = msg.id,
                channel_id = msg.channel_id,
                "not routing: channel not allowed and no default channel"
            );
            return None;
        }

        let router = self.clone();
        tokio::spawn(async move {
            router
                .react_if_explicit(msg.guild_id, msg.channel_id, msg.id)
                .await
        });

        let key = PoolKey {
            channel_id: msg.channel_id,
            user_id: msg.author_id,
        };
        let now = Instant::now();
        let pool = pools.entry(key).or_insert_with(|| Pool {
            guild_id: msg.guild_id,
            settings,
            message_ids: Vec::new(),
            first_at: now,
            deadline: now,
        });
        pool.message_ids.push(msg.id);
        pool.deadline = (now + Duration::from_millis(pool.settings.pool_debounce))
            .min(pool.first_at + Duration::from_millis(pool.settings.pool_max_wait));
        if pool.message_ids.len() >= pool.settings.pool_max_messages {
            return pools.remove_entry(&key);
        }
        None
    }

    fn spawn_classify(self: Arc<Self>, (key, pool): (PoolKey, Pool)) {
        // Classification time: when the pool closed, before waiting for the channel.
        let classified_at = SystemTime::now();
        tokio::spawn(async move {
            let lock = self.channel_lock(key.channel_id);
            let _guard = lock.lock().await;
            if let Err(e) = self.classify(key, &pool, classified_at).await {
                warn!(
                    channel_id = key.channel_id,
                    message_ids = ?pool.message_ids,
                    error = format!("{e:#}"),
                    "classification failed"
                );
            }
        });
    }

    /// Reacts to a routed message so it's visible that Kiki was triggered and a
    /// reply is pending. Runs in the background; failures are only logged.
    fn react_pending(&self, channel_id: i64, message_id: i64) {
        if self.config.pending_emoji.is_empty() {
            return;
        }
        let http = self.http.clone();
        let emoji = self.config.pending_emoji.clone();
        tokio::spawn(async move {
            let reaction = RequestReactionType::Unicode { name: &emoji };
            let request = http.create_reaction(
                Id::new(channel_id as u64),
                Id::new(message_id as u64),
                &reaction,
            );
            if let Err(e) = request.await {
                warn!(channel_id, message_id, error = %e, "couldn't add the pending reaction");
            }
        });
    }

    fn channel_lock(&self, channel_id: i64) -> Arc<tokio::sync::Mutex<()>> {
        let mut locks = self
            .channel_locks
            .lock()
            .expect("channel lock map poisoned");
        locks.entry(channel_id).or_default().clone()
    }
}

async fn sleep_until(deadline: Option<Instant>) {
    match deadline {
        Some(deadline) => tokio::time::sleep_until(deadline).await,
        None => std::future::pending().await,
    }
}

pub(crate) fn unix_millis() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis() as i64
}
