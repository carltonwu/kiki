use anyhow::Context;

/// Runtime configuration, read from environment variables.
pub struct Config {
    pub database_url: String,
    /// Port for the health endpoint. Railway injects `PORT`.
    pub port: u16,
    /// Emit JSON logs (Railway parses these into structured fields).
    pub json_logs: bool,
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
