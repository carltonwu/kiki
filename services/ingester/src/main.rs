//! Kiki ingester: holds the Discord Gateway connection, records every message to
//! Postgres, and routes conversations to agents.
//!
//! Usage:
//!   ingester           run the service
//!   ingester migrate   apply database migrations and exit (Railway pre-deploy step)

mod catch_up;
mod config;
mod gateway;
mod health;
mod leader;
mod rest;
mod store;

use std::{sync::Arc, time::Duration};

use anyhow::{Context, bail};
use sqlx::postgres::PgPoolOptions;
use tokio::sync::watch;
use tracing::info;

use crate::{
    catch_up::CatchUp,
    config::{Config, DiscordConfig},
    gateway::Listener,
};

static MIGRATOR: sqlx::migrate::Migrator = sqlx::migrate!("./migrations");

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    dotenvy::dotenv().ok(); // local development only; Railway sets real env vars
    let config = Config::from_env()?;
    init_tracing(&config);

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

    let discord = DiscordConfig::from_env()?;

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

    // Only one instance may hold the Gateway connection. During a Railway deploy the
    // new instance waits here until the old one shuts down and releases the lock.
    let Some(mut leader) = leader::acquire(&pool, shutdown_rx.clone()).await? else {
        info!("shut down before acquiring leadership");
        return Ok(());
    };
    info!("leader lock acquired");

    let mut ingest = tokio::spawn(ingest(pool, discord, shutdown_rx.clone()));

    // `ingest` returns after shutdown, once the Gateway connection is closed.
    let result = tokio::select! {
        result = &mut ingest => joined(result),
        error = leader.watch() => Err(error),
        result = &mut health => match joined(result) {
            Ok(()) => joined(ingest.await), // shutting down; let ingest finish
            Err(e) => Err(e),
        },
    };
    // Only now may the next instance connect to the Gateway.
    leader.release().await;
    result?;

    info!("stopped");
    Ok(())
}

/// Backfills every enabled guild over REST, then listens to the Gateway.
async fn ingest(
    pool: sqlx::PgPool,
    discord: DiscordConfig,
    mut shutdown: watch::Receiver<bool>,
) -> anyhow::Result<()> {
    let http = Arc::new(twilight_http::Client::new(discord.token.clone()));
    let catch_up = Arc::new(CatchUp::new(http, pool.clone(), &discord.guild_ids));

    info!(
        guilds = discord.guild_ids.len(),
        "backfilling before connecting to the Gateway"
    );
    let backfill = async {
        for &guild_id in &discord.guild_ids {
            catch_up.run_logged(guild_id).await;
        }
    };
    tokio::select! {
        () = backfill => {}
        _ = shutdown.wait_for(|stop| *stop) => return Ok(()),
    }

    let listener = Listener {
        db: pool,
        catch_up,
        guild_ids: discord.guild_ids.iter().map(|id| id.get() as i64).collect(),
    };
    listener.run(discord.token, shutdown).await
}

fn joined(result: Result<anyhow::Result<()>, tokio::task::JoinError>) -> anyhow::Result<()> {
    result.context("task panicked")?
}

fn init_tracing(config: &Config) {
    let filter = tracing_subscriber::EnvFilter::try_from_default_env()
        .unwrap_or_else(|_| "info,sqlx=warn".into());
    let builder = tracing_subscriber::fmt().with_env_filter(filter);
    if config.json_logs {
        builder.json().flatten_event(true).init();
    } else {
        builder.init();
    }
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
