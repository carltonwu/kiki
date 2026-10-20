//! Kiki ingester: holds the Discord Gateway connection, records every message to
//! Postgres, and routes conversations to agents.
//!
//! Usage:
//!   ingester           run the service
//!   ingester migrate   apply database migrations and exit (Railway pre-deploy step)

mod agent;
mod catch_up;
mod chat;
mod commands;
mod config;
mod conversation;
mod emojis;
mod gateway;
mod guild_settings;
mod health;
mod jev;
mod leader;
mod metrics;
mod platform;
mod rest;
mod router;
mod store;
mod telemetry;
#[cfg(test)]
mod test_db;

use std::{sync::Arc, time::Duration};

use anyhow::{Context, bail};
use sqlx::postgres::PgPoolOptions;
use tokio::sync::watch;
use tracing::{info, warn};

use crate::{
    agent::Agent,
    catch_up::CatchUp,
    commands::{Commands, Settings},
    config::{AgentConfig, Config, DiscordConfig, RouterConfig},
    gateway::Listener,
    router::Router,
};

static MIGRATOR: sqlx::migrate::Migrator = sqlx::migrate!("./migrations");

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    dotenvy::dotenv().ok(); // local development only; Railway sets real env vars
    let _ = rustls::crypto::ring::default_provider().install_default();
    let config = Config::from_env()?;
    // `migrate` runs as Railway's pre-deploy step; only the service exports.
    let telemetry = telemetry::init(config.json_logs, std::env::args().nth(1).is_none());
    let result = run(config).await;
    telemetry.shutdown().await;
    result
}

async fn run(config: Config) -> anyhow::Result<()> {
    let pool = PgPoolOptions::new()
        .max_connections(10)
        .acquire_timeout(Duration::from_secs(10))
        .connect(&config.database_url)
        .await
        .context("connecting to Postgres")?;

    match std::env::args().nth(1).as_deref() {
        None => {}
        Some("migrate") => {
            MIGRATOR.run(&pool).await.context("running migrations")?;
            info!("migrations applied");
            return Ok(());
        }
        Some(other) => bail!("unknown command `{other}` (expected `migrate` or no arguments)"),
    }
    metrics::observe_pool(&pool);

    let discord = DiscordConfig::from_env()?;
    let router_config = RouterConfig::from_env()?;
    let agent_config = AgentConfig::from_env()?;

    let (shutdown_tx, shutdown_rx) = watch::channel(false);
    tokio::spawn(async move {
        shutdown_signal().await;
        info!("shutdown signal received");
        let _ = shutdown_tx.send(true);
    });

    let mut health = tokio::spawn(health::serve(
        config.port,
        pool.clone(),
        shutdown_rx.clone(),
    ));

    let Some(mut leader) = leader::acquire(&pool, shutdown_rx.clone()).await? else {
        info!("shut down before acquiring leadership");
        return Ok(());
    };
    info!("leader lock acquired");
    let leader_held = telemetry::meter()
        .u64_gauge("kiki.leader.held")
        .with_description("1 while this instance holds the leader lock")
        .build();
    leader_held.record(1, &[]);

    // Leader only, so one instance reports the platform's metrics.
    let poller = tokio::spawn(platform::run(pool.clone(), shutdown_rx.clone()));
    let mut ingest = tokio::spawn(ingest(
        pool,
        discord,
        router_config,
        agent_config,
        shutdown_rx.clone(),
    ));

    let result = tokio::select! {
        result = &mut ingest => joined(result),
        error = leader.watch() => Err(error),
        result = &mut health => match joined(result) {
            Ok(()) => joined(ingest.await), // shutting down; let ingest finish
            Err(e) => Err(e),
        },
    };
    // Stop the poller (its gauges go silent with it) while still the leader.
    poller.abort();
    let _ = poller.await;
    // Only now may the next instance connect to the Gateway.
    leader.release().await;
    leader_held.record(0, &[]);
    result?;

    info!("stopped");
    Ok(())
}

async fn ingest(
    pool: sqlx::PgPool,
    discord: DiscordConfig,
    router_config: RouterConfig,
    agent_config: AgentConfig,
    shutdown: watch::Receiver<bool>,
) -> anyhow::Result<()> {
    let mut http = twilight_http::Client::builder().token(discord.token.clone());
    if let Some(proxy) = &discord.api_proxy {
        warn!(proxy, gateway = ?discord.gateway_url, "using a fake Discord (local testing only)");
        http = http.proxy(proxy.clone(), true).ratelimiter(None);
    }
    let http = Arc::new(http.build());
    let catch_up = Arc::new(CatchUp::new(http.clone(), pool.clone(), &discord.guild_ids));

    // The router starts before the Gateway so it sees every live message.
    let pending_emoji = router_config.pending_emoji.clone();
    let guild_settings = Arc::new(guild_settings::Store::new(
        pool.clone(),
        guild_settings::GuildSettings::defaults(&router_config, &agent_config),
        router_config.max_message_age.as_millis() as u64,
    )?);
    guild_settings
        .load(
            &discord
                .guild_ids
                .iter()
                .map(|id| id.get() as i64)
                .collect::<Vec<_>>(),
        )
        .await?;
    let settings = Settings::new(&router_config, &agent_config, guild_settings.clone())?;
    let router = Arc::new(
        Router::new(
            pool.clone(),
            http.clone(),
            &discord.guild_ids,
            router_config,
            guild_settings.clone(),
        )
        .await?,
    );
    let agent = match agent_config.host_url.clone() {
        Some(_) => Some(Arc::new(Agent::new(
            pool.clone(),
            http.clone(),
            agent_config,
            router.bot_user_id(),
            pending_emoji,
            guild_settings.clone(),
        )?)),
        None => {
            warn!("AGENT_HOST_URL is not set: the agent is off and routed messages get no reply");
            None
        }
    };
    let commands =
        Arc::new(Commands::new(pool.clone(), http.clone(), router.bot_user_id(), settings).await?);
    commands.register(&discord.guild_ids).await;
    let listener = Listener {
        commands,
        db: pool,
        catch_up,
        bot_user_id: router.bot_user_id(),
        guild_ids: discord.guild_ids.iter().map(|id| id.get() as i64).collect(),
        gateway_url: discord.gateway_url.clone(),
    };
    let agent_shutdown = shutdown.clone();
    tokio::try_join!(
        router.run(shutdown.clone()),
        async move {
            match agent {
                Some(agent) => agent.run(agent_shutdown).await,
                None => Ok(()),
            }
        },
        listener.run(discord.token, shutdown),
    )?;
    Ok(())
}

fn joined(result: Result<anyhow::Result<()>, tokio::task::JoinError>) -> anyhow::Result<()> {
    result.context("task panicked")?
}

async fn shutdown_signal() {
    let ctrl_c = tokio::signal::ctrl_c();
    #[cfg(unix)]
    {
        let mut sigterm = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
            .expect("installing SIGTERM handler");
        tokio::select! {
            _ = ctrl_c => {}
            _ = sigterm.recv() => {}
        }
    }
    #[cfg(not(unix))]
    let _ = ctrl_c.await;
}
