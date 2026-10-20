//! Sending Kiki's reply to Discord, and tagging it into the conversation.

use std::time::Duration;

use anyhow::{Context as _, ensure};
use serde_json::Value;
use tracing::{Instrument, debug, field::Empty, info_span, warn};
use twilight_http::request::channel::reaction::RequestReactionType;
use twilight_model::{channel::message::AllowedMentions, http::attachment::Attachment, id::Id};

use super::{Agent, RunRecord, context::Context, emoji::Reaction, host::ImageRef};
use crate::{
    guild_settings::reply_channel,
    metrics::{self, Send},
    router::unix_millis,
    store,
};

/// Discord allows 2000 characters per message; leave room for safety.
pub(super) const MAX_MESSAGE_CHARS: usize = 1900;

/// Starts the first message of a reply posted in the default channel, because
/// the channel it answers isn't on the server's allowlist. The prompt leaves
/// this line out (`context::MESSAGE_COLUMNS`).
const REDIRECT_PREFIX: &str = "-# ↪ ";

/// Who and what a redirected reply answers: subtext, so it stays out of the way,
/// and a link back to the message. About 120 characters.
fn redirect_header(guild_id: i64, channel_id: i64, message_id: i64, author_id: i64) -> String {
    format!(
        "{REDIRECT_PREFIX}<@{author_id}> in https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"
    )
}

/// Puts the header on the first message, or before it when both don't fit in
/// one Discord message.
fn with_header(plan: &mut Vec<(String, std::time::Duration)>, header: &str) {
    match plan.first_mut() {
        Some((text, _)) if text.is_empty() => *text = header.to_owned(),
        Some((text, _)) if header.chars().count() + 1 + text.chars().count() <= 2000 => {
            *text = format!("{header}\n{text}");
        }
        _ => plan.insert(0, (header.to_owned(), std::time::Duration::ZERO)),
    }
}

/// Where a reply goes, by [`reply_channel`].
pub(super) struct Target {
    pub channel_id: i64,
    /// The trigger's channel and author, when that isn't `channel_id`.
    pub redirect: Option<(i64, i64)>,
}

impl Agent {
    /// Judged by the trigger's own channel: a conversation redirected to the
    /// default channel can continue there, where replies need no redirect.
    pub(super) async fn reply_target(&self, context: &Context) -> anyhow::Result<Option<Target>> {
        let (channel_id, author_id): (i64, i64) =
            sqlx::query_as("SELECT channel_id, author_id FROM messages WHERE id = $1")
                .bind(context.reply_to)
                .fetch_one(&self.db)
                .await?;
        Ok(
            reply_channel(&context.settings, channel_id).map(|target| Target {
                channel_id: target,
                redirect: (target != channel_id).then_some((channel_id, author_id)),
            }),
        )
    }

    /// Delivers planned messages with bounded pauses and, if `reference`, a reply
    /// reference on the first (which also pings its author); images go with the
    /// first message. Each message is stored right away (the Gateway copy may
    /// arrive later) and tagged into the conversation.
    ///
    /// From a channel off the allowlist, the reply goes to the default channel
    /// instead, its first message pinging the author with a link back.
    ///
    /// Sends aren't retried: a retry after a timeout could post the reply twice.
    ///
    /// A reply's first message sent ends `kiki.reply.latency`, from when Discord
    /// created the message it answers.
    #[allow(clippy::too_many_arguments)]
    pub(super) async fn send(
        &self,
        context: &Context,
        run_id: i64,
        conversation_id: i64,
        plan: &[super::delivery::Message],
        images: &[Attachment],
        reference: bool,
        kind: Send,
        record: &mut RunRecord,
    ) -> anyhow::Result<()> {
        let target = self
            .reply_target(context)
            .await?
            .context("this channel isn't allowed and the server has no default channel")?;
        let redirect = target.redirect.map(|(channel_id, author_id)| {
            (
                redirect_header(context.guild_id, channel_id, context.reply_to, author_id),
                author_id,
            )
        });
        // A reply reference can't cross channels; the redirect header links instead.
        let reference = reference && redirect.is_none();
        let mentions = AllowedMentions {
            replied_user: reference,
            users: redirect.iter().map(|&(_, a)| Id::new(a as u64)).collect(),
            ..Default::default()
        };
        let mut plan: Vec<(String, std::time::Duration)> =
            plan.iter().map(|m| (m.text.clone(), m.delay)).collect();
        if let Some((header, _)) = &redirect {
            with_header(&mut plan, header);
        }
        let mut draining = self.draining.subscribe();
        for (i, (text, delay)) in plan.iter().enumerate() {
            if !*draining.borrow() && !delay.is_zero() {
                tokio::select! {
                    _ = tokio::time::sleep(*delay) => {},
                    _ = draining.wait_for(|drain| *drain) => {},
                }
            }
            let mut request = self
                .http
                .create_message(Id::new(target.channel_id as u64))
                .allowed_mentions(Some(&mentions));
            if !text.is_empty() {
                request = request.content(text);
            }
            if i == 0 && !images.is_empty() {
                request = request.attachments(images);
            }
            if i == 0 && reference {
                request = request
                    .reply(Id::new(context.reply_to as u64))
                    .fail_if_not_exists(false);
            }
            let span = info_span!(
                "discord_send",
                otel.kind = "client",
                kind = kind.as_str(),
                channel_id = target.channel_id,
                reply_to = context.reply_to,
                message_id = Empty,
            );
            let sent = async {
                let response = request.await.context("sending reply")?;
                let bytes = response.bytes().await.context("reading sent message")?;
                let message: Value =
                    serde_json::from_slice(&bytes).context("parsing sent message")?;
                let id: i64 = message["id"]
                    .as_str()
                    .and_then(|id| id.parse().ok())
                    .context("sent message has no id")?;
                anyhow::Ok((message, id))
            }
            .instrument(span.clone())
            .await;
            let (message, id) = match sent {
                Ok(sent) => sent,
                Err(e) => {
                    metrics::send_error(kind);
                    return Err(e);
                }
            };
            span.record("message_id", id);
            metrics::message_sent(kind);
            if kind == Send::Reply && i == 0 {
                let created = metrics::snowflake_ms(context.reply_to);
                metrics::reply_latency(Duration::from_millis(
                    (unix_millis() - created).max(0) as u64
                ));
            }
            record.reply_message_ids.push(id);

            let mut tx = self.db.begin().await?;
            store::upsert_messages(&mut *tx, context.guild_id, std::slice::from_ref(&message))
                .await?;
            sqlx::query(
                "INSERT INTO conversation_messages (message_id, conversation_id, via) \
                 VALUES ($1, $2, 'agent') ON CONFLICT (message_id) DO NOTHING",
            )
            .bind(id)
            .bind(conversation_id)
            .execute(&mut *tx)
            .await?;
            // Attribute each message now: people can answer it during a pause.
            sqlx::query("UPDATE agent_runs SET reply_message_ids = $2 WHERE id = $1")
                .bind(run_id)
                .bind(&record.reply_message_ids)
                .execute(&mut *tx)
                .await?;
            tx.commit().await?;
        }
        sqlx::query(
            "UPDATE conversations SET last_active_at = now(), last_reply_at = now() WHERE id = $1",
        )
        .bind(conversation_id)
        .execute(&self.db)
        .await?;
        Ok(())
    }

    /// Reacts to the newest trigger message, like a person would before typing.
    /// Adding a reaction is idempotent, so unlike sends a failure is only logged:
    /// Discord rejects emoji it doesn't know or that Kiki can't use.
    pub(super) async fn react(
        &self,
        context: &Context,
        reactions: &[Reaction],
        record: &mut RunRecord,
    ) {
        let (channel_id, message_id) = (context.channel_id, context.reply_to);
        for reaction in reactions {
            let emoji = reaction.request();
            let request = self.http.create_reaction(
                Id::new(channel_id as u64),
                Id::new(message_id as u64),
                &emoji,
            );
            let result = request.await;
            metrics::reaction(result.is_ok());
            match result {
                Ok(_) => {
                    record.reactions.push(reaction.to_string());
                    record.reacted_to = Some(message_id);
                }
                Err(e) => {
                    warn!(channel_id, message_id, %reaction, error = %e, "couldn't add a reaction")
                }
            }
        }
    }

    /// The images the host generated for this job, as Discord attachments.
    pub(super) async fn load_images(
        &self,
        job_id: &str,
        refs: &[ImageRef],
    ) -> anyhow::Result<Vec<Attachment>> {
        if refs.is_empty() {
            return Ok(Vec::new());
        }
        let ids: Vec<i64> = refs.iter().map(|r| r.id).collect();
        let rows: Vec<(i64, String, Vec<u8>)> = sqlx::query_as(
            "SELECT id, mime_type, data FROM agent_host_images \
             WHERE job_id = $1 AND id = ANY($2) ORDER BY id",
        )
        .bind(job_id)
        .bind(&ids)
        .fetch_all(&self.db)
        .await?;
        ensure!(
            rows.len() == refs.len(),
            "agent host returned images that weren't saved for this job"
        );
        Ok(rows
            .into_iter()
            .enumerate()
            .map(|(i, (id, mime_type, data))| {
                let filename = format!("kiki-{id}.{}", extension(&mime_type));
                Attachment::from_bytes(filename, data, i as u64)
            })
            .collect())
    }

    pub(super) fn clear_pending(&self, channel_id: i64, message_ids: &[i64]) {
        if self.pending_emoji.is_empty() {
            return;
        }
        let http = self.http.clone();
        let emoji = self.pending_emoji.clone();
        let message_ids = message_ids.to_vec();
        tokio::spawn(async move {
            let reaction = RequestReactionType::Unicode { name: &emoji };
            for message_id in message_ids {
                let request = http.delete_current_user_reaction(
                    Id::new(channel_id as u64),
                    Id::new(message_id as u64),
                    &reaction,
                );
                if let Err(e) = request.await {
                    debug!(channel_id, message_id, error = %e, "couldn't remove the pending reaction");
                }
            }
        });
    }
}

fn extension(mime_type: &str) -> &'static str {
    match mime_type {
        "image/jpeg" => "jpg",
        "image/webp" => "webp",
        "image/gif" => "gif",
        _ => "png",
    }
}

pub(super) fn split_message(text: &str, max: usize) -> Vec<String> {
    let mut chunks = Vec::new();
    let mut rest = text.trim();
    while rest.chars().count() > max {
        let limit = rest.char_indices().nth(max).map_or(rest.len(), |(i, _)| i);
        let window = &rest[..limit];
        let cut = window
            .rfind('\n')
            .or_else(|| window.rfind(' '))
            .filter(|&i| i > 0)
            .unwrap_or(limit);
        chunks.push(rest[..cut].trim_end().to_owned());
        rest = rest[cut..].trim_start();
    }
    if !rest.is_empty() {
        chunks.push(rest.to_owned());
    }
    chunks
}

#[cfg(test)]
mod tests {
    use super::{extension, redirect_header, split_message, with_header};

    #[test]
    fn redirect_header_links_back_on_the_first_message() {
        let header = redirect_header(1, 2, 3, 4);
        assert_eq!(header, "-# ↪ <@4> in https://discord.com/channels/1/2/3");
        let zero = std::time::Duration::ZERO;
        let mut plan = vec![("hey".to_owned(), zero), ("there".to_owned(), zero)];
        with_header(&mut plan, &header);
        assert_eq!(plan[0].0, format!("{header}\nhey"));
        let mut images_only = vec![(String::new(), zero)];
        with_header(&mut images_only, &header);
        assert_eq!(images_only[0].0, header);
        let mut long = vec![("é".repeat(1990), zero)];
        with_header(&mut long, &header);
        assert_eq!((long.len(), long[0].0.as_str()), (2, header.as_str()));
    }

    #[tokio::test]
    async fn loads_only_this_jobs_images() {
        use crate::agent::{context::tests, host::ImageRef};
        let Some(test) = crate::test_db::migrated("ingester_images_test").await else {
            return;
        };
        let db = test.db.clone();
        let ids: Vec<i64> = sqlx::query_scalar(
            "WITH jobs AS (INSERT INTO agent_host_jobs (id, request, status) \
                 VALUES ('reply:1', '{}', 'running'), ('reply:2', '{}', 'running')) \
             INSERT INTO agent_host_images (job_id, mime_type, data) \
             VALUES ('reply:1', 'image/jpeg', '\\x01'), ('reply:2', 'image/png', '\\x02') \
             RETURNING id",
        )
        .fetch_all(&db)
        .await
        .unwrap();
        let agent = tests::agent(db.clone());
        let mine = agent
            .load_images("reply:1", &[ImageRef { id: ids[0] }])
            .await
            .unwrap();
        assert_eq!(mine.len(), 1);
        assert_eq!(mine[0].filename, format!("kiki-{}.jpg", ids[0]));
        assert_eq!(mine[0].file, [1]);
        assert!(agent.load_images("reply:1", &[]).await.unwrap().is_empty());
        let theirs = agent
            .load_images("reply:1", &[ImageRef { id: ids[1] }])
            .await;
        assert!(theirs.is_err());
        test.drop().await;
    }

    #[test]
    fn image_filenames_follow_the_mime_type() {
        assert_eq!(extension("image/jpeg"), "jpg");
        assert_eq!(extension("image/webp"), "webp");
        assert_eq!(extension("image/png"), "png");
        assert_eq!(extension("image/unknown"), "png");
    }

    #[test]
    fn short_text_is_one_chunk() {
        assert_eq!(split_message("  hi there ", 10), ["hi there"]);
    }

    #[test]
    fn splits_on_newlines_then_spaces() {
        assert_eq!(
            split_message("aaa bbb\nccc ddd", 10),
            ["aaa bbb", "ccc ddd"]
        );
        assert_eq!(split_message("aaa bbb ccc", 8), ["aaa bbb", "ccc"]);
    }

    #[test]
    fn hard_splits_long_words_on_char_boundaries() {
        assert_eq!(split_message("ééééé", 2), ["éé", "éé", "é"]);
    }
}
