//! Pictures a reply queued (docs/image-generation.md).
//!
//! ```text
//! reply job result { text: ack, drawings: [...] } → agent_image_jobs (pending)
//!     → ack posted (the run) → draw: host image job `image:<run>:<n>`
//!     → follow-up message replying to the request: the image and Kiki's caption,
//!       or Kiki's note on why it failed
//! ```
//!
//! Each drawing is its own row and host job, so a retry reuses the saved image
//! rather than drawing (and paying) again, and the conversation can carry on while
//! it draws. Like runs, a crash mid-send fails the row rather than posting twice.

use std::{sync::Arc, time::Duration};

use anyhow::Context as _;
use tracing::{debug, info, warn};

use super::{
    Agent, Claim, RunRecord,
    context::Context,
    delivery, discord,
    host::{self, ImageJobRequest, ImageRef},
};

/// Posted when the host couldn't even write Kiki's own note (host down, or the
/// chat model failed too): the request must not end in silence.
const FALLBACK: &str = "(couldn't make that picture)";
/// A drawing queued longer ago than this isn't drawn: after an outage, a
/// picture nobody's waiting for anymore would only interrupt.
const MAX_AGE: Duration = Duration::from_secs(15 * 60);

struct Job {
    id: i64,
    run_id: i64,
    reply_to: i64,
    request: ImageJobRequest,
    expired: bool,
    queued_ms: i64,
}

/// `drawing` with the images shared with the request that none of its subjects
/// use (`Drawing.shared` on the host), so the attachment, the meme replied to or
/// the photo linked reaches the image model even when Kiki didn't name it.
fn with_shared(drawing: &serde_json::Value, shared: &[i64]) -> serde_json::Value {
    let mut drawing = drawing.clone();
    let used: Vec<&str> = drawing["subjects"]
        .as_array()
        .into_iter()
        .flatten()
        .flat_map(|s| s["photos"].as_array().into_iter().flatten())
        .filter_map(|p| p.as_str())
        .collect();
    let unused: Vec<String> = shared
        .iter()
        .map(i64::to_string)
        .filter(|id| !used.contains(&id.as_str()))
        .collect();
    if !unused.is_empty()
        && let Some(object) = drawing.as_object_mut()
    {
        object.insert("shared".into(), unused.into());
    }
    drawing
}

impl Agent {
    /// Saves the reply's drawings as pending image jobs, before anything is
    /// posted, so a crash can't lose a requested picture. Idempotent per run.
    pub(super) async fn queue_drawings(
        &self,
        claim: &Claim,
        context: &Context,
        job: &host::JobRequest,
        drawings: &[serde_json::Value],
        ack: &str,
    ) -> anyhow::Result<()> {
        let Some(image_model) = &job.image_model else {
            anyhow::bail!("the host queued a drawing without an image model");
        };
        for (n, drawing) in drawings.iter().enumerate() {
            let request = ImageJobRequest {
                id: format!("image:{}:{}", claim.run_id, n + 1),
                scope: host::Scope::new(
                    context.guild_id,
                    context.channel_id,
                    claim.conversation_id,
                ),
                image_model: image_model.clone(),
                reply_job: job.id.clone(),
                chat_model: job.model.clone(),
                ack: ack.to_owned(),
                drawing: with_shared(drawing, &context.shared),
            };
            sqlx::query(
                "INSERT INTO agent_image_jobs (run_id, position, reply_to_id, request) \
                 VALUES ($1, $2, $3, $4) ON CONFLICT (run_id, position) DO NOTHING",
            )
            .bind(claim.run_id)
            .bind(n as i16 + 1)
            .bind(context.reply_to)
            .bind(serde_json::to_value(&request)?)
            .execute(&self.db)
            .await?;
        }
        debug!(
            run_id = claim.run_id,
            drawings = drawings.len(),
            "queued drawings"
        );
        Ok(())
    }

    /// Starts drawing the pending pictures of `run_id`, or of every run that's
    /// finished (after a restart or a lost task). A job is claimed atomically, so
    /// starting one twice is harmless.
    pub(super) async fn spawn_drawings(self: &Arc<Self>, run_id: Option<i64>) {
        let ids: sqlx::Result<Vec<i64>> = sqlx::query_scalar(
            "SELECT j.id FROM agent_image_jobs j JOIN agent_runs r ON r.id = j.run_id \
             WHERE j.status = 'pending' AND ($1::bigint IS NULL OR j.run_id = $1) \
               AND r.status IN ('done', 'failed', 'superseded') ORDER BY j.id",
        )
        .bind(run_id)
        .fetch_all(&self.db)
        .await;
        match ids {
            Ok(ids) => {
                for id in ids {
                    tokio::spawn(self.clone().draw(id));
                }
            }
            Err(e) => warn!(error = %e, "couldn't list pending drawings"),
        }
    }

    /// At startup: a drawing cut off mid-send may have posted, so it fails; one
    /// cut off while drawing is drawn again (the host returns its saved image).
    pub(super) async fn recover_drawings(&self) -> sqlx::Result<()> {
        sqlx::query(
            "UPDATE agent_image_jobs SET status = 'failed', error = 'interrupted', \
             finished_at = now() WHERE status = 'delivering'",
        )
        .execute(&self.db)
        .await?;
        sqlx::query("UPDATE agent_image_jobs SET status = 'pending' WHERE status = 'drawing'")
            .execute(&self.db)
            .await?;
        Ok(())
    }

    async fn draw(self: Arc<Self>, id: i64) {
        let job = match self.claim_drawing(id).await {
            Ok(Some(job)) => job,
            Ok(None) => return,
            Err(e) => {
                return warn!(
                    image_job_id = id,
                    error = format!("{e:#}"),
                    "couldn't claim drawing"
                );
            }
        };
        if job.expired {
            info!(
                image_job_id = id,
                run_id = job.run_id,
                "drawing expired before it started"
            );
            let _ = self
                .finish_drawing(id, "failed", Some("expired"), None, None, None)
                .await;
            return;
        }
        let (status, error) = match self.deliver_drawing(&job).await {
            Ok(None) => ("done", None),
            Ok(Some(reason)) => ("failed", Some(reason)),
            Err(e) => ("failed", Some(format!("{e:#}"))),
        };
        match &error {
            None => info!(image_job_id = id, run_id = job.run_id, "drawing posted"),
            Some(error) => warn!(
                image_job_id = id,
                run_id = job.run_id,
                error,
                "drawing failed"
            ),
        }
        if status == "failed" {
            let _ = sqlx::query(
                "UPDATE agent_image_jobs SET status = 'failed', error = coalesce(error, $2), \
                 finished_at = coalesce(finished_at, now()) WHERE id = $1 AND status <> 'done'",
            )
            .bind(id)
            .bind(&error)
            .execute(&self.db)
            .await;
        }
        // The pending emoji stays on the request until the picture is there when
        // Kiki didn't ack it.
        let triggers: Vec<i64> =
            sqlx::query_scalar("SELECT trigger_message_ids FROM agent_runs WHERE id = $1")
                .bind(job.run_id)
                .fetch_optional(&self.db)
                .await
                .ok()
                .flatten()
                .unwrap_or_default();
        let channel_id: i64 = job.request.scope.channel_id.parse().unwrap_or_default();
        self.clear_pending(channel_id, &triggers);
    }

    async fn claim_drawing(&self, id: i64) -> anyhow::Result<Option<Job>> {
        let row: Option<(i64, i64, serde_json::Value, bool, i64)> = sqlx::query_as(
            "UPDATE agent_image_jobs SET status = 'drawing' WHERE id = $1 AND status = 'pending' \
             RETURNING run_id, reply_to_id, request, created_at < now() - make_interval(secs => $2), \
                       (extract(epoch FROM created_at) * 1000)::bigint",
        )
        .bind(id)
        .bind(MAX_AGE.as_secs_f64())
        .fetch_optional(&self.db)
        .await?;
        let Some((run_id, reply_to, request, expired, queued_ms)) = row else {
            return Ok(None);
        };
        Ok(Some(Job {
            id,
            run_id,
            reply_to,
            request: serde_json::from_value(request)?,
            expired,
            queued_ms,
        }))
    }

    /// Draws and posts; `Some(reason)` when what was posted is a failure notice.
    async fn deliver_drawing(&self, job: &Job) -> anyhow::Result<Option<String>> {
        let scope = &job.request.scope;
        let guild_id: i64 = scope.guild_id.parse()?;
        let channel_id: i64 = scope.channel_id.parse()?;
        let conversation_id: i64 = scope.conversation_id.parse()?;

        let drawn = self.host.draw(&job.request).await;
        let (text, image, cost, failure) = match drawn {
            Ok(r) => match r.image {
                Some(image) => (r.caption, Some(image), r.cost, None),
                None => {
                    let reason = r.error.unwrap_or_default();
                    let note = r.failure_note.trim().to_owned();
                    let text = if note.is_empty() {
                        FALLBACK.to_owned()
                    } else {
                        note
                    };
                    (text, None, r.cost, Some(reason))
                }
            },
            Err(e) => (FALLBACK.to_owned(), None, None, Some(format!("{e:#}"))),
        };
        let attachments = match &image {
            Some(image) => {
                self.load_images(&job.request.id, std::slice::from_ref(image))
                    .await?
            }
            None => Vec::new(),
        };
        let mut messages: Vec<delivery::Message> =
            discord::split_message(&super::clean_reply(&text), discord::MAX_MESSAGE_CHARS)
                .into_iter()
                .map(|text| delivery::Message {
                    text,
                    delay: Duration::ZERO,
                })
                .collect();
        if messages.is_empty() {
            messages.push(delivery::Message {
                text: String::new(),
                delay: Duration::ZERO,
            });
        }

        // Commit before the send, as runs do: a crash from here fails the row
        // instead of posting it twice.
        sqlx::query("UPDATE agent_image_jobs SET status = 'delivering' WHERE id = $1")
            .bind(job.id)
            .execute(&self.db)
            .await?;
        let context = Context {
            guild_id,
            settings: self.settings.snapshot(guild_id),
            channel_id,
            reply_to: job.reply_to,
            messages: Vec::new(),
            people: Vec::new(),
            shared: Vec::new(),
        };
        // The run's earlier messages stay attributed to it; the follow-up joins them,
        // so replies to the picture reach the same conversation.
        let mut record = RunRecord {
            reply_message_ids: sqlx::query_scalar(
                "SELECT coalesce(reply_message_ids, '{}') FROM agent_runs WHERE id = $1",
            )
            .bind(job.run_id)
            .fetch_one(&self.db)
            .await?,
            ..RunRecord::default()
        };
        let before = record.reply_message_ids.len();
        // Always a reply to the request: it arrives after other messages.
        self.send(
            &context,
            job.run_id,
            conversation_id,
            &messages,
            &attachments,
            true,
            &mut record,
        )
        .await
        .context("posting the drawing")?;
        let posted = record.reply_message_ids.get(before).copied();
        let latency_ms = (crate::router::unix_millis() - job.queued_ms).max(0) as i32;
        self.finish_drawing(
            job.id,
            if failure.is_some() { "failed" } else { "done" },
            failure.as_deref(),
            image.as_ref().map(|i: &ImageRef| i.id),
            posted,
            Some((cost, latency_ms)),
        )
        .await?;
        Ok(failure)
    }

    async fn finish_drawing(
        &self,
        id: i64,
        status: &str,
        error: Option<&str>,
        image_id: Option<i64>,
        message_id: Option<i64>,
        spent: Option<(Option<f64>, i32)>,
    ) -> sqlx::Result<()> {
        let (cost, latency_ms) = spent.unwrap_or((None, 0));
        sqlx::query(
            "UPDATE agent_image_jobs SET status = $2, error = $3, image_id = $4, message_id = $5, \
             cost_usd = $6, latency_ms = $7, finished_at = now() WHERE id = $1",
        )
        .bind(id)
        .bind(status)
        .bind(error)
        .bind(image_id)
        .bind(message_id)
        .bind(cost)
        .bind(spent.map(|_| latency_ms))
        .execute(&self.db)
        .await?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    #[test]
    fn shared_images_skip_the_subjects_photos() {
        let drawing = json!({"prompt": "x", "subjects": [{"name": "Nico", "photos": ["30"]}]});
        let with = super::with_shared(&drawing, &[35, 30]);
        assert_eq!(with["shared"], json!(["35"]));
        assert_eq!(with["subjects"], drawing["subjects"]);
        // Nothing to add: the drawing is sent as the host returned it.
        assert_eq!(super::with_shared(&drawing, &[30]), drawing);
        assert_eq!(super::with_shared(&drawing, &[]), drawing);
    }
}
