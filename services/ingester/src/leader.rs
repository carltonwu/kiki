//! Single-leader lock via a Postgres session advisory lock.
//!
//! Railway starts the new deployment before stopping the old one, so two instances
//! briefly run at once. Only the lock holder may connect to the Discord Gateway;
//! otherwise every message would be processed (and replied to) twice.

use std::time::Duration;

use anyhow::{Context, anyhow};
use sqlx::{Connection, PgConnection, PgPool};
use tokio::sync::watch;
use tracing::info;

/// Arbitrary constant identifying this lock ("kiki" in ASCII).
const LOCK_KEY: i64 = 0x6b69_6b69;
const RETRY_INTERVAL: Duration = Duration::from_secs(2);
const PING_INTERVAL: Duration = Duration::from_secs(10);

/// Holds the dedicated connection that owns the lock. Dropping it closes the
/// connection, which releases the lock.
pub struct Leader {
    conn: PgConnection,
}

/// Waits until this instance holds the lock. Returns `None` if shutdown was
/// requested first.
pub async fn acquire(
    pool: &PgPool,
    mut shutdown: watch::Receiver<bool>,
) -> anyhow::Result<Option<Leader>> {
    // Session advisory locks belong to a connection, so take one out of the pool for good.
    let mut conn = pool.acquire().await?.detach();
    let mut logged = false;
    loop {
        let locked: bool = sqlx::query_scalar("SELECT pg_try_advisory_lock($1)")
            .bind(LOCK_KEY)
            .fetch_one(&mut conn)
            .await
            .context("trying leader lock")?;
        if locked {
            return Ok(Some(Leader { conn }));
        }
        if !logged {
            info!("another instance holds the leader lock; waiting for it to stop");
            logged = true;
        }
        tokio::select! {
            _ = tokio::time::sleep(RETRY_INTERVAL) => {}
            _ = shutdown.wait_for(|stop| *stop) => return Ok(None),
        }
    }
}

impl Leader {
    /// Returns only if the lock connection drops: the lock is gone and another
    /// instance may take over, so the caller must stop; exiting lets Railway restart us.
    pub async fn watch(&mut self) -> anyhow::Error {
        loop {
            tokio::time::sleep(PING_INTERVAL).await;
            if let Err(e) = self.conn.ping().await {
                return anyhow!("lost leader lock connection: {e}");
            }
        }
    }

    /// Releases the lock. Call only after the Gateway connection is closed.
    pub async fn release(self) {
        self.conn.close().await.ok();
        info!("leader lock released");
    }
}
