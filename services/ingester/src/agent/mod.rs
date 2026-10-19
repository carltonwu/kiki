//! Agent: answers routed pools (`agent_runs`) in Discord.
//!
//! ```text
//! agent_runs INSERT ─NOTIFY→ dispatch (one task per conversation) → claim newest run
//!     → context → Python host (HTTP) → reply in Discord → tag → refresh topic
//!                                        └→ queued drawings, posted as follow-ups
//! ```
//!
//! Runs of one conversation are answered one at a time. When several are pending
//! (people kept talking while Kiki was replying), only the newest is answered and
//! the others are marked `superseded`: their messages are in the history anyway.
//! Drawings run on their own tasks ([`drawing`]), so the conversation carries on.

mod context;
mod delivery;
mod discord;
mod drawing;
pub(crate) mod emoji;
pub(crate) mod host;
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

pub struct Agent {
    db: PgPool,
    http: Arc<Client>,
    host: host::Host,
    config: AgentConfig,
    settings: Arc<crate::guild_settings::Store>,
    bot_user_id: i64,
    pending_emoji: String,
    draining: watch::Sender<bool>,
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
    /// Reactions Discord accepted, as written in message text.
    reactions: Vec<String>,
    reacted_to: Option<i64>,
    model: Option<String>,
    steps: i32,
    usage: Usage,
    /// Pictures the reply queued.
    drawings: usize,
}

impl Agent {
    pub fn new(
        db: PgPool,
        http: Arc<Client>,
        config: AgentConfig,
        bot_user_id: i64,
        pending_emoji: String,
        settings: Arc<crate::guild_settings::Store>,
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
            settings,
            bot_user_id,
            pending_emoji,
            draining: watch::channel(false).0,
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
        self.recover_drawings().await?;

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
                // Also picks up runs queued before startup (first tick is immediate),
                // and drawings left pending by a restart.
                _ = poll.tick() => {
                    self.dispatch(&mut workers).await;
                    self.spawn_drawings(None).await;
                }
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

        self.draining.send_replace(true);
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

    #[tracing::instrument(
        name = "agent_run",
        skip_all,
        fields(
            run_id = claim.run_id,
            conversation_id = claim.conversation_id,
            channel_id = claim.channel_id
        )
    )]
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
                reactions = ?record.reactions,
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
                reactions = ?record.reactions,
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
        // Keep the pending emoji where Kiki chose it as her reaction.
        let mut pending = claim.trigger_message_ids.clone();
        if record.reactions.contains(&self.pending_emoji) {
            pending.retain(|&id| Some(id) != record.reacted_to);
        }
        // A queued picture nothing acknowledged: the pending emoji says it's
        // coming until it's posted.
        if record.drawings > 0 && record.reply_message_ids.is_empty() && record.reactions.is_empty()
        {
            pending.clear();
        }
        self.clear_pending(claim.channel_id, &pending);
        if record.drawings > 0 {
            self.spawn_drawings(Some(claim.run_id)).await;
        }
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
        // The allowlist changed after routing, leaving nowhere to reply.
        let Some(target) = self.reply_target(&context).await? else {
            debug!(run_id = claim.run_id, "channel no longer allowed; skipping");
            return Ok(());
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
                    model: context.settings.chat_model.clone(),
                    messages: context.messages.clone(),
                    max_output_tokens: context.settings.reply_max_tokens,
                    people: context.people.clone(),
                    image_model: context
                        .settings
                        .image_generation
                        .then(|| context.settings.image_model.clone()),
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
        let images = self.load_images(&request.id, &result.images).await?;
        let mut plan = delivery::plan(&result.text, &context.settings)?;
        emoji::resolve(&self.db, context.guild_id, &mut plan).await?;
        if target.redirect.is_some() {
            // Reactions would land in the channel the allowlist keeps her out of.
            plan.reactions.clear();
        }
        if !result.drawings.is_empty() {
            // Kiki's own words: the heads-up that a picture is coming.
            let ack: Vec<&str> = plan.messages.iter().map(|m| m.text.as_str()).collect();
            self.queue_drawings(claim, &context, &request, &result.drawings, &ack.join("\n"))
                .await?;
            record.drawings = result.drawings.len();
        }
        if plan.messages.is_empty() && !images.is_empty() {
            // Someone asked for the picture; don't drop it over a silence marker.
            plan.messages.push(delivery::Message {
                text: String::new(),
                delay: Duration::ZERO,
            });
        }
        if plan.messages.is_empty() && plan.reactions.is_empty() {
            debug!(run_id = claim.run_id, "agent chose silence");
            return Ok(());
        }
        // Commit before any Discord send: a crash from here may have posted a
        // reply, so startup fails this run instead of posting it again.
        sqlx::query("UPDATE agent_runs SET status = 'delivering', reply_to_id = $2 WHERE id = $1")
            .bind(claim.run_id)
            .bind(context.reply_to)
            .execute(&self.db)
            .await?;
        self.react(&context, &plan.reactions, record).await;
        if plan.messages.is_empty() {
            debug!(run_id = claim.run_id, "agent only reacted");
            return Ok(());
        }
        self.send(
            &context,
            claim.run_id,
            claim.conversation_id,
            &plan.messages,
            &images,
            context.settings.chat_reply_reference,
            record,
        )
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
                steps = $6, prompt_tokens = $7, completion_tokens = $8, cost_usd = $9, latency_ms = $10,
                reactions = $11
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
        .bind(&record.reactions)
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
    fix_custom_emoji(strip_speaker(text.trim()).trim())
}

/// Drops a leading `Kiki:`, as models sometimes label their own lines.
fn strip_speaker(text: &str) -> &str {
    ["Kiki:", "**Kiki**:", "[Kiki]:", "[Kiki]"]
        .iter()
        .find_map(|prefix| text.strip_prefix(prefix))
        .unwrap_or(text)
}

/// Models often write a custom emoji as `<:name:id:>`, which Discord shows as
/// text. Drops the colon after the ID.
fn fix_custom_emoji(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    let mut rest = text;
    while let Some(i) = rest.find('<') {
        let (before, at) = rest.split_at(i);
        let (fixed, len) = match emoji::custom(at) {
            Some((reaction, len)) if at[..len].ends_with(":>") => (reaction.to_string(), len),
            _ => ("<".to_owned(), 1),
        };
        out.push_str(before);
        out.push_str(&fixed);
        rest = &at[len..];
    }
    out.push_str(rest);
    out
}

/// Kiki's answer when her previous reply already covered the messages (see
/// `context`), give or take formatting.
fn is_no_reply(text: &str) -> bool {
    text.trim_matches(|c: char| !c.is_alphanumeric() && c != '_') == "NO_REPLY"
}

#[cfg(test)]
mod tests {
    use super::{clean_reply, fix_custom_emoji, is_no_reply};

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
        assert_eq!(
            clean_reply("ok <:froge:814672652526420008:> and <a:ape:1444819174172922043:>"),
            "ok <:froge:814672652526420008> and <a:ape:1444819174172922043>"
        );
        assert_eq!(
            clean_reply("fine <:froge:81> :> 12:>"),
            "fine <:froge:81> :> 12:>"
        );
    }

    #[test]
    fn fixes_custom_emoji() {
        for (text, fixed) in [
            (
                "<a:ape_2:1444819174172922043:>!",
                "<a:ape_2:1444819174172922043>!",
            ),
            ("a < b <:x:5:> c <:x:6> :>", "a < b <:x:5> c <:x:6> :>"),
            ("<", "<"),
            ("< :a:5:> <b:a:5:> <::5:>", "< :a:5:> <b:a:5:> <::5:>"),
            ("é <:x:5:>", "é <:x:5>"),
        ] {
            assert_eq!(fix_custom_emoji(text), fixed, "{text}");
        }
    }
}
