//! Kiki ingester: holds the Discord Gateway connection, records every message to
//! Postgres, and routes conversations to agents.
//!
//! Usage:
//!   ingester           run the service
//!   ingester migrate   apply database migrations and exit (Railway pre-deploy step)

mod config;
mod health;
mod leader;

use std::time::Duration;

use anyhow::{Context, bail};
use sqlx::postgres::PgPoolOptions;
use tokio::sync::watch;
use tracing::info;

use crate::config::Config;

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

    let (shutdown_tx, shutdown_rx) = watch::channel(false);
    tokio::spawn(async move {
        shutdown_signal().await;
        info!("shutdown signal received");
        let _ = shutdown_tx.send(true);
    });

    let health = tokio::spawn(health::serve(
        config.port,
        pool.clone(),
        shutdown_rx.clone(),
    ));

    // Only one instance may hold the Gateway connection. During a Railway deploy the
    // new instance waits here until the old one shuts down and releases the lock.
    let Some(leader) = leader::acquire(&pool, shutdown_rx.clone()).await? else {
        info!("shut down before acquiring leadership");
        return Ok(());
    };
    info!("leader lock acquired");

    // TODO: start the Gateway connection and message pipeline here.

    tokio::select! {
        result = leader.hold(shutdown_rx.clone()) => result?,
        result = health => result??,
    }

    info!("stopped");
    Ok(())
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
