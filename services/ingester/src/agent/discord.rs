//! Sending Kiki's reply to Discord, and tagging it into the conversation.

use anyhow::Context as _;
use serde_json::Value;
use tracing::debug;
use twilight_http::request::channel::reaction::RequestReactionType;
use twilight_model::{channel::message::AllowedMentions, id::Id};

use super::{Agent, RunRecord, context::Context};
use crate::store;

/// Discord allows 2000 characters per message; leave room for safety.
const MAX_MESSAGE_CHARS: usize = 1900;

impl Agent {
    /// Sends `text` as a reply to the context's trigger message, split across
    /// messages if it's too long. Each sent message is stored right away (the
    /// Gateway copy may arrive later) and tagged into the conversation.
    ///
    /// Sends aren't retried: a retry after a timeout could post the reply twice.
    pub(super) async fn send(
        &self,
        context: &Context,
        conversation_id: i64,
        text: &str,
        record: &mut RunRecord,
    ) -> anyhow::Result<()> {
        let mentions = AllowedMentions {
            replied_user: true,
            ..Default::default()
        };
        for (i, chunk) in split_message(text, MAX_MESSAGE_CHARS).iter().enumerate() {
            let mut request = self
                .http
                .create_message(Id::new(context.channel_id as u64))
                .content(chunk)
                .allowed_mentions(Some(&mentions));
            if i == 0 {
                request = request
                    .reply(Id::new(context.reply_to as u64))
                    .fail_if_not_exists(false);
            }
            let response = request.await.context("sending reply")?;
            let bytes = response.bytes().await.context("reading sent message")?;
            let message: Value = serde_json::from_slice(&bytes).context("parsing sent message")?;
            let id: i64 = message["id"]
                .as_str()
                .and_then(|id| id.parse().ok())
                .context("sent message has no id")?;
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

fn split_message(text: &str, max: usize) -> Vec<String> {
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
    use super::split_message;

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
