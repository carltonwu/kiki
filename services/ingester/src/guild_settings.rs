//! Server overrides and immutable work snapshots. The leader is the sole writer;
//! Discord writes publish to this cache after committing, with no routing I/O.
use std::{
    collections::{BTreeMap, HashMap},
    sync::{Arc, RwLock},
};

use crate::{
    config::{AgentConfig, RouterConfig},
    conversation,
};
use anyhow::{Context, bail, ensure};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sqlx::{PgExecutor, PgPool};

#[derive(Clone, Copy)]
pub enum Kind {
    Bool,
    Integer(u64, u64),
    Float(f64, f64),
    /// Native unit in milliseconds, lower and upper bounds in native units.
    Duration(u64, u64, u64),
    Keywords,
    Model,
    /// Channel or thread IDs; empty means every channel.
    Channels,
    /// One channel or thread ID, or none.
    Channel,
}

/// Most channels a server can allowlist.
pub const MAX_CHANNELS: usize = 50;

pub struct Spec {
    pub key: &'static str,
    pub kind: Kind,
    pub description: &'static str,
}

macro_rules! settings {
    ($r:ident, $a:ident; $( $key:ident: $ty:ty = $default:expr, $kind:expr, $description:literal; )*) => {
        #[derive(Clone, Debug, Serialize, Deserialize)]
        pub struct GuildSettings { $(pub $key: $ty,)* }
        impl GuildSettings {
            pub fn defaults($r: &RouterConfig, $a: &AgentConfig) -> Self {
                Self { $($key: $default,)* }
            }
        }
        pub static SPECS: &[Spec] = &[$(Spec {key: stringify!($key), kind: $kind, description: $description},)*];
    }
}

settings! { r, a;
    chat_model: String = a.chat_model.clone(), Kind::Model, "OpenRouter text model with tools, or refuseless/<id>";
    image_generation: bool = true, Kind::Bool, "Let Kiki draw images and attach them to replies";
    image_model: String = a.image_model.clone(), Kind::Model, "OpenRouter model that outputs images";
    keywords: Vec<String> = r.keywords.clone(), Kind::Keywords, "Comma-separated trigger words; empty disables";
    pool_debounce: u64 = r.pool_debounce.as_millis() as u64, Kind::Duration(1, 0, 30_000), "Quiet time before routing (ms)";
    pool_max_wait: u64 = r.pool_max_wait.as_millis() as u64, Kind::Duration(1, 0, 30_000), "Longest pool wait (ms)";
    pool_max_messages: usize = r.pool_max_messages, Kind::Integer(1, 50), "Messages before routing a pool";
    active_window: u64 = r.active_window.as_secs(), Kind::Duration(1000, 0, 86_400), "Decide on active-channel messages (seconds)";
    max_active_conversations: i64 = r.max_active_conversations, Kind::Integer(1, 8), "Open conversations in this server";
    decide_min_confidence: f64 = r.jev_min_confidence, Kind::Float(0.0, 1.0), "Minimum Decide confidence";
    decide_keyword_min_confidence: f64 = 0.25, Kind::Float(0.0, 1.0), "Minimum Decide confidence when Kiki is named";
    follow_up_window: u64 = 300, Kind::Duration(1000, 0, 3600), "After Kiki answers someone, their follow-ups to her continue without (or despite an unsure) Decide (seconds; 0 disables)";
    follow_up_max_no_confidence: f64 = 0.55, Kind::Float(0.0, 1.0), "Decide must be this sure to ignore such a follow-up";
    stale_after: u64 = 1800, Kind::Duration(1000, 60, 86_400), "Idle time before a conversation is stale (seconds)";
    stale_min_confidence: f64 = 0.9, Kind::Float(0.0, 1.0), "Confidence to continue a stale conversation";
    decide_recent_messages: i64 = 15, Kind::Integer(1, 50), "Recent channel messages shown to Decide";
    decide_conversation_preview: i64 = 3, Kind::Integer(0, 10), "Preview messages per Decide conversation";
    history_messages: i64 = a.history_messages, Kind::Integer(1, 190), "Conversation messages shown per reply";
    channel_context: i64 = 8, Kind::Integer(0, 30), "Background channel messages per reply";
    channel_context_age: u64 = 1800, Kind::Duration(1000, 0, 86_400), "Maximum background message age (seconds)";
    chat_min_messages: usize = 1, Kind::Integer(1, 8), "Preferred minimum messages per turn";
    chat_max_messages: usize = 3, Kind::Integer(1, 8), "Maximum planned messages per turn (Discord length splits may add more)";
    chat_max_delay: u64 = 3000, Kind::Duration(1, 0, 10_000), "Maximum pause between messages (ms; total capped at 8s)";
    chat_allow_silence: bool = true, Kind::Bool, "Allow Kiki to sit out when a reply would add little";
    chat_reply_reference: bool = false, Kind::Bool, "Attach and ping a Discord reply on the first message";
    chat_reactions: bool = true, Kind::Bool, "Let Kiki react to messages with emoji, alone or with a reply";
    status_reactions: bool = true, Kind::Bool, "Show routing and pending status reactions";
    reply_max_tokens: u32 = 2000, Kind::Integer(100, 4000), "Output tokens per model request";
    topic_every_messages: i64 = a.topic_every_messages, Kind::Integer(1, 50), "New messages before refreshing a topic";
    topic_sample_messages: i64 = 12, Kind::Integer(4, 50), "Messages shown to the topic summarizer";
    lingo_nudge: i64 = 35, Kind::Integer(0, 100), "Chance (%) a reply is nudged to use one of the server's phrases";
    allowed_channels: Vec<i64> = vec![], Kind::Channels, "Channels Kiki replies in; empty allows every channel. Threads are listed separately";
    default_channel: Option<i64> = None, Kind::Channel, "Where Kiki replies when called from another channel; unset means she stays quiet there";
}

/// Where Kiki's reply to a message in `channel_id` goes: the channel itself
/// when allowed, else the default channel, else nowhere. The allowlist covers
/// replies only; every channel is still stored and read as context.
pub fn reply_channel(settings: &GuildSettings, channel_id: i64) -> Option<i64> {
    if settings.allowed_channels.is_empty() || settings.allowed_channels.contains(&channel_id) {
        Some(channel_id)
    } else {
        settings.default_channel
    }
}

type Overrides = BTreeMap<String, Value>;

#[derive(Debug)]
pub struct Resolved {
    pub values: GuildSettings,
    pub overrides: Overrides,
    pub ignored: Vec<String>,
}

impl std::ops::Deref for Resolved {
    type Target = GuildSettings;
    fn deref(&self) -> &Self::Target {
        &self.values
    }
}

pub fn spec(key: &str) -> anyhow::Result<&'static Spec> {
    SPECS
        .iter()
        .find(|s| s.key == key)
        .context("Unknown server setting")
}

impl Spec {
    pub fn bounds(&self) -> String {
        match self.kind {
            Kind::Bool => "true/false".into(),
            Kind::Integer(min, max) => format!("{min}–{max}"),
            Kind::Float(min, max) => format!("{min}–{max}"),
            Kind::Duration(unit, min, max) => {
                format!("{min}–{max} {}", if unit == 1 { "ms" } else { "s" })
            }
            Kind::Keywords => "0–20 words, ≤32 characters each".into(),
            Kind::Model => "provider/model, ≤200 bytes".into(),
            Kind::Channels => format!("0–{MAX_CHANNELS} channel mentions or IDs"),
            Kind::Channel => "one channel mention or ID, on allowed_channels".into(),
        }
    }

    pub fn parse(&self, text: &str) -> anyhow::Result<Value> {
        let text = text.trim();
        let value = match self.kind {
            Kind::Bool => json!(match text.to_ascii_lowercase().as_str() {
                "true" | "on" | "yes" => true,
                "false" | "off" | "no" => false,
                _ => bail!("Use true/false, on/off or yes/no"),
            }),
            Kind::Integer(..) => json!(text.parse::<u64>().context("Use a whole number")?),
            Kind::Float(..) => {
                let n: f64 = text.parse().context("Use a number")?;
                ensure!(n.is_finite(), "Use a finite number");
                json!(n)
            }
            Kind::Duration(unit, ..) => {
                let (number, multiplier) =
                    [("ms", 1_u64), ("s", 1000), ("m", 60_000), ("h", 3_600_000)]
                        .into_iter()
                        .find_map(|(suffix, multiplier)| {
                            text.strip_suffix(suffix).map(|n| (n, multiplier))
                        })
                        .unwrap_or((text, unit));
                let millis = number
                    .trim()
                    .parse::<u64>()
                    .context("Use a whole duration, such as 500ms, 8s, 30m or 1h")?
                    .checked_mul(multiplier)
                    .context("Duration is too large")?;
                ensure!(
                    millis.is_multiple_of(unit),
                    "Duration must use whole native units"
                );
                json!(millis / unit)
            }
            Kind::Keywords => json!(
                text.split(',')
                    .map(|s| s.trim().to_lowercase())
                    .filter(|s| !s.is_empty())
                    .collect::<Vec<_>>()
            ),
            Kind::Model => json!(text),
            Kind::Channels => {
                let mut ids = Vec::new();
                for part in text.split([',', ' ']).filter(|s| !s.is_empty()) {
                    let id = channel_id(part)?;
                    if !ids.contains(&id) {
                        ids.push(id);
                    }
                }
                json!(ids)
            }
            Kind::Channel => json!(channel_id(text)?),
        };
        self.validate(&value)?;
        Ok(value)
    }

    fn validate(&self, value: &Value) -> anyhow::Result<()> {
        let valid = match self.kind {
            Kind::Bool => value.is_boolean(),
            Kind::Integer(min, max) | Kind::Duration(_, min, max) => {
                value.as_u64().is_some_and(|n| (min..=max).contains(&n))
            }
            Kind::Float(min, max) => value
                .as_f64()
                .is_some_and(|n| n.is_finite() && (min..=max).contains(&n)),
            Kind::Keywords => value.as_array().is_some_and(|words| {
                words.len() <= 20
                    && words.iter().all(|w| {
                        w.as_str().is_some_and(|w| {
                            !w.is_empty()
                                && w.chars().count() <= 32
                                && w.chars().all(char::is_alphanumeric)
                        })
                    })
            }),
            Kind::Model => value.as_str().is_some_and(valid_model_id),
            Kind::Channels => value.as_array().is_some_and(|ids| {
                ids.len() <= MAX_CHANNELS
                    && ids.iter().all(|id| id.as_i64().is_some_and(|id| id > 0))
            }),
            Kind::Channel => value.is_null() || value.as_i64().is_some_and(|id| id > 0),
        };
        ensure!(valid, "{} must be {}", self.key, self.bounds());
        Ok(())
    }
}

/// `<#123>` (how Discord writes a channel picked in a text option) or `123`.
fn channel_id(text: &str) -> anyhow::Result<i64> {
    let text = text.trim();
    let id = text
        .strip_prefix("<#")
        .and_then(|t| t.strip_suffix('>'))
        .unwrap_or(text);
    id.parse::<i64>()
        .ok()
        .filter(|&id| id > 0)
        .with_context(|| format!("{text} isn't a channel; mention it with # or use its ID"))
}

pub fn valid_model_id(model: &str) -> bool {
    model.len() <= 200
        && model
            .split_once('/')
            .is_some_and(|(p, n)| !p.is_empty() && !n.is_empty())
        && model
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || b"/-_.:~".contains(&c))
}

/// An empty allowlist allows every channel, the default included.
fn default_allowed(values: &GuildSettings) -> bool {
    values.allowed_channels.is_empty()
        || values
            .default_channel
            .is_none_or(|id| values.allowed_channels.contains(&id))
}

pub struct Store {
    db: PgPool,
    defaults: Arc<Resolved>,
    max_message_age_ms: u64,
    cache: RwLock<HashMap<i64, Arc<Resolved>>>,
    /// Orders commit and publication, including two simultaneous edits.
    writer: tokio::sync::Mutex<()>,
}

impl Store {
    pub fn new(
        db: PgPool,
        defaults: GuildSettings,
        max_message_age_ms: u64,
    ) -> anyhow::Result<Self> {
        let store = Self {
            db,
            defaults: Arc::new(Resolved {
                values: defaults,
                overrides: Overrides::new(),
                ignored: vec![],
            }),
            max_message_age_ms,
            cache: RwLock::default(),
            writer: tokio::sync::Mutex::new(()),
        };
        store
            .validate(&store.defaults.values)
            .context("Invalid server defaults from environment")?;
        Ok(store)
    }

    pub async fn load(&self, guild_ids: &[i64]) -> anyhow::Result<()> {
        for &guild_id in guild_ids {
            let resolved = self.read(&self.db, guild_id).await?;
            if !resolved.ignored.is_empty() {
                tracing::warn!(guild_id, ignored = ?resolved.ignored, "invalid server overrides ignored");
            }
            self.cache
                .write()
                .expect("settings cache poisoned")
                .insert(guild_id, Arc::new(resolved));
        }
        Ok(())
    }

    pub fn snapshot(&self, guild_id: i64) -> Arc<Resolved> {
        self.cache
            .read()
            .expect("settings cache poisoned")
            .get(&guild_id)
            .cloned()
            .unwrap_or_else(|| self.defaults.clone())
    }

    pub fn default_values(&self) -> &GuildSettings {
        &self.defaults.values
    }

    fn validate(&self, values: &GuildSettings) -> anyhow::Result<()> {
        let encoded = serde_json::to_value(values)?;
        for s in SPECS {
            s.validate(&encoded[s.key])?;
        }
        ensure!(
            values.pool_debounce.saturating_add(2000) <= self.max_message_age_ms,
            "pool_debounce + 2s must not exceed global ROUTE_MAX_MESSAGE_AGE_MS"
        );
        ensure!(
            values.pool_max_wait >= values.pool_debounce,
            "pool_max_wait must be at least pool_debounce"
        );
        ensure!(
            values.stale_min_confidence >= values.decide_min_confidence,
            "stale_min_confidence must be at least decide_min_confidence"
        );
        ensure!(
            values.chat_min_messages <= values.chat_max_messages,
            "chat_min_messages must not exceed chat_max_messages"
        );
        ensure!(
            default_allowed(values),
            "default_channel must be on allowed_channels: add it there first, or reset default_channel before removing it"
        );
        Ok(())
    }

    fn merge(&self, overrides: &Overrides) -> anyhow::Result<GuildSettings> {
        let mut encoded = serde_json::to_value(&self.defaults.values)?;
        for (key, value) in overrides {
            spec(key)?.validate(value)?;
            encoded[key] = value.clone();
        }
        let values = serde_json::from_value(encoded)?;
        self.validate(&values)?;
        Ok(values)
    }

    /// Invalid rows never prevent routing after an operator changes defaults.
    /// Drop invalid individual keys, then the participating cross-field overrides.
    fn resolve(&self, mut overrides: Overrides) -> Resolved {
        let mut ignored = vec![];
        overrides.retain(|key, value| {
            let valid = spec(key).and_then(|s| s.validate(value)).is_ok();
            if !valid {
                ignored.push(key.clone());
            }
            valid
        });
        if self.merge(&overrides).is_err() {
            for group in [
                ["pool_debounce", "pool_max_wait"],
                ["decide_min_confidence", "stale_min_confidence"],
                ["chat_min_messages", "chat_max_messages"],
                ["allowed_channels", "default_channel"],
            ] {
                // Resolve each independent constraint group using defaults for the other.
                let encoded = |key: &str| {
                    overrides.get(key).cloned().unwrap_or_else(|| {
                        serde_json::to_value(&self.defaults.values).unwrap()[key].clone()
                    })
                };
                let invalid = match group[0] {
                    "pool_debounce" => {
                        let debounce = encoded("pool_debounce").as_u64().unwrap();
                        debounce.saturating_add(2000) > self.max_message_age_ms
                            || encoded("pool_max_wait").as_u64().unwrap() < debounce
                    }
                    "chat_min_messages" => {
                        encoded("chat_min_messages").as_u64().unwrap()
                            > encoded("chat_max_messages").as_u64().unwrap()
                    }
                    "allowed_channels" => {
                        let allowed = encoded("allowed_channels");
                        let allowed = allowed.as_array().unwrap();
                        let default = encoded("default_channel");
                        !allowed.is_empty() && !default.is_null() && !allowed.contains(&default)
                    }
                    "decide_min_confidence" => {
                        encoded("stale_min_confidence").as_f64().unwrap()
                            < encoded("decide_min_confidence").as_f64().unwrap()
                    }
                    _ => unreachable!("known constraint group"),
                };
                if invalid {
                    for key in group {
                        if overrides.remove(key).is_some() {
                            ignored.push(key.to_owned());
                        }
                    }
                }
            }
        }
        let values = self
            .merge(&overrides)
            .expect("validated defaults and sanitized overrides");
        Resolved {
            values,
            overrides,
            ignored,
        }
    }

    async fn read(&self, db: impl PgExecutor<'_>, guild_id: i64) -> sqlx::Result<Resolved> {
        let rows: Vec<(String, Value)> =
            sqlx::query_as("SELECT key, value FROM guild_settings WHERE guild_id = $1")
                .bind(guild_id)
                .fetch_all(db)
                .await?;
        Ok(self.resolve(rows.into_iter().collect()))
    }

    /// Caller holds conversation::lock_pool. Read the committed limit here rather
    /// than a work snapshot: a setting shrink must also constrain waiting tags.
    pub async fn active_limit(&self, db: impl PgExecutor<'_>, guild_id: i64) -> sqlx::Result<i64> {
        Ok(self.read(db, guild_id).await?.max_active_conversations)
    }

    pub async fn change(
        &self,
        guild_id: i64,
        key: &str,
        value: Option<Value>,
        actor: i64,
    ) -> anyhow::Result<()> {
        spec(key)?;
        let _writer = self.writer.lock().await;
        let mut tx = self.db.begin().await?;
        conversation::lock_pool(&mut tx).await?;
        let mut current = self.read(&mut *tx, guild_id).await?.overrides;
        match &value {
            Some(v) => {
                current.insert(key.to_owned(), v.clone());
            }
            None => {
                current.remove(key);
            }
        }
        let values = self.merge(&current)?;
        match value {
            Some(value) => {
                sqlx::query("INSERT INTO guild_settings (guild_id, key, value, updated_by) VALUES ($1, $2, $3, $4) ON CONFLICT (guild_id, key) DO UPDATE SET value = EXCLUDED.value, updated_by = EXCLUDED.updated_by, updated_at = now()")
                    .bind(guild_id).bind(key).bind(value).bind(actor).execute(&mut *tx).await?;
            }
            None => {
                sqlx::query("DELETE FROM guild_settings WHERE guild_id = $1 AND key = $2")
                    .bind(guild_id)
                    .bind(key)
                    .execute(&mut *tx)
                    .await?;
            }
        }
        let evicted =
            conversation::evict_beyond(&mut tx, guild_id, values.max_active_conversations).await?;
        let resolved = self.read(&mut *tx, guild_id).await?;
        // Re-resolution must not discard the proposed change to satisfy a constraint.
        if resolved.ignored.iter().any(|k| k == key) {
            bail!(
                "Setting conflicts with another server override; reset the conflicting key first"
            );
        }
        tx.commit().await?;
        crate::metrics::conversations_closed("evicted", evicted.iter().map(|&(_, n)| n));
        self.cache
            .write()
            .expect("settings cache poisoned")
            .insert(guild_id, Arc::new(resolved));
        Ok(())
    }
}

#[cfg(test)]
pub mod tests {
    use super::*;
    use sqlx::{Executor, postgres::PgConnectOptions};
    use std::time::{SystemTime, UNIX_EPOCH};

    pub fn defaults() -> GuildSettings {
        serde_json::from_value(json!({
            "chat_model": "test/default", "image_generation": true, "image_model": "test/image",
            "keywords": ["kiki"],
            "pool_debounce": 500, "pool_max_wait": 8000, "pool_max_messages": 10,
            "active_window": 3600, "max_active_conversations": 8,
            "decide_min_confidence": 0.5, "decide_keyword_min_confidence": 0.25,
            "follow_up_window": 300, "follow_up_max_no_confidence": 0.55, "stale_after": 1800, "stale_min_confidence": 0.9,
            "decide_recent_messages": 15, "decide_conversation_preview": 3,
            "history_messages": 30, "channel_context": 8, "channel_context_age": 1800,
            "reply_max_tokens": 2000, "topic_every_messages": 4, "topic_sample_messages": 12,
            "lingo_nudge": 35, "chat_min_messages": 1, "chat_max_messages": 3,
            "chat_max_delay": 3000, "chat_allow_silence": true,
            "chat_reply_reference": false, "chat_reactions": true, "status_reactions": true,
            "allowed_channels": [], "default_channel": null
        }))
        .unwrap()
    }

    fn store() -> Store {
        Store::new(
            PgPool::connect_lazy("postgres://unused:unused@localhost/unused").unwrap(),
            defaults(),
            5000,
        )
        .unwrap()
    }

    #[test]
    fn parses_units_keywords_models_and_rejects_invalid_values() {
        for (key, text, expected) in [
            ("pool_debounce", "2s", json!(2000)),
            ("status_reactions", "off", json!(false)),
            ("chat_allow_silence", "yes", json!(true)),
            ("pool_debounce", "500", json!(500)),
            ("active_window", "30m", json!(1800)),
            ("channel_context_age", "1h", json!(3600)),
            ("keywords", " KIKI, Héllo ", json!(["kiki", "héllo"])),
            ("keywords", "", json!([])),
            ("reply_max_tokens", "3000", json!(3000)),
            ("allowed_channels", "<#12>, 34 <#12>", json!([12, 34])),
            ("allowed_channels", "", json!([])),
            ("default_channel", " <#12> ", json!(12)),
        ] {
            assert_eq!(spec(key).unwrap().parse(text).unwrap(), expected);
        }
        for (key, text) in [
            ("pool_max_messages", "0"),
            ("status_reactions", "maybe"),
            ("chat_max_messages", "9"),
            ("history_messages", "191"),
            ("decide_min_confidence", "NaN"),
            ("stale_min_confidence", "1.1"),
            ("active_window", "500ms"),
            ("pool_debounce", "18446744073709551615h"),
            ("keywords", "hi-there"),
            ("chat_model", "bad model"),
            ("allowed_channels", "#general"),
            ("allowed_channels", "<@12>"),
            ("default_channel", "12, 34"),
            ("default_channel", ""),
            ("default_channel", "0"),
        ] {
            assert!(spec(key).unwrap().parse(text).is_err(), "{key}: {text}");
        }
        assert!(spec("secret").is_err());
    }

    #[test]
    fn replies_in_allowed_channels_else_the_default_else_nowhere() {
        let mut settings = defaults();
        assert_eq!(reply_channel(&settings, 7), Some(7));
        settings.allowed_channels = vec![5, 6];
        assert_eq!(reply_channel(&settings, 6), Some(6));
        assert_eq!(reply_channel(&settings, 7), None);
        settings.default_channel = Some(5);
        assert_eq!(reply_channel(&settings, 7), Some(5));
    }

    #[tokio::test]
    async fn validates_cross_fields_and_falls_back_only_for_affected_overrides() {
        let store = store();
        let overrides: Overrides = [
            ("pool_debounce".into(), json!(3001)),
            ("history_messages".into(), json!(10)),
            ("stale_min_confidence".into(), json!(0.2)),
            ("unknown".into(), json!(1)),
        ]
        .into();
        assert!(store.merge(&overrides).is_err());
        let resolved = store.resolve(overrides);
        assert_eq!(resolved.pool_debounce, 500);
        assert_eq!(resolved.history_messages, 10);
        assert_eq!(resolved.stale_min_confidence, 0.9);
        assert_eq!(resolved.ignored.len(), 3);
        for overrides in [
            [("pool_max_wait".into(), json!(499))].into(),
            [("chat_min_messages".into(), json!(4))].into(),
            [("decide_min_confidence".into(), json!(0.95))].into(),
        ] {
            assert!(store.merge(&overrides).is_err());
            assert!(store.resolve(overrides).ignored.len() == 1);
        }
        // The default channel must stay on a non-empty allowlist.
        for (allowed, default, valid) in [
            (json!([]), json!(5), true),
            (json!([5, 6]), json!(5), true),
            (json!([6]), json!(null), true),
            (json!([6]), json!(5), false),
        ] {
            let overrides: Overrides = [
                ("allowed_channels".into(), allowed),
                ("default_channel".into(), default),
            ]
            .into();
            assert_eq!(store.merge(&overrides).is_ok(), valid);
            assert_eq!(
                store.resolve(overrides).ignored.len(),
                if valid { 0 } else { 2 }
            );
        }
        let mut invalid = defaults();
        invalid.history_messages = 0;
        assert!(Store::new(store.db.clone(), invalid, 5000).is_err());
    }

    async fn database() -> Option<(PgPool, PgPool, String)> {
        let url = std::env::var("TEST_DATABASE_URL").ok()?;
        let schema = format!(
            "guild_settings_test_{}",
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
        // Avoid pgvector: settings have no ingestion/retrieval dependencies.
        sqlx::raw_sql(include_str!("../migrations/0011_guild_chat_models.sql"))
            .execute(&db)
            .await
            .unwrap();
        sqlx::query("INSERT INTO guild_chat_models (guild_id, model, updated_by) VALUES (1, 'test/migrated', 42)").execute(&db).await.unwrap();
        sqlx::raw_sql(include_str!("../migrations/0012_guild_settings.sql"))
            .execute(&db)
            .await
            .unwrap();
        sqlx::raw_sql("CREATE TABLE conversations (id BIGSERIAL PRIMARY KEY, guild_id BIGINT NOT NULL, message_count INTEGER NOT NULL DEFAULT 0, last_active_at TIMESTAMPTZ NOT NULL DEFAULT now(), closed_at TIMESTAMPTZ, closed_reason TEXT)").execute(&db).await.unwrap();
        Some((admin, db, schema))
    }

    async fn cleanup(admin: PgPool, db: PgPool, schema: String) {
        db.close().await;
        admin
            .execute(format!("DROP SCHEMA {schema} CASCADE").as_str())
            .await
            .unwrap();
    }

    #[tokio::test]
    async fn migration_persistence_reset_guild_isolation_and_snapshot_publication() {
        let Some((admin, db, schema)) = database().await else {
            return;
        };
        let store = Store::new(db.clone(), defaults(), 5000).unwrap();
        store.load(&[1, 2]).await.unwrap();
        assert_eq!(store.snapshot(1).chat_model, "test/migrated");
        let metadata: (i64, bool) = sqlx::query_as("SELECT s.updated_by, s.updated_at = m.updated_at FROM guild_settings s JOIN guild_chat_models m USING (guild_id)").fetch_one(&db).await.unwrap();
        assert_eq!(metadata, (42, true));
        let old_pool = store.snapshot(1);
        store
            .change(1, "pool_max_messages", Some(json!(3)), 99)
            .await
            .unwrap();
        store
            .change(2, "chat_model", Some(json!("test/other")), 98)
            .await
            .unwrap();
        assert_eq!(old_pool.pool_max_messages, 10);
        assert_eq!(store.snapshot(1).pool_max_messages, 3);
        assert_eq!(store.snapshot(2).pool_max_messages, 10);
        assert!(
            store
                .change(1, "pool_max_wait", Some(json!(100)), 99)
                .await
                .is_err()
        );
        assert_eq!(store.snapshot(1).pool_max_wait, 8000);
        drop(store);
        let mut new_defaults = defaults();
        new_defaults.chat_model = "test/new-default".into();
        let store = Store::new(db.clone(), new_defaults, 5000).unwrap();
        store.load(&[1, 2]).await.unwrap();
        assert_eq!(store.snapshot(1).pool_max_messages, 3);
        store.change(1, "chat_model", None, 99).await.unwrap();
        assert_eq!(store.snapshot(1).chat_model, "test/new-default");
        assert_eq!(store.snapshot(2).chat_model, "test/other");
        let actor: i64 = sqlx::query_scalar(
            "SELECT updated_by FROM guild_settings WHERE guild_id=1 AND key='pool_max_messages'",
        )
        .fetch_one(&db)
        .await
        .unwrap();
        assert_eq!(actor, 99);
        cleanup(admin, db, schema).await;
    }

    #[tokio::test]
    async fn concurrent_edits_validate_the_whole_config_and_publish_in_order() {
        let Some((admin, db, schema)) = database().await else {
            return;
        };
        let store = Store::new(db.clone(), defaults(), 5000).unwrap();
        let (a, b) = tokio::join!(
            store.change(1, "pool_debounce", Some(json!(2000)), 1),
            store.change(1, "pool_max_wait", Some(json!(1000)), 2)
        );
        assert_ne!(a.is_ok(), b.is_ok());
        let disk = store.read(&db, 1).await.unwrap();
        assert_eq!(store.snapshot(1).overrides, disk.overrides);
        assert!(disk.ignored.is_empty());
        cleanup(admin, db, schema).await;
    }

    #[tokio::test]
    async fn shrinking_evicts_only_this_guild_and_waiting_tags_read_the_new_limit() {
        let Some((admin, db, schema)) = database().await else {
            return;
        };
        sqlx::query("INSERT INTO conversations (guild_id) VALUES (1), (1), (1), (2), (2), (2)")
            .execute(&db)
            .await
            .unwrap();
        let store = Arc::new(Store::new(db.clone(), defaults(), 5000).unwrap());
        let old_pool = store.snapshot(1);
        // A classification currently holds the pool lock; the shrink must wait.
        let mut tagging = db.begin().await.unwrap();
        conversation::lock_pool(&mut tagging).await.unwrap();
        let changing = store.clone();
        let shrink = tokio::spawn(async move {
            changing
                .change(1, "max_active_conversations", Some(json!(1)), 42)
                .await
        });
        conversation::evict_beyond(&mut tagging, 1, 8)
            .await
            .unwrap();
        tagging.commit().await.unwrap();
        shrink.await.unwrap().unwrap();
        assert_eq!(old_pool.max_active_conversations, 8);
        // A tag created before shrink now acquires the lock and revives a row.
        let mut waiting_tag = db.begin().await.unwrap();
        conversation::lock_pool(&mut waiting_tag).await.unwrap();
        sqlx::query("UPDATE conversations SET closed_at = NULL WHERE id = 1")
            .execute(&mut *waiting_tag)
            .await
            .unwrap();
        let max = store.active_limit(&mut *waiting_tag, 1).await.unwrap();
        assert_eq!(max, 1);
        conversation::evict_beyond(&mut waiting_tag, 1, max)
            .await
            .unwrap();
        waiting_tag.commit().await.unwrap();
        let counts: Vec<(i64, i64)> = sqlx::query_as("SELECT guild_id, count(*) FROM conversations WHERE closed_at IS NULL GROUP BY guild_id ORDER BY guild_id").fetch_all(&db).await.unwrap();
        assert_eq!(counts, [(1, 1), (2, 3)]);
        cleanup(admin, db, schema).await;
    }
}
