//! Discord REST calls that return the raw JSON body, with retries.
//!
//! twilight-http's rate limiter paces requests per bucket but doesn't retry, so
//! 429s (e.g. a limit shared with another process using the token), 5xx and
//! network errors are retried here.

use std::{future::IntoFuture, time::Duration};

use anyhow::{Context, bail};
use serde_json::Value;
use tracing::{debug, warn};
use twilight_http::{Error, Response, error::ErrorType};

const MAX_RETRIES: u32 = 8;

/// Sends the request built by `make` and parses the body. Returns `None` if the bot
/// can't see the resource (403/404). `make` is called again for each retry.
pub async fn get_json<F, R, T>(what: &str, mut make: F) -> anyhow::Result<Option<Value>>
where
    F: FnMut() -> R,
    R: IntoFuture<Output = Result<Response<T>, Error>>,
{
    let mut attempt = 0;
    loop {
        let (delay, reason, status) = match make().await {
            Ok(response) => {
                let bytes = response
                    .bytes()
                    .await
                    .with_context(|| format!("reading {what}"))?;
                return Ok(Some(
                    serde_json::from_slice(&bytes).with_context(|| format!("parsing {what}"))?,
                ));
            }
            Err(e) => match e.kind() {
                ErrorType::Response { status, body, .. } => match status.get() {
                    403 | 404 => {
                        debug!(
                            what,
                            status = status.get(),
                            "Discord resource inaccessible; skipping"
                        );
                        return Ok(None);
                    }
                    429 => (
                        retry_after(body).unwrap_or(Duration::from_secs(5)),
                        "rate_limit",
                        Some(status.get()),
                    ),
                    s if s >= 500 => (backoff(attempt), "server", Some(s)),
                    _ => return Err(e).with_context(|| format!("requesting {what}")),
                },
                ErrorType::Unauthorized => bail!("Discord rejected the bot token (401)"),
                ErrorType::RequestError
                | ErrorType::RequestTimedOut
                | ErrorType::RequestCanceled => (backoff(attempt), "network", None),
                _ => return Err(e).with_context(|| format!("requesting {what}")),
            },
        };
        attempt += 1;
        if attempt > MAX_RETRIES {
            bail!(
                "giving up on {what} after {MAX_RETRIES} retries (reason={reason}, status={status:?})"
            );
        }
        warn!(
            what,
            attempt,
            reason,
            status,
            delay_ms = delay.as_millis() as u64,
            "Discord request failed; retrying"
        );
        crate::metrics::rest_retry(reason);
        tokio::time::sleep(delay).await;
    }
}

fn retry_after(body: &[u8]) -> Option<Duration> {
    let body: Value = serde_json::from_slice(body).ok()?;
    Duration::try_from_secs_f64(body.get("retry_after")?.as_f64()?).ok()
}

fn backoff(attempt: u32) -> Duration {
    Duration::from_secs(1 << attempt.min(6))
}
