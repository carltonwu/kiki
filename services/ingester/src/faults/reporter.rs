//! Files `fault_events` as GitHub issues, on the leader only.
//!
//! A pass, woken by `fault_event` notifications (at most every [`MIN_GAP`]) or
//! every [`POLL`], takes each fingerprint's unreported events:
//! - With no tracked issue, it looks for one by search (an issue filed before
//!   the table was lost, or by another environment's reporter) and adopts it,
//!   else opens one: at most [`MAX_NEW_PER_HOUR`] an hour, the rest wait.
//! - An open issue counts them, for a comment at most every
//!   [`COMMENT_EVERY`].
//! - One closed as not planned mutes the fingerprint: counted, never commented.
//! - One closed otherwise was fixed, so this is a regression: a new issue
//!   links the old one.
//!
//! Quiet fallbacks (`tool_degraded`) are filed only at [`DEGRADED_PER_HOUR`]
//! an hour; fewer are dropped once an hour old. Events from another
//! environment than `FAULT_REPORT_ENVIRONMENT` (default `production`) are
//! dropped, so a staging deploy files nothing. A refused credential turns
//! reporting off until restart (still a failure each pass events wait, for
//! the alert); other GitHub failures back off. Events are
//! deleted after [`RETENTION_DAYS`], reported or not.

use std::time::Duration;

use sqlx::{FromRow, PgPool, postgres::PgListener};
use tokio::{
    sync::watch,
    time::{Instant, sleep_until},
};
use tracing::{info, warn};

use super::{
    TOOL_DEGRADED,
    github::{self, Github},
};
use crate::metrics::{self, FaultAction};

const POLL: Duration = Duration::from_secs(60);
const MIN_GAP: Duration = Duration::from_secs(30);
/// After a notification, for the rest of a failing job's faults.
const DEBOUNCE: Duration = Duration::from_secs(5);
const MIN_BACKOFF: Duration = Duration::from_secs(60);
const MAX_BACKOFF: Duration = Duration::from_secs(30 * 60);
const MAX_NEW_PER_HOUR: i64 = 5;
const DEGRADED_PER_HOUR: i64 = 5;
const COMMENT_EVERY: &str = "6 hours";
const RETENTION_DAYS: i32 = 30;
/// Fingerprints, and comments, a pass handles; more wait for the next.
const PER_PASS: i64 = 50;
const NOTIFY_CHANNEL: &str = "fault_event";
const LABEL: &str = "kiki-fault";

pub struct Reporter {
    db: PgPool,
    github: Option<Github>,
    environment: String,
    labelled: bool,
    /// Why GitHub refused the credential: nothing is sent until restart.
    refused: Option<String>,
}

/// Reports until shutdown. Spawn it once this instance is the leader.
pub async fn run(db: PgPool, mut shutdown: watch::Receiver<bool>) {
    let mut reporter = Reporter::from_env(db.clone(), |name| std::env::var(name).ok());
    let mut listener = match PgListener::connect_with(&db).await {
        Ok(mut listener) => match listener.listen(NOTIFY_CHANNEL).await {
            Ok(()) => Some(listener),
            Err(e) => {
                warn!(error = %e, "fault reporter can't listen; polling only");
                None
            }
        },
        Err(e) => {
            warn!(error = %e, "fault reporter can't listen; polling only");
            None
        }
    };
    info!(
        repo = reporter.github.as_ref().map(Github::repo),
        environment = reporter.environment,
        "fault reporter started"
    );
    let mut next = Instant::now() + DEBOUNCE;
    let mut last = Instant::now();
    let mut backoff: Option<Duration> = None;
    loop {
        tokio::select! {
            () = sleep_until(next) => {}
            notification = async {
                match &mut listener {
                    Some(listener) => listener.recv().await,
                    None => std::future::pending().await,
                }
            } => {
                match notification {
                    // Sooner, unless backing off.
                    Ok(_) if backoff.is_none() => {
                        next = next.min((Instant::now() + DEBOUNCE).max(last + MIN_GAP));
                    }
                    Ok(_) => {}
                    // PgListener reconnects on the next recv; the poll covers
                    // what it missed.
                    Err(e) => {
                        warn!(error = %e, "fault reporter lost its Postgres listener; reconnecting");
                        tokio::time::sleep(Duration::from_secs(1)).await;
                    }
                }
                continue;
            }
            _ = async { shutdown.wait_for(|stop| *stop).await.map(drop) } => return,
        }
        last = Instant::now();
        next = last + POLL;
        match reporter.pass().await {
            Ok(()) => backoff = None,
            // Counted each pass faults wait, for the alert.
            Err(github::Error::Unauthorized(message)) => {
                if reporter.refused.is_none() {
                    warn!(
                        message,
                        "GitHub refused the fault reporter's credential; reporting is off until restart"
                    );
                    reporter.refused = Some(message);
                }
                metrics::fault_reported(FaultAction::Failed);
            }
            Err(e) => {
                let wait = backoff.map_or(MIN_BACKOFF, |b| (b * 2).min(MAX_BACKOFF));
                let wait = match e {
                    github::Error::RateLimited(Some(asked)) => wait.max(asked).min(MAX_BACKOFF),
                    _ => wait,
                };
                warn!(error = %e, retry_in_s = wait.as_secs(), "fault report pass failed");
                metrics::fault_reported(FaultAction::Failed);
                backoff = Some(wait);
                next = last + wait;
            }
        }
    }
}

/// One fingerprint's unreported events, with its latest event's fields.
#[derive(Debug, FromRow)]
struct Group {
    fingerprint: String,
    count: i64,
    max_id: i64,
    first_at: String,
    last_at: String,
    last_hour: i64,
    models: Vec<String>,
    service: String,
    kind: String,
    job_kind: Option<String>,
    error_type: Option<String>,
    stage: Option<String>,
    http_status: Option<i32>,
    tool: Option<String>,
    finish_reason: Option<String>,
    version: Option<String>,
    environment: Option<String>,
    trace_id: Option<String>,
    run_id: Option<String>,
    issue_number: Option<i32>,
}

/// An issue owing a comment.
#[derive(Debug, FromRow)]
struct Owed {
    fingerprint: String,
    issue_number: i32,
    unreported: i64,
    occurrences: i64,
    since: String,
    last_version: Option<String>,
    last_trace_id: Option<String>,
    last_run_id: Option<String>,
}

impl Reporter {
    pub fn from_env(db: PgPool, env: impl Fn(&str) -> Option<String>) -> Self {
        let off = env("FAULT_REPORTS").is_some_and(|v| v.trim().eq_ignore_ascii_case("off"));
        let github = if off {
            info!("fault reports are off (FAULT_REPORTS=off)");
            None
        } else {
            match Github::from_env(&env) {
                Ok(github) => github,
                Err(e) => {
                    warn!(error = %e, "fault reports are off");
                    None
                }
            }
        };
        let environment = env("FAULT_REPORT_ENVIRONMENT")
            .map(|v| v.trim().to_owned())
            .filter(|v| !v.is_empty())
            .unwrap_or_else(|| "production".into());
        Self {
            db,
            github,
            environment,
            labelled: false,
            refused: None,
        }
    }

    /// Without a credential, only deletes old events. A database error is
    /// `Other`, for the backoff. Once refused, fails without calling GitHub
    /// while events wait.
    pub async fn pass(&mut self) -> Result<(), github::Error> {
        sqlx::query(
            "DELETE FROM fault_events WHERE created_at < now() - make_interval(days => $1)",
        )
        .bind(RETENTION_DAYS)
        .execute(&self.db)
        .await
        .map_err(db)?;
        let Some(github) = self.github.as_mut() else {
            return Ok(());
        };
        if let Some(refused) = &self.refused {
            let waiting: bool = sqlx::query_scalar(
                "SELECT EXISTS (SELECT 1 FROM fault_events WHERE reported_at IS NULL)",
            )
            .fetch_one(&self.db)
            .await
            .map_err(db)?;
            return match waiting {
                true => Err(github::Error::Unauthorized(refused.clone())),
                false => Ok(()),
            };
        }
        if !self.labelled {
            ensure_labels(github).await?;
            self.labelled = true;
        }
        let dropped: Vec<(String, String, String)> = sqlx::query_as(
            "UPDATE fault_events SET reported_at = now()
             WHERE reported_at IS NULL AND environment IS DISTINCT FROM $1
             RETURNING fingerprint, service, kind",
        )
        .bind(&self.environment)
        .fetch_all(&self.db)
        .await
        .map_err(db)?;
        took_in(&dropped, FaultAction::Dropped);

        for group in self.groups().await? {
            self.report(group).await?;
        }
        self.comment().await
    }

    async fn groups(&self) -> Result<Vec<Group>, github::Error> {
        sqlx::query_as(
            r#"
            WITH pending AS (
                SELECT fingerprint, count(*) AS count, max(id) AS max_id,
                       min(created_at) AS first_at, max(created_at) AS last_at,
                       array_remove(array_agg(DISTINCT model), NULL) AS models
                FROM fault_events WHERE reported_at IS NULL
                GROUP BY fingerprint
            )
            SELECT p.fingerprint, p.count, p.max_id, p.models,
                   to_char(p.first_at AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI "UTC"') AS first_at,
                   to_char(p.last_at AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI "UTC"') AS last_at,
                   (SELECT count(*) FROM fault_events h
                    WHERE h.fingerprint = p.fingerprint AND h.created_at > now() - interval '1 hour'
                   ) AS last_hour,
                   e.service, e.kind, e.job_kind, e.error_type, e.stage, e.http_status, e.tool,
                   e.finish_reason, e.version, e.environment, e.trace_id, e.run_id,
                   i.issue_number
            FROM pending p
            JOIN fault_events e ON e.id = p.max_id
            LEFT JOIN fault_issues i ON i.fingerprint = p.fingerprint
            ORDER BY p.max_id
            LIMIT $1
            "#,
        )
        .bind(PER_PASS)
        .fetch_all(&self.db)
        .await
        .map_err(db)
    }

    async fn report(&mut self, group: Group) -> Result<(), github::Error> {
        let github = self.github.as_mut().expect("checked by pass");
        if group.kind == TOOL_DEGRADED
            && group.issue_number.is_none()
            && group.last_hour < DEGRADED_PER_HOUR
        {
            // Too few to file; newer ones still count toward the rate.
            let dropped: Vec<(String, String, String)> = sqlx::query_as(
                "UPDATE fault_events SET reported_at = now()
                 WHERE fingerprint = $1 AND id <= $2 AND reported_at IS NULL
                   AND created_at <= now() - interval '1 hour'
                 RETURNING fingerprint, service, kind",
            )
            .bind(&group.fingerprint)
            .bind(group.max_id)
            .fetch_all(&self.db)
            .await
            .map_err(db)?;
            took_in(&dropped, FaultAction::Dropped);
            return Ok(());
        }

        let mut issue = None;
        if let Some(number) = group.issue_number {
            match github.get(number).await {
                Ok(found) => issue = Some(found),
                // Deleted or transferred: file it again.
                Err(github::Error::NotFound) => {
                    sqlx::query("DELETE FROM fault_issues WHERE fingerprint = $1")
                        .bind(&group.fingerprint)
                        .execute(&self.db)
                        .await
                        .map_err(db)?;
                }
                Err(e) => return Err(e),
            }
        } else if let Some(found) = github.find(&group.fingerprint).await? {
            // Owes no comment yet, so its first comes on the next pass.
            sqlx::query(
                "INSERT INTO fault_issues (fingerprint, issue_number, last_comment_at)
                 VALUES ($1, $2, now() - interval '6 hours')
                 ON CONFLICT (fingerprint) DO NOTHING",
            )
            .bind(&group.fingerprint)
            .bind(found.number)
            .execute(&self.db)
            .await
            .map_err(db)?;
            info!(
                fingerprint = group.fingerprint,
                issue = found.number,
                "fault issue adopted"
            );
            issue = Some(found);
        }

        let action = match &issue {
            Some(issue) if issue.state == "open" => {
                self.count(&group, issue.number, true).await?;
                FaultAction::Counted
            }
            Some(issue) if issue.not_planned() => {
                self.count(&group, issue.number, false).await?;
                FaultAction::Muted
            }
            closed => {
                if self.filed_last_hour().await? >= MAX_NEW_PER_HOUR {
                    metrics::fault_reported(FaultAction::Deferred);
                    return Ok(());
                }
                let regressed = closed.as_ref().map(|i| i.number);
                self.file(&group, regressed).await?;
                FaultAction::Created
            }
        };
        metrics::fault_reported(action);
        Ok(())
    }

    /// Counts the group into `number`, to be commented on if `comment`.
    async fn count(&self, group: &Group, number: i32, comment: bool) -> Result<(), github::Error> {
        let mut tx = self.db.begin().await.map_err(db)?;
        sqlx::query(
            "UPDATE fault_issues
             SET issue_number = $2, occurrences = occurrences + $3,
                 unreported = unreported + CASE WHEN $4 THEN $3 ELSE 0 END,
                 last_version = $5, last_trace_id = $6, last_run_id = $7
             WHERE fingerprint = $1",
        )
        .bind(&group.fingerprint)
        .bind(number)
        .bind(group.count)
        .bind(comment)
        .bind(&group.version)
        .bind(&group.trace_id)
        .bind(&group.run_id)
        .execute(&mut *tx)
        .await
        .map_err(db)?;
        let taken = mark(&mut tx, group).await?;
        tx.commit().await.map_err(db)?;
        took_in(&taken, FaultAction::Counted);
        Ok(())
    }

    /// Opens an issue for the group; `regressed` is the closed one it reopens.
    async fn file(&mut self, group: &Group, regressed: Option<i32>) -> Result<(), github::Error> {
        let github = self.github.as_mut().expect("checked by pass");
        let mut title = title(group);
        let mut body = body(group);
        if let Some(old) = regressed {
            title = format!("Regression: {title}");
            body = format!("Seen again after #{old} was closed.\n\n{body}");
        }
        let labels = [LABEL, "bug", service_label(&group.service)];
        let created = github.create(&title, &body, &labels).await?;
        info!(
            fingerprint = group.fingerprint,
            issue = created.number,
            regressed,
            "fault issue opened"
        );
        let mut tx = self.db.begin().await.map_err(db)?;
        sqlx::query(
            "INSERT INTO fault_issues (fingerprint, issue_number, filed_at, occurrences,
                                       last_version, last_trace_id, last_run_id)
             VALUES ($1, $2, now(), $3, $4, $5, $6)
             ON CONFLICT (fingerprint) DO UPDATE
             SET issue_number = $2, filed_at = now(), occurrences = $3, unreported = 0,
                 last_comment_at = now(), last_version = $4, last_trace_id = $5, last_run_id = $6",
        )
        .bind(&group.fingerprint)
        .bind(created.number)
        .bind(group.count)
        .bind(&group.version)
        .bind(&group.trace_id)
        .bind(&group.run_id)
        .execute(&mut *tx)
        .await
        .map_err(db)?;
        let taken = mark(&mut tx, group).await?;
        tx.commit().await.map_err(db)?;
        took_in(&taken, FaultAction::Created);
        Ok(())
    }

    async fn filed_last_hour(&self) -> Result<i64, github::Error> {
        sqlx::query_scalar(
            "SELECT count(*) FROM fault_issues WHERE filed_at > now() - interval '1 hour'",
        )
        .fetch_one(&self.db)
        .await
        .map_err(db)
    }

    /// Comments on the open issues with occurrences since their last comment,
    /// once that is [`COMMENT_EVERY`] old.
    async fn comment(&mut self) -> Result<(), github::Error> {
        let owed: Vec<Owed> = sqlx::query_as(&format!(
            "SELECT fingerprint, issue_number, unreported, occurrences, last_version,
                    last_trace_id, last_run_id,
                    to_char(last_comment_at AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI \"UTC\"') AS since
             FROM fault_issues
             WHERE unreported > 0 AND last_comment_at <= now() - interval '{COMMENT_EVERY}'
             ORDER BY last_comment_at
             LIMIT $1"
        ))
        .bind(PER_PASS)
        .fetch_all(&self.db)
        .await
        .map_err(db)?;
        let github = self.github.as_mut().expect("checked by pass");
        for owed in owed {
            match github.comment(owed.issue_number, &comment(&owed)).await {
                Ok(()) => {
                    sqlx::query(
                        "UPDATE fault_issues
                         SET unreported = greatest(unreported - $2, 0), last_comment_at = now()
                         WHERE fingerprint = $1",
                    )
                    .bind(&owed.fingerprint)
                    .bind(owed.unreported)
                    .execute(&self.db)
                    .await
                    .map_err(db)?;
                    metrics::fault_reported(FaultAction::Commented);
                }
                // The next occurrence files it again.
                Err(github::Error::NotFound) => {
                    sqlx::query("DELETE FROM fault_issues WHERE fingerprint = $1")
                        .bind(&owed.fingerprint)
                        .execute(&self.db)
                        .await
                        .map_err(db)?;
                }
                Err(e) => return Err(e),
            }
        }
        Ok(())
    }
}

async fn ensure_labels(github: &mut Github) -> Result<(), github::Error> {
    for (name, color, description) in [
        (LABEL, "b60205", "Filed by Kiki's fault reporter"),
        ("bug", "d73a4a", "Something isn't working"),
        ("service:agent", "1d76db", "The Python agent host"),
        ("service:ingester", "5319e7", "The Rust ingester"),
    ] {
        github.ensure_label(name, color, description).await?;
    }
    Ok(())
}

fn service_label(service: &str) -> &'static str {
    if service == "agent" {
        "service:agent"
    } else {
        "service:ingester"
    }
}

/// Marks the group's events reported, returning them for the metrics.
async fn mark(
    tx: &mut sqlx::Transaction<'_, sqlx::Postgres>,
    group: &Group,
) -> Result<Vec<(String, String, String)>, github::Error> {
    sqlx::query_as(
        "UPDATE fault_events SET reported_at = now()
         WHERE fingerprint = $1 AND id <= $2 AND reported_at IS NULL
         RETURNING fingerprint, service, kind",
    )
    .bind(&group.fingerprint)
    .bind(group.max_id)
    .fetch_all(&mut **tx)
    .await
    .map_err(db)
}

/// Counts events taken in, and once per fingerprint what was done with them.
fn took_in(events: &[(String, String, String)], action: FaultAction) {
    for (_, service, kind) in events {
        metrics::fault_recorded(service, kind);
    }
    if action == FaultAction::Dropped {
        let mut fingerprints: Vec<&str> = events.iter().map(|(f, ..)| f.as_str()).collect();
        fingerprints.sort_unstable();
        fingerprints.dedup();
        for _ in fingerprints {
            metrics::fault_reported(FaultAction::Dropped);
        }
    }
}

fn db(e: sqlx::Error) -> github::Error {
    github::Error::Other(anyhow::Error::new(e).context("fault reporter database query"))
}

/// A name or code, safe in a public issue's Markdown: the characters
/// identifiers use, capped. Anything else (a message that got into a field) is
/// cut, not shown.
fn clean(value: &str) -> String {
    value
        .chars()
        .filter(|c| c.is_ascii_alphanumeric() || "_.:/-".contains(*c))
        .take(100)
        .collect()
}

fn code(value: Option<&str>) -> String {
    match value.map(clean).filter(|v| !v.is_empty()) {
        Some(v) => format!("`{v}`"),
        None => "–".into(),
    }
}

fn title(group: &Group) -> String {
    let what = group
        .error_type
        .as_deref()
        .map(clean)
        .filter(|e| !e.is_empty())
        .or(group.http_status.map(|s| format!("HTTP {s}")))
        .or(group.tool.as_deref().map(clean));
    let mut title = format!("[{LABEL}] {}", clean(&group.kind));
    if let Some(what) = &what {
        title += &format!(": {what}");
    }
    if let Some(stage) = group.stage.as_deref().map(clean).filter(|s| !s.is_empty()) {
        title += &format!(" in {stage}");
    }
    if let Some(tool) = group.tool.as_deref().map(clean)
        && what.as_deref() != Some(tool.as_str())
    {
        title += &format!(" ({tool})");
    }
    title
}

fn body(group: &Group) -> String {
    let models = if group.models.is_empty() {
        code(None)
    } else {
        let mut shown: Vec<String> = group.models.iter().take(5).map(|m| code(Some(m))).collect();
        if group.models.len() > 5 {
            shown.push(format!("and {} more", group.models.len() - 5));
        }
        shown.join(", ")
    };
    let status = group.http_status.map(|s| s.to_string());
    let rows = [
        ("Fingerprint", code(Some(&group.fingerprint))),
        ("Service", code(Some(&group.service))),
        ("Kind", code(Some(&group.kind))),
        ("Error type", code(group.error_type.as_deref())),
        ("Stage", code(group.stage.as_deref())),
        ("HTTP status", code(status.as_deref())),
        ("Tool", code(group.tool.as_deref())),
        ("Job kind", code(group.job_kind.as_deref())),
        ("Models", models),
        ("Finish reason", code(group.finish_reason.as_deref())),
        ("Version", code(group.version.as_deref())),
        ("Environment", code(group.environment.as_deref())),
        (
            "Occurrences",
            format!("{} ({} to {})", group.count, group.first_at, group.last_at),
        ),
        ("Latest trace", code(group.trace_id.as_deref())),
        ("Latest job", code(group.run_id.as_deref())),
    ];
    let table: String = rows
        .iter()
        .map(|(name, value)| format!("| {name} | {value} |\n"))
        .collect();
    format!(
        "<!-- kiki-fault:{fingerprint} -->\n\
         Kiki hit an unexpected failure. Opened by the ingester's fault reporter \
         (docs/observability.md, \"Fault issues\"); faults hold structured fields only.\n\n\
         | Field | Value |\n|---|---|\n{table}\n\
         Find the latest trace in Grafana Tempo, and the job's logs by its ID.\n\n\
         - New occurrences are counted here, in a comment at most every {COMMENT_EVERY}.\n\
         - Close as completed once fixed: if it happens again, a regression issue is opened.\n\
         - Close as not planned to stop hearing about it.\n",
        fingerprint = clean(&group.fingerprint),
    )
}

fn comment(owed: &Owed) -> String {
    let times = |n: i64| {
        if n == 1 {
            "once".to_owned()
        } else {
            format!("{n} times")
        }
    };
    format!(
        "Seen {} more since {} ({} in all). Latest: version {}, trace {}, job {}.",
        times(owed.unreported),
        owed.since,
        owed.occurrences,
        code(owed.last_version.as_deref()),
        code(owed.last_trace_id.as_deref()),
        code(owed.last_run_id.as_deref()),
    )
}

#[cfg(test)]
mod tests {
    use std::{
        collections::HashMap,
        sync::{Arc, Mutex},
    };

    use axum::{
        Json, Router,
        extract::{Path, Query, Request, State},
        http::StatusCode,
        middleware::{self, Next},
        response::{IntoResponse, Response},
        routing::{get, post},
    };
    use serde_json::{Value, json};

    use super::*;
    use crate::{faults::fingerprint, test_db};

    const TOKEN: &str = "Bearer test-token";
    const INSTALLATION_TOKEN: &str = "Bearer installation-token";

    #[derive(Default)]
    struct Fake {
        issues: Vec<Value>,
        comments: Vec<(i64, String)>,
        labels: Vec<String>,
        refuse: bool,
        app_tokens: usize,
    }

    type Shared = Arc<Mutex<Fake>>;

    impl Fake {
        fn issue(&mut self, number: i64) -> &mut Value {
            self.issues
                .iter_mut()
                .find(|i| i["number"] == number)
                .expect("an issue")
        }
    }

    /// GitHub's issue endpoints, as far as the reporter uses them.
    async fn github() -> (String, Shared) {
        async fn auth(State(fake): State<Shared>, request: Request, next: Next) -> Response {
            let header = request
                .headers()
                .get("authorization")
                .and_then(|v| v.to_str().ok())
                .unwrap_or_default()
                .to_owned();
            let ok = if request.uri().path().ends_with("/access_tokens") {
                header.starts_with("Bearer ey")
            } else {
                header == TOKEN || header == INSTALLATION_TOKEN
            };
            if !ok || fake.lock().unwrap().refuse {
                return (
                    StatusCode::UNAUTHORIZED,
                    Json(json!({"message": "Bad credentials"})),
                )
                    .into_response();
            }
            next.run(request).await
        }
        let fake = Shared::default();
        let router = Router::new()
            .route(
                "/repos/{owner}/{repo}/labels",
                post(|State(fake): State<Shared>, Json(label): Json<Value>| async move {
                    let mut fake = fake.lock().unwrap();
                    let name = label["name"].as_str().unwrap().to_owned();
                    if fake.labels.contains(&name) {
                        return StatusCode::UNPROCESSABLE_ENTITY.into_response();
                    }
                    fake.labels.push(name);
                    (StatusCode::CREATED, Json(label)).into_response()
                }),
            )
            .route(
                "/search/issues",
                get(
                    |State(fake): State<Shared>, Query(query): Query<HashMap<String, String>>| async move {
                        let q = &query["q"];
                        assert!(q.contains("repo:carltonwu/kiki") && q.contains("label:kiki-fault"));
                        let word = q.rsplit(' ').next().unwrap();
                        let items: Vec<Value> = fake
                            .lock()
                            .unwrap()
                            .issues
                            .iter()
                            .rev()
                            .filter(|i| i["body"].as_str().unwrap().contains(word))
                            .filter(|i| i["labels"].as_array().unwrap().contains(&json!("kiki-fault")))
                            .cloned()
                            .collect();
                        Json(json!({ "total_count": items.len(), "items": items }))
                    },
                ),
            )
            .route(
                "/repos/{owner}/{repo}/issues",
                post(|State(fake): State<Shared>, Json(issue): Json<Value>| async move {
                    let mut fake = fake.lock().unwrap();
                    let created = json!({
                        "number": fake.issues.len() + 1,
                        "title": issue["title"],
                        "body": issue["body"],
                        "labels": issue["labels"],
                        "state": "open",
                        "state_reason": null,
                    });
                    fake.issues.push(created.clone());
                    (StatusCode::CREATED, Json(created))
                }),
            )
            .route(
                "/repos/{owner}/{repo}/issues/{number}",
                get(|State(fake): State<Shared>, Path((_, _, number)): Path<(String, String, i64)>| async move {
                    let fake = fake.lock().unwrap();
                    match fake.issues.iter().find(|i| i["number"] == number) {
                        Some(issue) => Json(issue.clone()).into_response(),
                        None => StatusCode::NOT_FOUND.into_response(),
                    }
                }),
            )
            .route(
                "/repos/{owner}/{repo}/issues/{number}/comments",
                post(
                    |State(fake): State<Shared>,
                     Path((_, _, number)): Path<(String, String, i64)>,
                     Json(comment): Json<Value>| async move {
                        let body = comment["body"].as_str().unwrap().to_owned();
                        fake.lock().unwrap().comments.push((number, body));
                        StatusCode::CREATED
                    },
                ),
            )
            .route(
                "/app/installations/{id}/access_tokens",
                post(|State(fake): State<Shared>, Path(id): Path<String>| async move {
                    assert_eq!(id, "77");
                    fake.lock().unwrap().app_tokens += 1;
                    (StatusCode::CREATED, Json(json!({ "token": "installation-token" })))
                }),
            )
            .layer(middleware::from_fn_with_state(fake.clone(), auth))
            .with_state(fake.clone());
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let url = format!("http://{}", listener.local_addr().unwrap());
        tokio::spawn(async move { axum::serve(listener, router).await.unwrap() });
        (url, fake)
    }

    struct Test {
        test: test_db::TestDb,
        reporter: Reporter,
        fake: Shared,
    }

    async fn start(name: &str) -> Option<Test> {
        let _ = rustls::crypto::ring::default_provider().install_default();
        let test = test_db::migrated(name).await?;
        let (url, fake) = github().await;
        let vars = [
            ("GITHUB_TOKEN", "test-token".to_owned()),
            ("GITHUB_API_URL", url),
            ("FAULT_REPORT_ENVIRONMENT", "test".to_owned()),
        ];
        let reporter = Reporter::from_env(test.db.clone(), move |name| {
            vars.iter()
                .find(|(k, _)| *k == name)
                .map(|(_, v)| v.clone())
        });
        Some(Test {
            test,
            reporter,
            fake,
        })
    }

    struct Event {
        kind: &'static str,
        error_type: Option<&'static str>,
        tool: Option<&'static str>,
        environment: &'static str,
        minutes_ago: i32,
    }

    impl Default for Event {
        fn default() -> Self {
            Self {
                kind: "provider_rejected",
                error_type: Some("ModelHTTPError"),
                tool: None,
                environment: "test",
                minutes_ago: 0,
            }
        }
    }

    impl Test {
        /// Saves `event` as the agent host would; returns its fingerprint.
        async fn insert(&self, event: Event) -> String {
            let fp = fingerprint(
                "agent",
                event.kind,
                event.error_type,
                Some("reply_agent.run"),
                Some(400),
                event.tool,
            );
            sqlx::query(
                "INSERT INTO fault_events (fingerprint, service, kind, job_kind, error_type, stage,
                                           http_status, model, tool, version, environment,
                                           trace_id, run_id, created_at)
                 VALUES ($1, 'agent', $2, 'reply', $3, 'reply_agent.run', 400, 'test/model', $4,
                         'abc123', $5, '0af7651916cd43dd8448eb211c80319c', 'reply:42',
                         now() - make_interval(mins => $6))",
            )
            .bind(&fp)
            .bind(event.kind)
            .bind(event.error_type)
            .bind(event.tool)
            .bind(event.environment)
            .bind(event.minutes_ago)
            .execute(&self.test.db)
            .await
            .unwrap();
            fp
        }

        async fn pass(&mut self) {
            self.reporter.pass().await.unwrap();
        }

        async fn unreported(&self) -> i64 {
            sqlx::query_scalar("SELECT count(*) FROM fault_events WHERE reported_at IS NULL")
                .fetch_one(&self.test.db)
                .await
                .unwrap()
        }

        /// The tracked issue's number, occurrences and unreported count.
        async fn tracked(&self, fp: &str) -> Option<(i32, i64, i64)> {
            sqlx::query_as(
                "SELECT issue_number, occurrences, unreported FROM fault_issues WHERE fingerprint = $1",
            )
            .bind(fp)
            .fetch_optional(&self.test.db)
            .await
            .unwrap()
        }

        /// As if the last comment were long ago.
        async fn age_comments(&self) {
            sqlx::query("UPDATE fault_issues SET last_comment_at = now() - interval '7 hours'")
                .execute(&self.test.db)
                .await
                .unwrap();
        }

        fn issues(&self) -> Vec<Value> {
            self.fake.lock().unwrap().issues.clone()
        }

        fn comments(&self) -> Vec<(i64, String)> {
            self.fake.lock().unwrap().comments.clone()
        }
    }

    #[tokio::test]
    async fn files_one_issue_then_counts_into_it_for_a_later_comment() {
        let Some(mut t) = start("faults_file").await else {
            return;
        };
        let fp = t.insert(Event::default()).await;
        t.insert(Event::default()).await;
        t.pass().await;

        let [issue] = &t.issues()[..] else {
            panic!("one issue: {:?}", t.issues())
        };
        assert_eq!(
            issue["title"],
            "[kiki-fault] provider_rejected: ModelHTTPError in reply_agent.run"
        );
        assert_eq!(
            issue["labels"],
            json!(["kiki-fault", "bug", "service:agent"])
        );
        let body = issue["body"].as_str().unwrap();
        assert!(body.starts_with(&format!("<!-- kiki-fault:{fp} -->")));
        assert!(body.contains(&format!("| Fingerprint | `{fp}` |")));
        assert!(body.contains("| HTTP status | `400` |"));
        assert!(body.contains("| Models | `test/model` |"));
        assert!(body.contains("| Occurrences | 2 ("));
        assert!(body.contains("| Latest job | `reply:42` |"));
        assert_eq!(t.tracked(&fp).await, Some((1, 2, 0)));
        assert_eq!(t.unreported().await, 0);
        assert_eq!(
            t.fake.lock().unwrap().labels,
            ["kiki-fault", "bug", "service:agent", "service:ingester"]
        );

        // Counted, but not commented on so soon after.
        t.insert(Event::default()).await;
        t.pass().await;
        assert_eq!(t.issues().len(), 1);
        assert_eq!(t.tracked(&fp).await, Some((1, 3, 1)));
        assert!(t.comments().is_empty());

        t.age_comments().await;
        t.pass().await;
        let [(1, comment)] = &t.comments()[..] else {
            panic!("one comment: {:?}", t.comments())
        };
        assert!(comment.starts_with("Seen once more since "), "{comment}");
        assert!(comment.contains("(3 in all)") && comment.contains("job `reply:42`"));
        assert_eq!(t.tracked(&fp).await, Some((1, 3, 0)));
        // Nothing new: no comment.
        t.age_comments().await;
        t.pass().await;
        assert_eq!(t.comments().len(), 1);
        t.test.drop().await;
    }

    #[tokio::test]
    async fn adopts_an_issue_it_finds_instead_of_opening_another() {
        let Some(mut t) = start("faults_adopt").await else {
            return;
        };
        let fp = fingerprint(
            "agent",
            "provider_rejected",
            Some("ModelHTTPError"),
            Some("reply_agent.run"),
            Some(400),
            None,
        );
        t.fake.lock().unwrap().issues.push(json!({
            "number": 1,
            "body": format!("| Fingerprint | `{fp}` |"),
            "labels": ["kiki-fault"],
            "state": "open",
            "state_reason": null,
        }));
        t.insert(Event::default()).await;
        t.pass().await;
        assert_eq!(t.issues().len(), 1);
        assert_eq!(t.tracked(&fp).await, Some((1, 1, 0)));
        let [(1, comment)] = &t.comments()[..] else {
            panic!("one comment: {:?}", t.comments())
        };
        assert!(comment.contains("(1 in all)"));
        t.test.drop().await;
    }

    #[tokio::test]
    async fn a_fixed_issue_seen_again_is_a_regression_and_not_planned_mutes() {
        let Some(mut t) = start("faults_closed").await else {
            return;
        };
        let fp = t.insert(Event::default()).await;
        t.pass().await;
        {
            let mut fake = t.fake.lock().unwrap();
            let issue = fake.issue(1);
            issue["state"] = json!("closed");
            issue["state_reason"] = json!("completed");
        }
        t.insert(Event::default()).await;
        t.pass().await;
        let issues = t.issues();
        assert_eq!(issues.len(), 2);
        assert!(
            issues[1]["title"]
                .as_str()
                .unwrap()
                .starts_with("Regression: [kiki-fault]")
        );
        assert!(
            issues[1]["body"]
                .as_str()
                .unwrap()
                .starts_with("Seen again after #1 was closed.")
        );
        assert_eq!(t.tracked(&fp).await, Some((2, 1, 0)));

        {
            let mut fake = t.fake.lock().unwrap();
            let issue = fake.issue(2);
            issue["state"] = json!("closed");
            issue["state_reason"] = json!("not_planned");
        }
        t.insert(Event::default()).await;
        t.age_comments().await;
        t.pass().await;
        assert_eq!(t.issues().len(), 2);
        assert!(t.comments().is_empty());
        assert_eq!(t.tracked(&fp).await, Some((2, 2, 0)));
        assert_eq!(t.unreported().await, 0);
        t.test.drop().await;
    }

    #[tokio::test]
    async fn a_deleted_issue_is_filed_again() {
        let Some(mut t) = start("faults_deleted").await else {
            return;
        };
        let fp = t.insert(Event::default()).await;
        t.pass().await;
        t.fake.lock().unwrap().issues[0]["number"] = json!(100);
        t.insert(Event::default()).await;
        t.pass().await;
        assert_eq!(t.tracked(&fp).await, Some((2, 1, 0)));
        t.test.drop().await;
    }

    #[tokio::test]
    async fn opens_at_most_five_issues_an_hour() {
        let Some(mut t) = start("faults_cap").await else {
            return;
        };
        for error_type in ["A", "B", "C", "D", "E", "F", "G"] {
            t.insert(Event {
                error_type: Some(error_type),
                ..Event::default()
            })
            .await;
        }
        t.pass().await;
        assert_eq!(t.issues().len(), 5);
        assert_eq!(t.unreported().await, 2);
        sqlx::query("UPDATE fault_issues SET filed_at = now() - interval '2 hours'")
            .execute(&t.test.db)
            .await
            .unwrap();
        t.pass().await;
        assert_eq!(t.issues().len(), 7);
        assert_eq!(t.unreported().await, 0);
        t.test.drop().await;
    }

    #[tokio::test]
    async fn quiet_fallbacks_are_filed_only_past_a_rate() {
        let Some(mut t) = start("faults_degraded").await else {
            return;
        };
        let degraded = |minutes_ago| Event {
            kind: TOOL_DEGRADED,
            error_type: Some("HTTPError"),
            tool: Some("embeddings"),
            minutes_ago,
            ..Event::default()
        };
        t.insert(degraded(90)).await;
        for _ in 0..3 {
            t.insert(degraded(0)).await;
        }
        t.pass().await;
        assert!(t.issues().is_empty());
        // The old one is dropped; the recent ones count toward the rate.
        assert_eq!(t.unreported().await, 3);

        t.insert(degraded(0)).await;
        t.insert(degraded(0)).await;
        t.pass().await;
        let [issue] = &t.issues()[..] else {
            panic!("one issue: {:?}", t.issues())
        };
        assert_eq!(
            issue["title"],
            "[kiki-fault] tool_degraded: HTTPError in reply_agent.run (embeddings)"
        );
        assert_eq!(t.unreported().await, 0);
        t.test.drop().await;
    }

    #[tokio::test]
    async fn other_environments_file_nothing() {
        let Some(mut t) = start("faults_environment").await else {
            return;
        };
        t.insert(Event {
            environment: "staging",
            ..Event::default()
        })
        .await;
        t.pass().await;
        assert!(t.issues().is_empty());
        assert_eq!(t.unreported().await, 0);
        t.test.drop().await;
    }

    #[tokio::test]
    async fn a_refused_credential_fails_the_pass_and_files_nothing() {
        let Some(mut t) = start("faults_refused").await else {
            return;
        };
        t.fake.lock().unwrap().refuse = true;
        t.insert(Event::default()).await;
        let Err(github::Error::Unauthorized(message)) = t.reporter.pass().await else {
            panic!("refused")
        };
        // As the run loop does: no more requests, but still failing.
        t.reporter.refused = Some(message);
        t.fake.lock().unwrap().refuse = false;
        assert!(matches!(
            t.reporter.pass().await,
            Err(github::Error::Unauthorized(_))
        ));
        assert!(t.issues().is_empty());
        assert_eq!(t.unreported().await, 1);
        t.test.drop().await;
    }

    #[tokio::test]
    async fn drops_old_events_even_without_a_credential() {
        let Some(t) = start("faults_retention").await else {
            return;
        };
        t.insert(Event {
            minutes_ago: 31 * 24 * 60,
            ..Event::default()
        })
        .await;
        t.insert(Event::default()).await;
        let mut off = Reporter::from_env(t.test.db.clone(), |name| {
            (name == "FAULT_REPORTS").then(|| "off".to_owned())
        });
        off.pass().await.unwrap();
        assert!(t.issues().is_empty());
        assert_eq!(t.unreported().await, 1);
        t.test.drop().await;
    }

    #[tokio::test]
    async fn files_as_a_github_app_with_an_installation_token() {
        let _ = rustls::crypto::ring::default_provider().install_default();
        let Some(test) = test_db::migrated("faults_app").await else {
            return;
        };
        let (url, fake) = github().await;
        let vars = [
            ("GITHUB_APP_ID", "12345".to_owned()),
            ("GITHUB_APP_INSTALLATION_ID", "77".to_owned()),
            (
                "GITHUB_APP_PRIVATE_KEY",
                super::super::github::tests::TEST_KEY.to_owned(),
            ),
            ("GITHUB_API_URL", url),
            ("FAULT_REPORT_ENVIRONMENT", "test".to_owned()),
        ];
        let reporter = Reporter::from_env(test.db.clone(), move |name| {
            vars.iter()
                .find(|(k, _)| *k == name)
                .map(|(_, v)| v.clone())
        });
        let mut t = Test {
            test,
            reporter,
            fake,
        };
        t.insert(Event::default()).await;
        t.pass().await;
        t.insert(Event {
            error_type: Some("Other"),
            ..Event::default()
        })
        .await;
        t.pass().await;
        assert_eq!(t.issues().len(), 2);
        // One token, reused.
        assert_eq!(t.fake.lock().unwrap().app_tokens, 1);
        t.test.drop().await;
    }

    #[test]
    fn issue_text_keeps_identifiers_only() {
        assert_eq!(clean("ModelHTTPError"), "ModelHTTPError");
        assert_eq!(clean("a `b` <c> @everyone\nd"), "abceveryoned");
        assert_eq!(clean(&"x".repeat(300)).len(), 100);
        assert_eq!(code(Some("**")), "–");
    }
}
