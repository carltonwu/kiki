//! Unexpected failures, filed as GitHub issues (docs/observability.md, "Fault
//! issues").
//!
//! Both services save faults in `fault_events`: the agent host from
//! `faults.py`, the ingester with [`record`]. The leader's [`reporter`] groups
//! them by fingerprint and opens, or comments on, one issue each. The issues
//! are public, so a fault holds structured fields only: never message text, an
//! error's message, an HTTP body or a Discord ID.

pub mod github;
pub mod reporter;

use ring::digest;
use sqlx::PgPool;
use tracing::{info, warn};

use crate::telemetry;

/// The agent host's (`faults.py`) and the ingester's own kinds.
pub const KINDS: &[&str] = &[
    "executor_failed",
    "model_misbehaved",
    "provider_rejected",
    "tool_crashed",
    "drawing_failed",
    "tool_degraded",
    HOST_PROTOCOL,
    TASK_PANICKED,
];
/// The agent host answered outside its contract (`agent::host::Protocol`).
pub const HOST_PROTOCOL: &str = "host_protocol";
/// A conversation's agent task panicked.
pub const TASK_PANICKED: &str = "task_panicked";
/// Quiet fallbacks, filed only past a rate ([`reporter`]).
pub const TOOL_DEGRADED: &str = "tool_degraded";

/// One ingester fault. Strings are names and codes, never free text.
#[derive(Debug, Default, Clone)]
pub struct Fault {
    pub kind: &'static str,
    /// `reply`, `topic`, `image`.
    pub job_kind: Option<&'static str>,
    pub error_type: Option<&'static str>,
    pub stage: Option<&'static str>,
    pub http_status: Option<i32>,
    pub model: Option<String>,
    /// The host job ID (`reply:42`).
    pub run_id: Option<String>,
}

impl Fault {
    pub fn fingerprint(&self) -> String {
        fingerprint(
            "ingester",
            self.kind,
            self.error_type,
            self.stage,
            self.http_status,
            None,
        )
    }
}

/// What makes two faults the same issue: not the model, so one bug seen with
/// three models is one issue. `faults.fingerprint` in the agent host must
/// match (both test the same values).
pub fn fingerprint(
    service: &str,
    kind: &str,
    error_type: Option<&str>,
    stage: Option<&str>,
    http_status: Option<i32>,
    tool: Option<&str>,
) -> String {
    let status = http_status.map(|s| s.to_string());
    let identity = [
        Some(service),
        Some(kind),
        error_type,
        stage,
        status.as_deref(),
        tool,
    ]
    .map(|part| part.unwrap_or(""))
    .join("|");
    digest::digest(&digest::SHA256, identity.as_bytes()).as_ref()[..8]
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect()
}

/// Saves `fault` for the reporter. Never fails: reporting mustn't fail what
/// it reports.
pub async fn record(db: &PgPool, fault: Fault) {
    let fingerprint = fault.fingerprint();
    // `00-<trace id>-<span id>-01`
    let trace_id =
        telemetry::traceparent().and_then(|parent| parent.split('-').nth(1).map(str::to_owned));
    let saved = sqlx::query(
        r#"
        INSERT INTO fault_events (fingerprint, service, kind, job_kind, error_type, stage,
                                  http_status, model, version, environment, trace_id, run_id)
        VALUES ($1, 'ingester', $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
        "#,
    )
    .bind(&fingerprint)
    .bind(fault.kind)
    .bind(fault.job_kind)
    .bind(fault.error_type)
    .bind(fault.stage)
    .bind(fault.http_status)
    .bind(&fault.model)
    .bind(version())
    .bind(environment())
    .bind(trace_id)
    .bind(&fault.run_id)
    .execute(db)
    .await;
    match saved {
        Ok(_) => info!(
            kind = fault.kind,
            error_type = fault.error_type,
            stage = fault.stage,
            fingerprint,
            "fault recorded"
        ),
        Err(e) => warn!(kind = fault.kind, error = %e, "couldn't record a fault"),
    }
}

/// The deployed commit, as Railway names it.
pub fn version() -> Option<String> {
    env("RAILWAY_GIT_COMMIT_SHA")
}

/// The Railway environment, `local` outside Railway (as the agent host's).
pub fn environment() -> String {
    env("RAILWAY_ENVIRONMENT_NAME").unwrap_or_else(|| "local".into())
}

fn env(name: &str) -> Option<String> {
    std::env::var(name)
        .ok()
        .map(|v| v.trim().to_owned())
        .filter(|v| !v.is_empty())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fingerprints_match_the_agent_host() {
        // The same values as test_faults.py.
        assert_eq!(
            fingerprint(
                "agent",
                "provider_rejected",
                Some("ModelHTTPError"),
                Some("reply_agent.run"),
                Some(400),
                None
            ),
            "2a7c3c50decec229"
        );
        let fault = Fault {
            kind: HOST_PROTOCOL,
            stage: Some("host.response"),
            model: Some("any/model".into()),
            ..Fault::default()
        };
        assert_eq!(fault.fingerprint(), "b5e34f012e3eb402");
    }

    #[tokio::test]
    async fn records_structured_fields() {
        let Some(test) = crate::test_db::migrated("faults_record").await else {
            return;
        };
        record(
            &test.db,
            Fault {
                kind: HOST_PROTOCOL,
                job_kind: Some("reply"),
                error_type: Some("bad_status"),
                stage: Some("host.request"),
                http_status: Some(500),
                model: Some("test/model".into()),
                run_id: Some("reply:7".into()),
            },
        )
        .await;
        let row: (String, String, String, Option<i32>, String, Option<String>) = sqlx::query_as(
            "SELECT fingerprint, service, kind, http_status, environment, run_id FROM fault_events",
        )
        .fetch_one(&test.db)
        .await
        .unwrap();
        assert_eq!(
            row,
            (
                fingerprint(
                    "ingester",
                    HOST_PROTOCOL,
                    Some("bad_status"),
                    Some("host.request"),
                    Some(500),
                    None
                ),
                "ingester".into(),
                HOST_PROTOCOL.into(),
                Some(500),
                environment(),
                Some("reply:7".into()),
            )
        );
        test.drop().await;
    }
}
