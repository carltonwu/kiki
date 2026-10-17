use anyhow::{Context, bail};
use twilight_model::id::{Id, marker::GuildMarker};

/// Runtime configuration, read from environment variables.
pub struct Config {
    pub database_url: String,
    /// Port for the health endpoint. Railway injects `PORT`.
    pub port: u16,
    /// Emit JSON logs (Railway parses these into structured fields).
    pub json_logs: bool,
}

/// Discord settings. Separate from [`Config`] because `ingester migrate` doesn't need them.
pub struct DiscordConfig {
    pub token: String,
    /// Guilds to ingest. Events from any other guild the bot is in are ignored.
    pub guild_ids: Vec<Id<GuildMarker>>,
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
        Ok(Self {
            token: token.trim().to_owned(),
            guild_ids,
        })
    }
}
