//! Classification of one pool: checks 1–3, Decide (Jev via OpenRouter), then tag.
//!
//! 1. Reply to a Kiki message → continue that message's conversation. Checked
//!    before mentions: Discord replies ping the author, so they also mention Kiki.
//!    @Kiki (user or managed role) → always a new conversation.
//! 2. Keyword (e.g. "kiki") → Decide.
//! 3. Conversation in the channel active within `ROUTE_ACTIVE_WINDOW_SECS` → Decide.
//!
//! Every classified message gets a `classifications` row (including "no");
//! messages that join a conversation also get a `conversation_messages` row, and
//! the pool is queued in `agent_runs`.

use std::time::{Instant, SystemTime, UNIX_EPOCH};

use serde_json::{Map, Value, json};
use sqlx::FromRow;
use tracing::{debug, info, warn};

use super::{Pool, PoolKey, Router, unix_millis};
use crate::conversation;

const INSTRUCTIONS: &str = "Kiki is an AI chat assistant in this Discord channel. Based on new_messages, \
is their author talking to Kiki: continuing one of the listed conversations with her, starting a new \
one, or not talking to her at all? Use recent_channel_messages to resolve short follow-ups: an answer \
or correction to Kiki's recent reply continues that conversation even without mentioning her. \
Use that reply's conversation_id to distinguish conversations with the same people. \
Talking about Kiki or unrelated chatter is not talking to her; an active conversation alone is not \
enough. Kiki's own messages in recent_channel_messages have from = \"Kiki\".";
const RECENT_MESSAGES: i64 = 15;
// Jev allows 10 choices: eight conversations plus "new" and "no".
const CONVERSATION_CHOICES: usize = 8;
const CONVERSATION_PREVIEW: i64 = 3;
/// A conversation idle this long is usually over: Jev must be this confident to
/// continue it, or the message starts a new one. Replayed decisions put a
/// 75-minute-old match at 0.79 and real follow-ups within minutes at 0.82-0.95.
const STALE_AFTER_SECS: i64 = 30 * 60;
const STALE_MIN_CONFIDENCE: f64 = 0.9;

#[derive(FromRow)]
struct PooledMessage {
    id: i64,
    content: String,
    created_at_ms: i64,
    reply_to_id: Option<i64>,
    reply_author_id: Option<i64>,
    mention_user_ids: Vec<i64>,
    mention_role_ids: Vec<i64>,
}

#[derive(Clone, Copy)]
enum Check {
    Reply,
    Mention,
    Keyword,
    ActiveConversation,
    None,
}

enum Outcome {
    Continue {
        conversation_id: i64,
        via: &'static str,
    },
    New {
        via: &'static str,
    },
    No,
}

#[derive(Default)]
struct JevResult {
    model: Option<String>,
    choice: Option<String>,
    confidence: Option<f64>,
    cost_usd: Option<f64>,
    latency_ms: Option<i32>,
    error: Option<String>,
}

fn idle_secs(active: &[Value], conversation_id: i64) -> Option<i64> {
    active
        .iter()
        .find(|c| c["id"].as_i64() == Some(conversation_id))
        .and_then(|c| c["last_active_seconds_ago"].as_i64())
}

fn is_stale(active: &[Value], conversation_id: i64) -> bool {
    idle_secs(active, conversation_id).is_some_and(|idle| idle >= STALE_AFTER_SECS)
}

/// Whether any listed conversation was active within `window_secs`. Older ones
/// still go to Jev when a keyword matched, but don't send every message there:
/// past `STALE_AFTER_SECS` Jev rarely clears the bar to continue them anyway.
fn any_recent(active: &[Value], window_secs: i64) -> bool {
    active
        .iter()
        .filter_map(|c| c["last_active_seconds_ago"].as_i64())
        .any(|idle| idle < window_secs)
}

impl Router {
    pub(super) async fn classify(
        &self,
        key: PoolKey,
        pool: &Pool,
        classified_at: SystemTime,
    ) -> anyhow::Result<()> {
        // Re-read the pooled messages: they may have been edited or deleted while pooled.
        let messages = self.load_messages(&pool.message_ids).await?;
        let Some(newest) = messages.last() else {
            return Ok(()); // all deleted before classification
        };

        let classified_ms = classified_at
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_millis() as i64;
        let age_ms = classified_ms - newest.created_at_ms;
        if age_ms > self.config.max_message_age.as_millis() as i64 {
            debug!(
                channel_id = key.channel_id,
                user_id = key.user_id,
                message_ids = ?pool.message_ids,
                age_ms, "pool too old to classify; skipping"
            );
            return Ok(());
        }

        let mut jev = JevResult::default();
        let (check, outcome) = if let Some(outcome) = self.reply_to_kiki(&messages).await? {
            (Check::Reply, outcome)
        } else if messages.iter().any(|m| self.mentions_kiki(m)) {
            (Check::Mention, Outcome::New { via: "mention" })
        } else {
            let active = self.active_conversations(key.channel_id).await?;
            let check = if messages.iter().any(|m| self.has_keyword(&m.content)) {
                Check::Keyword
            } else if any_recent(&active, self.config.active_window.as_secs() as i64) {
                Check::ActiveConversation
            } else {
                Check::None
            };
            let outcome = match check {
                Check::None => Outcome::No,
                _ => self.decide(key, pool, &messages, &active, &mut jev).await,
            };
            (check, outcome)
        };

        self.tag(key, pool, &messages, check, outcome, &jev, classified_at)
            .await
    }

    /// Reacts as soon as an explicit trigger (check 1: @Kiki or a reply to Kiki)
    /// is stored, instead of when its pool closes. Check 1 needs no Jev call, so
    /// the outcome is already certain; the pool keeps collecting for the agent.
    pub(super) async fn react_if_explicit(&self, channel_id: i64, message_id: i64) {
        match self.load_messages(&[message_id]).await {
            Ok(messages) => {
                if messages.first().is_some_and(|m| self.is_explicit(m)) {
                    self.react_pending(channel_id, message_id);
                }
            }
            Err(e) => warn!(message_id, error = %e, "couldn't check for an explicit trigger"),
        }
    }

    async fn load_messages(&self, ids: &[i64]) -> sqlx::Result<Vec<PooledMessage>> {
        sqlx::query_as(
            r#"
            SELECT m.id, m.content, m.reply_to_id,
                   (extract(epoch FROM m.created_at) * 1000)::bigint AS created_at_ms,
                   coalesce(r.author_id, (m.raw->'referenced_message'->'author'->>'id')::bigint)
                       AS reply_author_id,
                   ARRAY(SELECT (u->>'id')::bigint
                         FROM jsonb_array_elements(coalesce(m.raw->'mentions', '[]')) u) AS mention_user_ids,
                   ARRAY(SELECT (r #>> '{}')::bigint
                         FROM jsonb_array_elements(coalesce(m.raw->'mention_roles', '[]')) r) AS mention_role_ids
            FROM messages m
            LEFT JOIN messages r ON r.id = m.reply_to_id
            WHERE m.id = ANY($1) AND m.deleted_at IS NULL
            ORDER BY m.id
            "#,
        )
        .bind(ids)
        .fetch_all(&self.db)
        .await
    }

    fn is_explicit(&self, m: &PooledMessage) -> bool {
        m.reply_author_id == Some(self.bot_user_id) || self.mentions_kiki(m)
    }

    async fn reply_to_kiki(&self, messages: &[PooledMessage]) -> anyhow::Result<Option<Outcome>> {
        let replied: Vec<i64> = messages
            .iter()
            .filter(|m| m.reply_author_id == Some(self.bot_user_id))
            .filter_map(|m| m.reply_to_id)
            .collect();
        if replied.is_empty() {
            return Ok(None);
        }
        let conversation_id: Option<i64> = sqlx::query_scalar(
            "SELECT conversation_id FROM conversation_messages WHERE message_id = ANY($1) \
             ORDER BY message_id DESC LIMIT 1",
        )
        .bind(&replied)
        .fetch_optional(&self.db)
        .await?;
        Ok(Some(match conversation_id {
            Some(conversation_id) => Outcome::Continue {
                conversation_id,
                via: "reply",
            },
            None => Outcome::New { via: "reply" },
        }))
    }

    fn mentions_kiki(&self, m: &PooledMessage) -> bool {
        m.mention_user_ids.contains(&self.bot_user_id)
            || m.mention_role_ids
                .iter()
                .any(|r| self.bot_role_ids.contains(r))
    }

    fn has_keyword(&self, content: &str) -> bool {
        content
            .to_lowercase()
            .split(|c: char| !c.is_alphanumeric())
            .any(|word| self.config.keywords.iter().any(|k| k == word))
    }

    async fn active_conversations(&self, channel_id: i64) -> anyhow::Result<Vec<Value>> {
        let active = conversation::active(&self.db, self.bot_user_id).await?;

        let mut conversations = Vec::new();
        for c in active
            .iter()
            .filter(|c| c.channel_id == channel_id)
            .take(CONVERSATION_CHOICES)
        {
            let latest: Vec<(i64, Option<String>, String)> = sqlx::query_as(
                r#"
                SELECT m.author_id, m.author_name, m.content
                FROM conversation_messages cm JOIN messages m ON m.id = cm.message_id
                WHERE cm.conversation_id = $1 AND m.deleted_at IS NULL
                ORDER BY m.id DESC LIMIT $2
                "#,
            )
            .bind(c.id)
            .bind(CONVERSATION_PREVIEW)
            .fetch_all(&self.db)
            .await?;
            let latest: Vec<Value> = latest
                .into_iter()
                .rev()
                .map(|(author, name, content)| json!({ "from": self.display_name(author, name), "text": content }))
                .collect();
            let participants: Vec<String> = c
                .participants
                .iter()
                .map(|p| p.name.clone().unwrap_or_else(|| p.id.to_string()))
                .collect();
            let mut conversation = json!({
                "id": c.id,
                "participants": participants,
                "last_active_seconds_ago": (unix_millis() - c.last_active_at_ms) / 1000,
                "latest_messages": latest,
            });
            if let Some(topic) = &c.topic {
                conversation["current_topic"] = json!(topic);
            }
            conversations.push(conversation);
        }
        Ok(conversations)
    }

    /// Asks Jev whether the pool is talking to Kiki. Errors, timeouts and low
    /// confidence all count as "no".
    async fn decide(
        &self,
        key: PoolKey,
        pool: &Pool,
        messages: &[PooledMessage],
        active: &[Value],
        jev_result: &mut JevResult,
    ) -> Outcome {
        let Some(jev) = &self.jev else {
            jev_result.error = Some("OPENROUTER_DECISION_API_KEY not set".into());
            return Outcome::No;
        };
        let state = match self.decide_state(key, pool, messages, active).await {
            Ok(state) => state,
            Err(e) => {
                warn!(
                    channel_id = key.channel_id,
                    pool_id = pool.message_ids.last(),
                    error = %e,
                    "couldn't build Jev state; treating as no"
                );
                jev_result.error = Some(format!("building state: {e:#}"));
                return Outcome::No;
            }
        };

        let mut criteria = Map::new();
        for c in active {
            let participants = c["participants"].as_array().map(|p| {
                p.iter()
                    .filter_map(Value::as_str)
                    .collect::<Vec<_>>()
                    .join(", ")
            });
            criteria.insert(
                format!("conversation_{}", c["id"]),
                json!(format!(
                    "Replying to Kiki in the conversation that {} are in (see conversations[id={}]), including a short answer or correction to her recent reply without naming her",
                    participants.unwrap_or_default(),
                    c["id"]
                )),
            );
        }
        criteria.insert(
            "new".into(),
            json!("Starting a new conversation with Kiki, not one of the listed ones"),
        );
        criteria.insert(
            "no".into(),
            json!("Not talking to Kiki: talking to other people, or about Kiki rather than to her"),
        );

        let started = Instant::now();
        let answer = jev.choose(state, INSTRUCTIONS, criteria).await;
        jev_result.latency_ms = Some(started.elapsed().as_millis() as i32);
        let answer = match answer {
            Ok(answer) => answer,
            Err(e) => {
                warn!(
                    channel_id = key.channel_id,
                    pool_id = pool.message_ids.last(),
                    latency_ms = jev_result.latency_ms,
                    error = format!("{e:#}"),
                    "Jev failed; treating as no"
                );
                jev_result.error = Some(format!("{e:#}"));
                return Outcome::No;
            }
        };
        jev_result.model = answer.model.clone();
        jev_result.cost_usd = answer.cost;
        jev_result.choice = Some(answer.choice.clone());
        jev_result.confidence = Some(answer.confidence);
        if answer.confidence < self.config.jev_min_confidence {
            debug!(
                channel_id = key.channel_id,
                pool_id = pool.message_ids.last(),
                confidence = answer.confidence,
                min_confidence = self.config.jev_min_confidence,
                "Jev confidence too low; treating as no"
            );
            return Outcome::No;
        }
        match answer.choice.as_str() {
            "new" => Outcome::New { via: "decide" },
            "no" => Outcome::No,
            // Only the offered conversations: anything else could be in another channel.
            other => match other
                .strip_prefix("conversation_")
                .and_then(|id| id.parse().ok())
                .filter(|&id| idle_secs(active, id).is_some())
            {
                Some(conversation_id)
                    if is_stale(active, conversation_id)
                        && answer.confidence < STALE_MIN_CONFIDENCE =>
                {
                    debug!(
                        conversation_id,
                        pool_id = pool.message_ids.last(),
                        confidence = answer.confidence,
                        min_confidence = STALE_MIN_CONFIDENCE,
                        "Jev chose a stale conversation; starting a new one"
                    );
                    Outcome::New { via: "decide" }
                }
                Some(conversation_id) => Outcome::Continue {
                    conversation_id,
                    via: "decide",
                },
                None => {
                    warn!(
                        channel_id = key.channel_id,
                        pool_id = pool.message_ids.last(),
                        "Jev returned an unoffered choice; treating as no"
                    );
                    jev_result.error = Some(format!("unknown choice `{other}`"));
                    Outcome::No
                }
            },
        }
    }

    async fn decide_state(
        &self,
        key: PoolKey,
        pool: &Pool,
        messages: &[PooledMessage],
        active: &[Value],
    ) -> anyhow::Result<Value> {
        let first = pool.message_ids.iter().min().copied().unwrap_or_default();
        let recent: Vec<(i64, Option<String>, String, Option<i64>)> = sqlx::query_as(
            r#"
            SELECT m.author_id, m.author_name, m.content, cm.conversation_id
            FROM messages m
            LEFT JOIN conversation_messages cm ON cm.message_id = m.id
            WHERE m.channel_id = $1 AND m.id < $2 AND m.deleted_at IS NULL
              AND NOT EXISTS (SELECT 1 FROM ignored_authors i WHERE i.user_id = m.author_id)
            ORDER BY m.id DESC LIMIT $3
            "#,
        )
        .bind(key.channel_id)
        .bind(first)
        .bind(RECENT_MESSAGES)
        .fetch_all(&self.db)
        .await?;
        let author: Option<String> =
            sqlx::query_scalar("SELECT author_name FROM messages WHERE id = $1")
                .bind(first)
                .fetch_optional(&self.db)
                .await?
                .flatten();

        let recent: Vec<Value> = recent
            .into_iter()
            .rev()
            .map(|(id, name, content, conversation_id)| {
                json!({
                    "from": self.display_name(id, name),
                    "text": content,
                    "conversation_id": conversation_id,
                })
            })
            .collect();
        Ok(json!({
            "recent_channel_messages": recent,
            "conversations": active,
            "new_messages": {
                "from": self.display_name(key.user_id, author),
                "texts": messages.iter().map(|m| m.content.as_str()).collect::<Vec<_>>(),
            },
        }))
    }

    fn display_name(&self, author_id: i64, name: Option<String>) -> String {
        if author_id == self.bot_user_id {
            "Kiki".into()
        } else {
            name.unwrap_or_else(|| author_id.to_string())
        }
    }

    /// Records the classification of every pooled message and, when routed, tags
    /// them into their conversation and queues the agent. One transaction.
    #[allow(clippy::too_many_arguments)]
    async fn tag(
        &self,
        key: PoolKey,
        pool: &Pool,
        messages: &[PooledMessage],
        check: Check,
        outcome: Outcome,
        jev: &JevResult,
        classified_at: SystemTime,
    ) -> anyhow::Result<()> {
        let ids: Vec<i64> = messages.iter().map(|m| m.id).collect();
        let pool_id = *ids.last().expect("pool is not empty");
        let mut tx = self.db.begin().await?;
        if !matches!(outcome, Outcome::No) {
            conversation::lock_pool(&mut tx).await?;
        }

        let (conversation_id, via, outcome_name) = match outcome {
            Outcome::No => (None, None, "no"),
            Outcome::Continue {
                conversation_id,
                via,
            } => {
                sqlx::query(
                    "UPDATE conversations SET last_active_at = now(), closed_at = NULL, closed_reason = NULL \
                     WHERE id = $1",
                )
                    .bind(conversation_id)
                    .execute(&mut *tx)
                    .await?;
                (Some(conversation_id), Some(via), "continue")
            }
            Outcome::New { via } => {
                let id: i64 = sqlx::query_scalar(
                    "INSERT INTO conversations (guild_id, channel_id, started_by) VALUES ($1, $2, $3) RETURNING id",
                )
                .bind(pool.guild_id)
                .bind(key.channel_id)
                .bind(key.user_id)
                .fetch_one(&mut *tx)
                .await?;
                (Some(id), Some(via), "new")
            }
        };

        let mut evicted = Vec::new();
        let mut run_id: Option<i64> = None;
        if let (Some(conversation_id), Some(via)) = (conversation_id, via) {
            evicted =
                conversation::evict_beyond(&mut tx, self.config.max_active_conversations).await?;
            sqlx::query(
                r#"
                INSERT INTO conversation_messages (message_id, conversation_id, via)
                SELECT unnest($1::bigint[]), $2, $3
                ON CONFLICT (message_id) DO NOTHING
                "#,
            )
            .bind(&ids)
            .bind(conversation_id)
            .bind(via)
            .execute(&mut *tx)
            .await?;
            run_id = Some(sqlx::query_scalar(
                "INSERT INTO agent_runs (conversation_id, trigger_message_ids) VALUES ($1, $2) RETURNING id",
            )
            .bind(conversation_id)
            .bind(&ids)
            .fetch_one(&mut *tx)
            .await?);
        }

        let classified_secs = classified_at
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs_f64();
        sqlx::query(
            r#"
            INSERT INTO classifications (message_id, pool_id, classified_at, check_hit, jev_model, jev_choice,
                                         jev_confidence, jev_cost_usd, jev_latency_ms, jev_error, outcome,
                                         conversation_id)
            SELECT unnest($1::bigint[]), $2, to_timestamp($3), $4, $5, $6, $7, $8, $9, $10, $11, $12
            ON CONFLICT (message_id) DO NOTHING
            "#,
        )
        .bind(&ids)
        .bind(pool_id)
        .bind(classified_secs)
        .bind(check.as_str())
        .bind(&jev.model)
        .bind(&jev.choice)
        .bind(jev.confidence.map(|c| c as f32))
        .bind(jev.cost_usd)
        .bind(jev.latency_ms)
        .bind(&jev.error)
        .bind(outcome_name)
        .bind(conversation_id)
        .execute(&mut *tx)
        .await?;
        tx.commit().await?;
        // Shown until the agent replies (it removes the reaction then). Explicit
        // triggers (check 1) were already reacted to when they were stored.
        if conversation_id.is_some() && !matches!(check, Check::Reply | Check::Mention) {
            self.react_pending(key.channel_id, pool_id);
        }

        if matches!(check, Check::None) {
            debug!(
                pool_id,
                channel_id = key.channel_id,
                messages = ids.len(),
                "no routing trigger"
            );
            return Ok(());
        }
        info!(
            pool_id,
            run_id,
            channel_id = key.channel_id,
            user_id = key.user_id,
            messages = ids.len(),
            check = check.as_str(),
            outcome = outcome_name,
            conversation_id,
            jev_model = jev.model,
            jev_choice = jev.choice,
            jev_confidence = jev.confidence,
            jev_cost_usd = jev.cost_usd,
            jev_latency_ms = jev.latency_ms,
            ?evicted,
            "classified"
        );
        Ok(())
    }
}

impl Check {
    fn as_str(self) -> &'static str {
        match self {
            Check::Reply => "reply",
            Check::Mention => "mention",
            Check::Keyword => "keyword",
            Check::ActiveConversation => "active_conversation",
            Check::None => "none",
        }
    }
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::{any_recent, is_stale};

    #[test]
    fn staleness_uses_the_chosen_conversation() {
        let active = [
            json!({ "id": 5, "last_active_seconds_ago": 4512 }),
            json!({ "id": 6, "last_active_seconds_ago": 30 }),
        ];
        assert!(is_stale(&active, 5));
        assert!(!is_stale(&active, 6));
        assert!(!is_stale(&active, 7));
        assert!(any_recent(&active, 60));
        assert!(!any_recent(&active[..1], 3600));
        assert!(!any_recent(&[], 3600));
    }
}
