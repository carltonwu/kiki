//! Agent: answers routed pools (`agent_runs`) in Discord.
//!
//! ```text
//! agent_runs INSERT ─NOTIFY→ dispatch (one task per conversation) → claim newest run
//!     → context → Python host (HTTP) → reply in Discord → tag → refresh topic
//! ```
//!
//! Runs of one conversation are answered one at a time. When several are pending
//! (people kept talking while Kiki was replying), only the newest is answered and
//! the others are marked `superseded`: their messages are in the history anyway.

mod context;
mod discord;
mod host;
mod people;
mod topic;

use std::{
    collections::{HashMap, HashSet},
    sync::Arc,
    time::{Duration, Instant},
};

use anyhow::bail;
use sqlx::{PgPool, postgres::PgListener};
use tokio::{sync::watch, task::JoinSet};
use tracing::{debug, info, warn};
use twilight_http::Client;

use crate::{chat::Usage, config::AgentConfig};

const NOTIFY_CHANNEL: &str = "agent_run_queued";
/// Pending runs are also checked this often, in case a notification was lost
/// while the listener reconnected.
const POLL_INTERVAL: Duration = Duration::from_secs(30);
/// At shutdown, replies in progress get this long to finish (Railway drains for 30 s).
const DRAIN_TIMEOUT: Duration = Duration::from_secs(20);
const MAX_REPLY_TOKENS: u32 = 2000;

pub struct Agent {
    db: PgPool,
    http: Arc<Client>,
    host: host::Host,
    config: AgentConfig,
    bot_user_id: i64,
    pending_emoji: String,
}

struct Claim {
    run_id: i64,
    conversation_id: i64,
    channel_id: i64,
    /// Trigger messages of every claimed run, oldest first.
    trigger_message_ids: Vec<i64>,
    created_at_ms: i64,
    host_request: Option<serde_json::Value>,
}

#[derive(Default)]
struct RunRecord {
    reply_message_ids: Vec<i64>,
    model: Option<String>,
    steps: i32,
    usage: Usage,
}

impl Agent {
    pub fn new(
        db: PgPool,
        http: Arc<Client>,
        config: AgentConfig,
        bot_user_id: i64,
        pending_emoji: String,
    ) -> anyhow::Result<Self> {
        info!(
            chat_model = config.chat_model,
            topic_model = config.topic_model,
            "agent ready"
        );
        Ok(Self {
            db,
            http,
            host: host::Host::new(&config)?,
            config,
            bot_user_id,
            pending_emoji,
        })
    }

    pub async fn run(self: Arc<Self>, mut shutdown: watch::Receiver<bool>) -> anyhow::Result<()> {
        // Delivery is not safely retryable after a crash. Host execution can
        // reuse a saved result through its stable job ID.
        let interrupted = sqlx::query(
            "UPDATE agent_runs SET status = 'failed', error = 'interrupted', finished_at = now() \
             WHERE status = 'delivering'",
        )
        .execute(&self.db)
        .await?
        .rows_affected();
        if interrupted > 0 {
            warn!(
                runs = interrupted,
                "marked interrupted agent runs as failed"
            );
        }

        let recovered =
            sqlx::query("UPDATE agent_runs SET status = 'pending' WHERE status = 'running'")
                .execute(&self.db)
                .await?
                .rows_affected();
        if recovered > 0 {
            info!(runs = recovered, "requeued interrupted agent runs");
        }

        let mut listener = PgListener::connect_with(&self.db).await?;
        listener.listen(NOTIFY_CHANNEL).await?;
        info!("agent listening for runs");

        let mut workers = Workers::default();
        let mut poll = tokio::time::interval(POLL_INTERVAL);
        loop {
            tokio::select! {
                notification = listener.recv() => match notification {
                    Ok(n) => match n.payload().parse() {
                        Ok(conversation_id) => workers.start(&self, conversation_id),
                        Err(_) => warn!(payload = n.payload(), "unparseable agent run notification"),
                    },
                    Err(e) => {
                        warn!(error = %e, "agent lost its Postgres listener; reconnecting");
                        tokio::time::sleep(Duration::from_secs(1)).await;
                    }
                },
                // Also picks up runs queued before startup (first tick is immediate).
                _ = poll.tick() => self.dispatch(&mut workers).await,
                Some(done) = workers.tasks.join_next_with_id() => {
                    let id = match &done {
                        Ok((id, _)) => *id,
                        Err(e) => {
                            warn!(conversation_id = workers.by_task.get(&e.id()), error = %e, "agent task panicked");
                            e.id()
                        }
                    };
                    if let Some(conversation_id) = workers.by_task.remove(&id) {
                        workers.busy.remove(&conversation_id);
                    }
                    // A run queued while that conversation was busy was skipped.
                    self.dispatch(&mut workers).await;
                }
                _ = async { shutdown.wait_for(|stop| *stop).await.map(drop) } => break,
            }
        }

        let busy = workers.tasks.len();
        if busy > 0 {
            info!(tasks = busy, "waiting for agent replies to finish");
        }
        let drained = tokio::time::timeout(DRAIN_TIMEOUT, async {
            while workers.tasks.join_next().await.is_some() {}
        })
        .await;
        if drained.is_err() {
            warn!(
                tasks = workers.tasks.len(),
                "agent replies still running at shutdown; aborting them"
            );
            workers.tasks.abort_all();
        }
        info!(
            drained = busy - workers.tasks.len(),
            aborted = workers.tasks.len(),
            "agent stopped"
        );
        Ok(())
    }

    async fn dispatch(self: &Arc<Self>, workers: &mut Workers) {
        let pending: sqlx::Result<Vec<i64>> = sqlx::query_scalar(
            "SELECT DISTINCT conversation_id FROM agent_runs WHERE status = 'pending'",
        )
        .fetch_all(&self.db)
        .await;
        match pending {
            Ok(ids) => ids.into_iter().for_each(|id| workers.start(self, id)),
            Err(e) => warn!(error = %e, "couldn't list pending agent runs"),
        }
    }

    async fn serve(self: Arc<Self>, conversation_id: i64) {
        loop {
            match self.claim(conversation_id).await {
                Ok(Some(claim)) => self.answer(claim).await,
                Ok(None) => return,
                Err(e) => {
                    warn!(
                        conversation_id,
                        error = format!("{e:#}"),
                        "couldn't claim agent run"
                    );
                    return;
                }
            }
        }
    }

    async fn claim(&self, conversation_id: i64) -> anyhow::Result<Option<Claim>> {
        let mut tx = self.db.begin().await?;
        let runs: Vec<(i64, Vec<i64>, i64, Option<serde_json::Value>)> = sqlx::query_as(
            r#"
            SELECT id, trigger_message_ids, (extract(epoch FROM created_at) * 1000)::bigint, host_request
            FROM agent_runs WHERE conversation_id = $1 AND status = 'pending'
            ORDER BY id FOR UPDATE SKIP LOCKED
            "#,
        )
        .bind(conversation_id)
        .fetch_all(&mut *tx)
        .await?;
        let Some((run_id, _, created_at_ms, host_request)) = runs.last() else {
            return Ok(None);
        };
        let (run_id, created_at_ms, host_request) = (*run_id, *created_at_ms, host_request.clone());
        let channel_id: i64 =
            sqlx::query_scalar("SELECT channel_id FROM conversations WHERE id = $1")
                .bind(conversation_id)
                .fetch_one(&mut *tx)
                .await?;
        let older: Vec<i64> = runs[..runs.len() - 1].iter().map(|r| r.0).collect();
        sqlx::query(
            "UPDATE agent_runs SET status = 'superseded', finished_at = now() WHERE id = ANY($1)",
        )
        .bind(&older)
        .execute(&mut *tx)
        .await?;
        sqlx::query("UPDATE agent_runs SET status = 'running', started_at = now() WHERE id = $1")
            .bind(run_id)
            .execute(&mut *tx)
            .await?;
        tx.commit().await?;

        let mut trigger_message_ids: Vec<i64> =
            runs.into_iter().flat_map(|(_, ids, _, _)| ids).collect();
        trigger_message_ids.sort_unstable();
        trigger_message_ids.dedup();
        debug!(
            run_id,
            conversation_id,
            channel_id,
            superseded = older.len(),
            messages = trigger_message_ids.len(),
            "claimed agent run"
        );
        Ok(Some(Claim {
            run_id,
            conversation_id,
            channel_id,
            trigger_message_ids,
            created_at_ms,
            host_request,
        }))
    }

    async fn answer(self: &Arc<Self>, claim: Claim) {
        let started = Instant::now();
        let mut record = RunRecord::default();
        let result = self.reply(&claim, &mut record).await;
        let latency_ms = started.elapsed().as_millis() as i32;

        let error = result.as_ref().err().map(|e| format!("{e:#}"));
        match &error {
            None => info!(
                run_id = claim.run_id,
                conversation_id = claim.conversation_id,
                channel_id = claim.channel_id,
                reply_message_ids = ?record.reply_message_ids,
                model = record.model,
                steps = record.steps,
                prompt_tokens = record.usage.prompt_tokens,
                completion_tokens = record.usage.completion_tokens,
                cost_usd = record.usage.cost,
                latency_ms,
                "agent run completed"
            ),
            Some(error) => warn!(
                run_id = claim.run_id,
                conversation_id = claim.conversation_id,
                channel_id = claim.channel_id,
                reply_message_ids = ?record.reply_message_ids,
                latency_ms,
                error,
                "agent run failed"
            ),
        }
        if let Err(e) = self
            .finish(&claim, &record, error.as_deref(), latency_ms)
            .await
        {
            warn!(run_id = claim.run_id, error = %e, "couldn't record agent run");
        }
        self.clear_pending(claim.channel_id, &claim.trigger_message_ids);
        if result.is_ok() {
            self.spawn_topic_refresh(claim.conversation_id);
        }
    }

    async fn reply(&self, claim: &Claim, record: &mut RunRecord) -> anyhow::Result<()> {
        let age = Duration::from_millis(
            (crate::router::unix_millis() - claim.created_at_ms).max(0) as u64,
        );
        if age > self.config.run_max_age {
            bail!("expired: queued {}s ago", age.as_secs());
        }
        let Some(context) = self
            .context(claim.conversation_id, &claim.trigger_message_ids)
            .await?
        else {
            bail!("trigger messages were deleted");
        };

        let request = match &claim.host_request {
            Some(value) => serde_json::from_value(value.clone())?,
            None => {
                let request = host::JobRequest {
                    id: format!("reply:{}", claim.run_id),
                    kind: "reply".into(),
                    scope: host::Scope::new(
                        context.guild_id,
                        context.channel_id,
                        claim.conversation_id,
                    ),
                    model: self.config.chat_model.clone(),
                    messages: context.messages.clone(),
                    max_output_tokens: MAX_REPLY_TOKENS,
                    people: context.people.clone(),
                };
                sqlx::query("UPDATE agent_runs SET host_request = $2 WHERE id = $1")
                    .bind(claim.run_id)
                    .bind(serde_json::to_value(&request)?)
                    .execute(&self.db)
                    .await?;
                request
            }
        };
        let result = self.host.execute(&request).await?;
        record.steps = result.steps;
        record.usage = result.usage;
        record.model = result.model;
        let text = clean_reply(&result.text);
        if text.is_empty() {
            bail!("agent host returned an empty reply");
        }
        if is_no_reply(&text) {
            debug!(
                run_id = claim.run_id,
                "the previous reply already covered these messages"
            );
            return Ok(());
        }
        // Commit before any Discord send: a crash from here may have posted a
        // reply, so startup fails this run instead of posting it again.
        sqlx::query("UPDATE agent_runs SET status = 'delivering' WHERE id = $1")
            .bind(claim.run_id)
            .execute(&self.db)
            .await?;
        self.send(&context, claim.conversation_id, &text, record)
            .await
    }

    async fn finish(
        &self,
        claim: &Claim,
        record: &RunRecord,
        error: Option<&str>,
        latency_ms: i32,
    ) -> sqlx::Result<()> {
        sqlx::query(
            r#"
            UPDATE agent_runs
            SET status = $2, error = $3, finished_at = now(), reply_message_ids = $4, model = $5,
                steps = $6, prompt_tokens = $7, completion_tokens = $8, cost_usd = $9, latency_ms = $10
            WHERE id = $1
            "#,
        )
        .bind(claim.run_id)
        .bind(if error.is_some() { "failed" } else { "done" })
        .bind(error)
        .bind(&record.reply_message_ids)
        .bind(&record.model)
        .bind(record.steps)
        .bind(record.usage.prompt_tokens)
        .bind(record.usage.completion_tokens)
        .bind(record.usage.cost)
        .bind(latency_ms)
        .execute(&self.db)
        .await?;
        Ok(())
    }
}

#[derive(Default)]
struct Workers {
    tasks: JoinSet<()>,
    busy: HashSet<i64>,
    by_task: HashMap<tokio::task::Id, i64>,
}

impl Workers {
    fn start(&mut self, agent: &Arc<Agent>, conversation_id: i64) {
        if !self.busy.insert(conversation_id) {
            return; // its worker claims new runs before exiting
        }
        let agent = agent.clone();
        let handle = self
            .tasks
            .spawn(async move { agent.serve(conversation_id).await });
        self.by_task.insert(handle.id(), conversation_id);
    }
}

fn clean_reply(text: &str) -> String {
    let text = text.trim();
    let text = ["Kiki:", "**Kiki**:", "[Kiki]:", "[Kiki]"]
        .iter()
        .find_map(|prefix| text.strip_prefix(prefix))
        .unwrap_or(text);
    text.trim().to_owned()
}

/// Kiki's answer when her previous reply already covered the messages (see
/// `context`), give or take formatting.
fn is_no_reply(text: &str) -> bool {
    text.trim_matches(|c: char| !c.is_alphanumeric() && c != '_') == "NO_REPLY"
}

#[cfg(test)]
mod tests {
    use super::{clean_reply, is_no_reply};

    #[test]
    fn recognizes_no_reply() {
        assert!(is_no_reply("NO_REPLY"));
        assert!(is_no_reply("`NO_REPLY`."));
        assert!(!is_no_reply("NO_REPLY, but also: hi"));
    }

    #[test]
    fn strips_name_prefix() {
        assert_eq!(clean_reply("  Kiki: hey there \n"), "hey there");
        assert_eq!(clean_reply("[Kiki] hey"), "hey");
        assert_eq!(clean_reply("hey Kiki: there"), "hey Kiki: there");
        assert_eq!(clean_reply("   "), "");
    }
}
