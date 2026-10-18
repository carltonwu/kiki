//! A conversation's current topic: what it's about now, not its whole history.
//!
//! Refreshed in the background after a reply, once `TOPIC_EVERY_MESSAGES`
//! messages arrived since the last summary (or right away if it has none). The
//! topic model sees the previous topic and the latest messages, so it can tell
//! when the conversation moved on.

use std::sync::Arc;

use tracing::{debug, warn};

use super::{
    Agent,
    host::{JobRequest, Scope},
};
use crate::{chat::Message, conversation};

const MAX_TOPIC_TOKENS: u32 = 400;
const MAX_TOPIC_CHARS: usize = 200;

const INSTRUCTIONS: &str = "You track the current topic of a Discord group chat that includes Kiki, an AI \
assistant. Given the previous topic (if any) and the latest messages, reply with the topic being discussed \
now: one short line, under 15 words, no quotes, no trailing period. If the chat moved on from the previous \
topic, describe the new one, not the history. Messages start with the sender in brackets, like \
\"[u1 Alex]\"; name people by name, without the ref.";

impl Agent {
    pub(super) fn spawn_topic_refresh(self: &Arc<Self>, conversation_id: i64) {
        let agent = self.clone();
        tokio::spawn(async move {
            if let Err(e) = agent.refresh_topic(conversation_id).await {
                warn!(
                    conversation_id,
                    error = format!("{e:#}"),
                    "couldn't refresh the topic"
                );
            }
        });
    }

    async fn refresh_topic(&self, conversation_id: i64) -> anyhow::Result<()> {
        let Some(c) = conversation::get(&self.db, self.bot_user_id, conversation_id).await? else {
            return Ok(());
        };
        let settings = self.settings.snapshot(c.guild_id);
        let new_messages: i64 = sqlx::query_scalar(
            "SELECT count(*) FROM conversation_messages WHERE conversation_id = $1 AND message_id > $2",
        )
        .bind(conversation_id)
        .bind(c.topic_through_message_id.unwrap_or(0))
        .fetch_one(&self.db)
        .await?;
        if c.topic.is_some() && new_messages < settings.topic_every_messages {
            return Ok(());
        }

        let messages = self
            .conversation_messages(conversation_id, settings.topic_sample_messages)
            .await?;
        let Some(through) = messages.last().map(|m| m.id) else {
            return Ok(());
        };
        let shown: Vec<_> = messages.iter().collect();
        let roster = self
            .roster(c.guild_id, conversation_id, &shown, &[])
            .await?;
        let transcript: Vec<String> = messages.iter().map(|m| roster.line(m)).collect();
        let prompt = format!(
            "Previous topic: {}\n\nLatest messages:\n{}",
            c.topic.as_deref().unwrap_or("(none yet)"),
            transcript.join("\n"),
        );
        let completion = self
            .host
            .execute(&JobRequest {
                id: topic_job_id(conversation_id, through, settings.topic_sample_messages),
                kind: "topic".into(),
                scope: Scope::new(c.guild_id, c.channel_id, conversation_id),
                model: self.config.topic_model.clone(),
                messages: vec![Message::system(INSTRUCTIONS), Message::user(prompt)],
                max_output_tokens: MAX_TOPIC_TOKENS,
                people: roster.wire(),
                image_model: None,
            })
            .await?;
        sqlx::query("UPDATE conversations SET topic_cost_usd = topic_cost_usd + $2 WHERE id = $1")
            .bind(conversation_id)
            .bind(completion.usage.cost.unwrap_or(0.0))
            .execute(&self.db)
            .await?;
        let Some(topic) = clean_topic(&completion.text) else {
            anyhow::bail!("agent host returned an empty topic");
        };

        // A slower, older summary must not overwrite a newer one.
        let updated = sqlx::query(
            r#"
            UPDATE conversations
            SET topic = $2, topic_through_message_id = $3, topic_updated_at = now()
            WHERE id = $1 AND (topic_through_message_id IS NULL OR topic_through_message_id < $3)
            "#,
        )
        .bind(conversation_id)
        .bind(&topic)
        .bind(through)
        .execute(&self.db)
        .await?
        .rows_affected();
        debug!(
            conversation_id,
            through_message_id = through,
            updated = updated > 0,
            cost_usd = completion.usage.cost,
            "topic refreshed"
        );
        Ok(())
    }
}

fn topic_job_id(conversation_id: i64, through: i64, sample: i64) -> String {
    format!("topic:{conversation_id}:{through}:{sample}")
}

fn clean_topic(text: &str) -> Option<String> {
    let line = text.lines().map(str::trim).find(|l| !l.is_empty())?;
    let line = line
        .strip_prefix("Topic:")
        .or_else(|| line.strip_prefix("Current topic:"))
        .unwrap_or(line)
        .trim()
        .trim_matches(|c| matches!(c, '"' | '\'' | '*' | '`'))
        .trim_end_matches('.')
        .trim();
    if line.is_empty() {
        return None;
    }
    Some(line.chars().take(MAX_TOPIC_CHARS).collect())
}

#[cfg(test)]
mod tests {
    use super::{clean_topic, topic_job_id};

    #[test]
    fn changed_sample_uses_a_distinct_durable_job() {
        assert_eq!(topic_job_id(1, 100, 12), "topic:1:100:12");
        assert_ne!(topic_job_id(1, 100, 12), topic_job_id(1, 100, 20));
    }

    #[test]
    fn cleans_model_output() {
        assert_eq!(
            clean_topic("\n  \"Rust async runtimes.\"\nmore").as_deref(),
            Some("Rust async runtimes")
        );
        assert_eq!(
            clean_topic("Topic: weekend plans").as_deref(),
            Some("weekend plans")
        );
        assert_eq!(clean_topic("  \n "), None);
    }
}
