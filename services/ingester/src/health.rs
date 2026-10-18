//! HTTP health endpoint. Railway calls it during a deploy to decide when the new
//! instance is ready. It reports process + database health only, never leadership:
//! the new instance must turn healthy *before* the old one is stopped and releases
//! the leader lock.
//!
//! With `ADMIN_API_TOKEN` set, the console's admin API ([`crate::admin`]) is served
//! here too, on every instance: its writes don't need leadership.

use std::net::{Ipv6Addr, SocketAddr};

use axum::{Router, extract::State, http::StatusCode, routing::get};
use sqlx::PgPool;
use tokio::sync::watch;
use tracing::{info, warn};

pub async fn serve(
    port: u16,
    pool: PgPool,
    admin: Option<Router>,
    mut shutdown: watch::Receiver<bool>,
) -> anyhow::Result<()> {
    let mut app = Router::new().route("/health", get(health)).with_state(pool);
    if let Some(admin) = admin {
        app = app.merge(admin);
    }
    // `::` accepts IPv4 and IPv6; Railway's private network is IPv6.
    let addr = SocketAddr::from((Ipv6Addr::UNSPECIFIED, port));
    let listener = tokio::net::TcpListener::bind(addr).await?;
    info!(%addr, "health endpoint listening");
    axum::serve(listener, app)
        .with_graceful_shutdown(async move {
            let _ = shutdown.wait_for(|stop| *stop).await;
        })
        .await?;
    Ok(())
}

async fn health(State(pool): State<PgPool>) -> StatusCode {
    match sqlx::query("SELECT 1").execute(&pool).await {
        Ok(_) => StatusCode::OK,
        Err(e) => {
            warn!(error = %e, "health check: database unreachable");
            StatusCode::SERVICE_UNAVAILABLE
        }
    }
}
