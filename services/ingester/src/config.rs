use std::{str::FromStr, time::Duration};

use anyhow::{Context, bail};
use twilight_model::id::{Id, marker::GuildMarker};

pub struct Config {
    pub database_url: String,
    pub port: u16,
    pub json_logs: bool,
}

/// Discord settings. Separate from [`Config`] because `ingester migrate` doesn't need them.
pub struct DiscordConfig {
    pub token: String,
    /// Guilds to ingest. Events from any other guild the bot is in are ignored.
    pub guild_ids: Vec<Id<GuildMarker>>,
    /// `DISCORD_API_PROXY` (`host:port`, plain HTTP) and `DISCORD_GATEWAY_URL`
    /// (`ws://…`): a fake Discord for local testing (`tools/sim`). Unset in production.
    pub api_proxy: Option<String>,
    pub gateway_url: Option<String>,
}

impl Config {
    pub fn from_env() -> anyhow::Result<Self> {
        Ok(Self {
            database_url: std::env::var("DATABASE_URL").context("DATABASE_URL is not set")?,
            port: match std::env::var("PORT") {
                Ok(p) => p.parse().context("PORT is not a valid port number")?,
                Err(_) => 8080,
            },
            json_logs: std::env::var("LOG_FORMAT").is_ok_and(|f| f.eq_ignore_ascii_case("json")),
        })
    }
}

impl DiscordConfig {
    pub fn from_env() -> anyhow::Result<Self> {
        let token = std::env::var("DISCORD_BOT_TOKEN").context("DISCORD_BOT_TOKEN is not set")?;
        let ids = std::env::var("DISCORD_GUILD_IDS").context("DISCORD_GUILD_IDS is not set")?;
        let guild_ids = ids
            .split(',')
            .map(str::trim)
            .filter(|s| !s.is_empty())
            .map(|s| {
                s.parse()
                    .with_context(|| format!("invalid guild id `{s}` in DISCORD_GUILD_IDS"))
            })
            .collect::<anyhow::Result<Vec<_>>>()?;
        if guild_ids.is_empty() {
            bail!("DISCORD_GUILD_IDS is empty");
        }
        let api_proxy = optional_env("DISCORD_API_PROXY");
        let gateway_url = optional_env("DISCORD_GATEWAY_URL");
        // Half a fake would send the rest of the traffic to real Discord.
        if api_proxy.is_some() != gateway_url.is_some() {
            bail!("set both DISCORD_API_PROXY and DISCORD_GATEWAY_URL, or neither");
        }
        Ok(Self {
            token: token.trim().to_owned(),
            guild_ids,
            api_proxy,
            gateway_url,
        })
    }
}

pub struct RouterConfig {
    /// `ROUTE_MAX_MESSAGE_AGE_MS`: messages older than this when classification
    /// starts are never classified (backfill, catch-up, late notifications).
    pub max_message_age: Duration,
    /// `POOL_DEBOUNCE_MS`: a pool is classified once its user is quiet this long.
    pub pool_debounce: Duration,
    /// `POOL_MAX_WAIT_MS`: ...or at the latest this long after its first message.
    pub pool_max_wait: Duration,
    /// `POOL_MAX_MESSAGES`: ...or as soon as it holds this many messages.
    pub pool_max_messages: usize,
    /// `ROUTE_KEYWORDS`: comma-separated words (case-insensitive, whole words)
    /// that send a message to Decide (check 2).
    pub keywords: Vec<String>,
    /// `OPENROUTER_DECISION_API_KEY`: OpenRouter key for Decide (Jev). Without
    /// it Decide always answers "no".
    pub jev_api_key: Option<String>,
    /// `OPENROUTER_DECISION_MODEL`: Jev model slug on OpenRouter.
    pub jev_model: String,
    /// `JEV_MIN_CONFIDENCE`: a Jev answer below this counts as "no".
    pub jev_min_confidence: f64,
    /// `JEV_TIMEOUT_MS`: a slower Jev answer counts as "no".
    pub jev_timeout: Duration,
    /// `ROUTE_PENDING_EMOJI`: reaction added to a message routed to Kiki, until
    /// she replies. Empty disables it.
    pub pending_emoji: String,
    /// `MAX_ACTIVE_CONVERSATIONS`: size of the active pool, across all channels.
    /// Opening one more closes the least recently active.
    pub max_active_conversations: i64,
    /// `ROUTE_ACTIVE_WINDOW_SECS`: check 3 (Decide on every message) only runs in
    /// channels with a conversation active this recently. Saves a Jev call per
    /// message in channels where Kiki talked long ago.
    pub active_window: Duration,
}

impl RouterConfig {
    pub fn from_env() -> anyhow::Result<Self> {
        Ok(Self {
            max_message_age: Duration::from_millis(env_or("ROUTE_MAX_MESSAGE_AGE_MS", 5000)?),
            pool_debounce: Duration::from_millis(env_or("POOL_DEBOUNCE_MS", 500)?),
            pool_max_wait: Duration::from_millis(env_or("POOL_MAX_WAIT_MS", 8000)?),
            pool_max_messages: env_or("POOL_MAX_MESSAGES", 10)?,
            keywords: std::env::var("ROUTE_KEYWORDS")
                .unwrap_or_else(|_| "kiki".into())
                .split(',')
                .map(|k| k.trim().to_lowercase())
                .filter(|k| !k.is_empty())
                .collect(),
            jev_api_key: optional_env("OPENROUTER_DECISION_API_KEY"),
            jev_model: std::env::var("OPENROUTER_DECISION_MODEL")
                .unwrap_or_else(|_| "~typesafe/jev-latest".into()),
            jev_min_confidence: env_or("JEV_MIN_CONFIDENCE", 0.5)?,
            jev_timeout: Duration::from_millis(env_or("JEV_TIMEOUT_MS", 5000)?),
            pending_emoji: std::env::var("ROUTE_PENDING_EMOJI")
                .map(|e| e.trim().to_owned())
                .unwrap_or_else(|_| "👀".into()),
            max_active_conversations: env_or("MAX_ACTIVE_CONVERSATIONS", 8)?,
            active_window: Duration::from_secs(env_or("ROUTE_ACTIVE_WINDOW_SECS", 3600)?),
        })
    }
}

pub struct AgentConfig {
    /// `AGENT_HOST_URL`: private HTTP base URL. Missing disables agent dispatch.
    pub host_url: Option<String>,
    /// `AGENT_HOST_TOKEN`: shared bearer token required when the host is enabled.
    pub host_token: Option<String>,
    /// Total deadline including cold-start retries and execution.
    pub host_timeout: Duration,
    pub chat_model: String,
    pub topic_model: String,
    /// `OPENROUTER_IMAGE_MODEL`: default model for the `generate_image` tool.
    pub image_model: String,
    /// `OPENROUTER_EMOJI_MODEL`: the vision model that describes custom emoji
    /// (`/kiki emojis resync:true`).
    pub emoji_model: String,
    /// `AGENT_HISTORY_MESSAGES`: conversation messages sent with each run.
    pub history_messages: i64,
    /// `AGENT_RUN_MAX_AGE_SECS`: runs queued longer ago than this get no reply
    /// (e.g. after an outage); a late answer is worse than none.
    pub run_max_age: Duration,
    /// `TOPIC_EVERY_MESSAGES`: the topic is re-summarized once this many messages
    /// arrived since the last summary.
    pub topic_every_messages: i64,
}

impl AgentConfig {
    pub fn from_env() -> anyhow::Result<Self> {
        let chat_model = std::env::var("OPENROUTER_CHAT_MODEL")
            .unwrap_or_else(|_| "deepseek/deepseek-v4.1-flash-1017".into());
        let host_url = optional_env("AGENT_HOST_URL");
        let host_token = optional_env("AGENT_HOST_TOKEN");
        if host_url.is_some() && host_token.is_none() {
            bail!("AGENT_HOST_TOKEN is required when AGENT_HOST_URL is set");
        }
        let timeout_ms = env_or("AGENT_HOST_TIMEOUT_MS", 180_000_u64)?;
        let history_messages = env_or("AGENT_HISTORY_MESSAGES", 30_i64)?;
        if timeout_ms == 0 || !(1..=190).contains(&history_messages) {
            bail!(
                "AGENT_HOST_TIMEOUT_MS must be positive and AGENT_HISTORY_MESSAGES must be 1..190"
            );
        }
        Ok(Self {
            host_url,
            host_token,
            host_timeout: Duration::from_millis(timeout_ms),
            topic_model: std::env::var("OPENROUTER_TOPIC_MODEL")
                .unwrap_or_else(|_| chat_model.clone()),
            emoji_model: optional_env("OPENROUTER_EMOJI_MODEL")
                .unwrap_or_else(|| "deepseek/deepseek-v4.1-flash-1017".into()),
            chat_model,
            image_model: std::env::var("OPENROUTER_IMAGE_MODEL")
                .unwrap_or_else(|_| "openai/gpt-image-2.5-flare".into()),
            history_messages,
            run_max_age: Duration::from_secs(env_or("AGENT_RUN_MAX_AGE_SECS", 120)?),
            topic_every_messages: env_or("TOPIC_EVERY_MESSAGES", 4)?,
        })
    }
}

/// `OPENROUTER_BASE_URL`: OpenRouter's API root. Overridden only by the local
/// simulator (`tools/sim`), which fakes Jev and the model catalog.
pub fn openrouter_base() -> String {
    optional_env("OPENROUTER_BASE_URL")
        .map(|url| url.trim_end_matches('/').to_owned())
        .unwrap_or_else(|| "https://openrouter.ai/api".into())
}

fn optional_env(name: &str) -> Option<String> {
    std::env::var(name)
        .ok()
        .map(|v| v.trim().to_owned())
        .filter(|v| !v.is_empty())
}

fn env_or<T: FromStr>(name: &str, default: T) -> anyhow::Result<T>
where
    T::Err: std::error::Error + Send + Sync + 'static,
{
    match std::env::var(name) {
        Ok(v) => v
            .trim()
            .parse()
            .with_context(|| format!("{name} is not valid")),
        Err(_) => Ok(default),
    }
}
