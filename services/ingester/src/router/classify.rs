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
use tracing::{Span, debug, field::Empty, info, warn};

use super::{Pool, PoolKey, Router, unix_millis};
use crate::{agent::NOT_A_FAILURE_NOTE, conversation, metrics, telemetry};

const INSTRUCTIONS: &str = "Kiki is an AI member of this Discord server, a close friend group where \
banter, teasing and roasting are normal. Based on new_messages, is their author talking to Kiki: \
continuing one of the listed conversations with her, starting a new one, or not talking to her at all? \
Kiki's own messages in recent_channel_messages have from = \"Kiki\". Right after Kiki replies to \
someone, their next message is usually for her even without her name: an answer, a correction, a \
follow-up question, a new request, a complaint that she didn't answer, or a reaction aimed at her \
(\"you're an idiot\", \"silly kiki\", \"so close\"). A question with \"you\" soon after her reply is \
for her. Pick the conversation of the reply being answered (its conversation_id; \
kiki_replied_seconds_ago says how fresh it is). new_messages.author_in_conversations lists the \
author's conversations with her, and kiki_last_answered_author_seconds_ago says how recently she \
answered them; replying_to in recent_channel_messages shows who each message answered. Her name followed by a question or request is \
addressed to her, even when it reads like a statement: \"kiki is chris single\", \"kiki who won\", \
\"kiki roast him\". Laughter or emoji alone (\"lmao\", \"💀\") is usually not for her. Talking \
about Kiki to other people (\"kiki is so annoying lol\", \"kiki doesn't need a reply\"), or chatter \
between other people, is not talking to her; an active conversation alone is not enough.";
// Jev allows 10 choices: eight conversations plus "new" and "no".
const CONVERSATION_CHOICES: usize = 8;
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

/// Second person or a question: "do I have to reply to you", "u up?".
fn addresses_you(text: &str) -> bool {
    text.contains('?')
        || text
            .to_lowercase()
            .split(|c: char| !c.is_alphanumeric())
            .any(|w| {
                matches!(
                    w,
                    "you" | "u" | "ur" | "your" | "yours" | "youre" | "ya" | "yourself"
                )
            })
}

/// A keyword as the first or last word: "kiki …" or "… kiki".
fn names_kiki(text: &str, keywords: &[String]) -> bool {
    let lower = text.to_lowercase();
    let words: Vec<&str> = lower
        .split(|c: char| !c.is_alphanumeric())
        .filter(|w| !w.is_empty())
        .collect();
    [words.first(), words.last()]
        .into_iter()
        .flatten()
        .any(|w| keywords.iter().any(|k| k == w))
}

fn idle_secs(active: &[Value], conversation_id: i64) -> Option<i64> {
    active
        .iter()
        .find(|c| c["id"].as_i64() == Some(conversation_id))
        .and_then(|c| c["last_active_seconds_ago"].as_i64())
}

fn is_stale(active: &[Value], conversation_id: i64, stale_after: u64) -> bool {
    idle_secs(active, conversation_id).is_some_and(|idle| idle >= stale_after as i64)
}

/// Whether any listed conversation was active within `window_secs`. Older ones
/// still go to Jev when a keyword matched, but don't send every message there:
/// past the configured stale age Jev rarely clears the bar to continue them anyway.
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
            let active = self
                .active_conversations(key.channel_id, pool.settings.decide_conversation_preview)
                .await?;
            let check = if messages
                .iter()
                .any(|m| has_keyword(&m.content, &pool.settings.keywords))
            {
                Check::Keyword
            } else if any_recent(&active, pool.settings.active_window as i64) {
                Check::ActiveConversation
            } else {
                Check::None
            };
            let outcome = match check {
                Check::None => Outcome::No,
                _ => match self.direct_follow_up(key, pool, &messages).await {
                    Ok(Some(conversation_id)) => {
                        info!(
                            channel_id = key.channel_id,
                            pool_id = pool.message_ids.last(),
                            conversation_id,
                            "direct follow-up to Kiki's answer; continuing without Decide"
                        );
                        Outcome::Continue {
                            conversation_id,
                            via: "decide",
                        }
                    }
                    result => {
                        if let Err(e) = result {
                            warn!(error = %e, "couldn't check for a direct follow-up");
                        }
                        self.decide(key, pool, check, &messages, &active, &mut jev)
                            .await
                    }
                },
            };
            (check, outcome)
        };

        let span = Span::current();
        span.record("check", check.as_str());
        span.record(
            "outcome",
            match outcome {
                Outcome::Continue { .. } => "continue",
                Outcome::New { .. } => "new",
                Outcome::No => "no",
            },
        );
        self.tag(key, pool, &messages, check, outcome, &jev, classified_at)
            .await
    }

    /// Reacts as soon as an explicit trigger (check 1: @Kiki or a reply to Kiki)
    /// is stored, instead of when its pool closes. Check 1 needs no Jev call, so
    /// the outcome is already certain; the pool keeps collecting for the agent.
    pub(super) async fn react_if_explicit(&self, guild_id: i64, channel_id: i64, message_id: i64) {
        if !self.settings.snapshot(guild_id).status_reactions {
            return;
        }
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

    async fn active_conversations(
        &self,
        channel_id: i64,
        preview: i64,
    ) -> anyhow::Result<Vec<Value>> {
        let active = conversation::active(&self.db, self.bot_user_id).await?;

        let mut conversations = Vec::new();
        for c in active
            .iter()
            .filter(|c| c.channel_id == channel_id)
            .take(CONVERSATION_CHOICES)
        {
            let latest: Vec<(i64, Option<String>, String)> = sqlx::query_as(&format!(
                r#"
                SELECT m.author_id, m.author_name, m.content
                FROM conversation_messages cm JOIN messages m ON m.id = cm.message_id
                WHERE cm.conversation_id = $1 AND m.deleted_at IS NULL AND {NOT_A_FAILURE_NOTE}
                ORDER BY m.id DESC LIMIT $2
                "#,
            ))
            .bind(c.id)
            .bind(preview)
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
            let replied: Option<i64> = sqlx::query_scalar(
                "SELECT extract(epoch FROM now() - last_reply_at)::bigint FROM conversations WHERE id = $1",
            )
            .bind(c.id)
            .fetch_one(&self.db)
            .await?;
            let mut conversation = json!({
                "id": c.id,
                "participants": participants,
                "last_active_seconds_ago": (unix_millis() - c.last_active_at_ms) / 1000,
                "kiki_replied_seconds_ago": replied,
                "latest_messages": latest,
            });
            if let Some(topic) = &c.topic {
                conversation["current_topic"] = json!(topic);
            }
            conversations.push(conversation);
        }
        Ok(conversations)
    }

    /// Asks Jev whether the pool is talking to Kiki. Errors and timeouts count as
    /// "no". So does low confidence, except when Kiki was named (a lower bar) or
    /// the author is answering Kiki's fresh reply (see [`Self::follow_up`]).
    #[allow(clippy::too_many_arguments)]
    async fn decide(
        &self,
        key: PoolKey,
        pool: &Pool,
        check: Check,
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
        let settings = &pool.settings;
        let min_confidence = match check {
            Check::Keyword => settings.decide_keyword_min_confidence,
            _ => settings.decide_min_confidence,
        };
        let unsure = answer.confidence < min_confidence;
        // Jev is least sure about short follow-ups ("bro its 11pm", "do I have to
        // reply to you?"): from someone Kiki just answered, take those as continuing.
        let doubtful_no =
            answer.choice == "no" && answer.confidence < settings.follow_up_max_no_confidence;
        // "kiki this guy hates you", "u up kiki?": her name opening or closing a
        // message that says "you" or asks something is addressed to her.
        let vocative = matches!(check, Check::Keyword)
            && messages
                .iter()
                .any(|m| addresses_you(&m.content) && names_kiki(&m.content, &settings.keywords));
        if unsure || doubtful_no || (vocative && answer.choice == "no") {
            match self.follow_up(key, settings.follow_up_window).await {
                Ok(Some(conversation_id)) => {
                    info!(
                        channel_id = key.channel_id,
                        pool_id = pool.message_ids.last(),
                        conversation_id,
                        choice = answer.choice,
                        confidence = answer.confidence,
                        "unsure Decide on a fresh follow-up; continuing"
                    );
                    return Outcome::Continue {
                        conversation_id,
                        via: "decide",
                    };
                }
                Ok(None) if vocative => {
                    info!(
                        channel_id = key.channel_id,
                        pool_id = pool.message_ids.last(),
                        choice = answer.choice,
                        confidence = answer.confidence,
                        "addressed to Kiki by name; starting a conversation"
                    );
                    return Outcome::New { via: "decide" };
                }
                Ok(None) => {}
                Err(e) => warn!(error = %e, "couldn't check for a follow-up"),
            }
        }
        if unsure {
            debug!(
                channel_id = key.channel_id,
                pool_id = pool.message_ids.last(),
                confidence = answer.confidence,
                min_confidence,
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
                    if is_stale(active, conversation_id, pool.settings.stale_after)
                        && answer.confidence < pool.settings.stale_min_confidence =>
                {
                    debug!(
                        conversation_id,
                        pool_id = pool.message_ids.last(),
                        confidence = answer.confidence,
                        min_confidence = pool.settings.stale_min_confidence,
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

    /// The conversation a pool directly follows up, if it's unmistakably aimed at
    /// Kiki: her latest message in the channel answered this author within
    /// `follow_up_window`, nobody else has spoken since, and the pool talks to her
    /// ("you", "u", a question) without @mentioning anyone else.
    async fn direct_follow_up(
        &self,
        key: PoolKey,
        pool: &Pool,
        messages: &[PooledMessage],
    ) -> sqlx::Result<Option<i64>> {
        let others = messages.iter().any(|m| {
            m.mention_user_ids
                .iter()
                .any(|&u| u != self.bot_user_id && u != key.user_id)
        });
        if pool.settings.follow_up_window == 0
            || others
            || !messages.iter().any(|m| addresses_you(&m.content))
        {
            return Ok(None);
        }
        let first = pool.message_ids.iter().min().copied().unwrap_or_default();
        sqlx::query_scalar(
            r#"
            WITH k AS (
                SELECT k.id, cm.conversation_id FROM messages k
                JOIN conversation_messages cm ON cm.message_id = k.id
                JOIN conversations c ON c.id = cm.conversation_id AND c.closed_at IS NULL
                WHERE k.channel_id = $1 AND k.author_id = $2 AND k.id < $4
                  AND k.deleted_at IS NULL
                ORDER BY k.id DESC LIMIT 1
            ), answered AS (
                -- Internal attribution also works for unreferenced channel posts.
                SELECT r.author_id FROM agent_runs ar
                JOIN messages r ON r.id = ar.reply_to_id
                WHERE ar.conversation_id = (SELECT conversation_id FROM k)
                  AND ar.reply_message_ids @> ARRAY[(SELECT id FROM k)]
                ORDER BY ar.id DESC LIMIT 1
            )
            SELECT k.conversation_id FROM k, messages km
            WHERE km.id = k.id AND km.created_at > now() - make_interval(secs => $5)
              AND (SELECT author_id FROM answered) = $3
              AND NOT EXISTS (
                  SELECT 1 FROM messages o
                  WHERE o.channel_id = $1 AND o.id > k.id AND o.id < $4 AND o.deleted_at IS NULL
                    AND NOT o.author_bot AND o.author_id <> $3
                    AND o.author_id <> ALL (ARRAY(SELECT user_id FROM ignored_authors)))
            "#,
        )
        .bind(key.channel_id)
        .bind(self.bot_user_id)
        .bind(key.user_id)
        .bind(first)
        .bind(pool.settings.follow_up_window as f64)
        .fetch_optional(&self.db)
        .await
    }

    /// The author's open conversation in this channel that Kiki replied to within
    /// `window_secs`, if any.
    async fn follow_up(&self, key: PoolKey, window_secs: u64) -> sqlx::Result<Option<i64>> {
        sqlx::query_scalar(
            r#"
            SELECT c.id FROM conversations c
            WHERE c.channel_id = $1 AND c.closed_at IS NULL
              AND c.last_reply_at > now() - make_interval(secs => $3)
              AND EXISTS (SELECT 1 FROM conversation_messages cm
                          JOIN messages m ON m.id = cm.message_id
                          WHERE cm.conversation_id = c.id AND m.author_id = $2)
            ORDER BY c.last_reply_at DESC LIMIT 1
            "#,
        )
        .bind(key.channel_id)
        .bind(key.user_id)
        .bind(window_secs as f64)
        .fetch_optional(&self.db)
        .await
    }

    async fn decide_state(
        &self,
        key: PoolKey,
        pool: &Pool,
        messages: &[PooledMessage],
        active: &[Value],
    ) -> anyhow::Result<Value> {
        let first = pool.message_ids.iter().min().copied().unwrap_or_default();
        #[allow(clippy::type_complexity)]
        let recent: Vec<(
            i64,
            Option<String>,
            String,
            Option<i64>,
            Option<i64>,
            Option<String>,
        )> = sqlx::query_as(&format!(
            r#"
            SELECT m.author_id, m.author_name, m.content, cm.conversation_id,
                   r.author_id, r.author_name
            FROM messages m
            LEFT JOIN conversation_messages cm ON cm.message_id = m.id
            LEFT JOIN messages r ON r.id = coalesce(m.reply_to_id, (
                SELECT ar.reply_to_id FROM agent_runs ar
                WHERE ar.conversation_id = cm.conversation_id AND ar.reply_message_ids @> ARRAY[m.id]
                ORDER BY ar.id DESC LIMIT 1
            ))
            WHERE m.channel_id = $1 AND m.id < $2 AND m.deleted_at IS NULL
              AND NOT EXISTS (SELECT 1 FROM ignored_authors i WHERE i.user_id = m.author_id)
              AND {NOT_A_FAILURE_NOTE}
            ORDER BY m.id DESC LIMIT $3
            "#,
        ))
        .bind(key.channel_id)
        .bind(first)
        .bind(pool.settings.decide_recent_messages)
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
            .map(|(id, name, content, conversation_id, to_id, to_name)| {
                let mut m = json!({
                    "from": self.display_name(id, name),
                    "text": content,
                    "conversation_id": conversation_id,
                });
                if let Some(to_id) = to_id {
                    m["replying_to"] = json!(self.display_name(to_id, to_name));
                }
                m
            })
            .collect();
        // Spelled out so Jev needn't infer them: which of the offered conversations
        // the author is in, and how fresh Kiki's last answer to them is.
        let offered: Vec<i64> = active.iter().filter_map(|c| c["id"].as_i64()).collect();
        let theirs: Vec<i64> = sqlx::query_scalar(
            "SELECT DISTINCT cm.conversation_id FROM conversation_messages cm \
             JOIN messages m ON m.id = cm.message_id \
             WHERE m.author_id = $1 AND cm.conversation_id = ANY($2) ORDER BY 1",
        )
        .bind(key.user_id)
        .bind(&offered)
        .fetch_all(&self.db)
        .await?;
        let answered: Option<i64> = sqlx::query_scalar(
            r#"
            SELECT extract(epoch FROM now() - k.created_at)::bigint
            FROM messages k
            JOIN agent_runs ar ON ar.reply_message_ids @> ARRAY[k.id]
            JOIN messages r ON r.id = ar.reply_to_id
            WHERE k.channel_id = $1 AND k.author_id = $2 AND r.author_id = $3 AND k.id < $4
            ORDER BY k.id DESC LIMIT 1
            "#,
        )
        .bind(key.channel_id)
        .bind(self.bot_user_id)
        .bind(key.user_id)
        .bind(first)
        .fetch_optional(&self.db)
        .await?
        .flatten();
        Ok(json!({
            "recent_channel_messages": recent,
            "conversations": active,
            "new_messages": {
                "from": self.display_name(key.user_id, author),
                "texts": messages.iter().map(|m| m.content.as_str()).collect::<Vec<_>>(),
                "author_in_conversations": theirs,
                "kiki_last_answered_author_seconds_ago": answered,
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
    /// them into their conversation and queues the agent. One transaction. The
    /// run stores this span's trace context, so the agent continues the trace.
    #[allow(clippy::too_many_arguments)]
    #[tracing::instrument(
        name = "tag",
        skip_all,
        fields(conversation_id = Empty, run_id = Empty, evicted = Empty)
    )]
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
        let span = Span::current();
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
            let max = self.settings.active_limit(&mut *tx, pool.guild_id).await?;
            evicted = conversation::evict_beyond(&mut tx, pool.guild_id, max).await?;
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
            run_id = Some(
                sqlx::query_scalar(
                    "INSERT INTO agent_runs (conversation_id, trigger_message_ids, trace_context) \
                 VALUES ($1, $2, $3) RETURNING id",
                )
                .bind(conversation_id)
                .bind(&ids)
                .bind(telemetry::traceparent())
                .fetch_one(&mut *tx)
                .await?,
            );
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

        metrics::classification(check.as_str(), outcome_name);
        if outcome_name == "new" {
            metrics::conversation_started();
        }
        metrics::conversations_closed("evicted", evicted.iter().map(|&(_, n)| n));
        if run_id.is_some() {
            metrics::run_queued(metrics::run_kind(Some(check.as_str())));
        }
        let evicted: Vec<i64> = evicted.into_iter().map(|(id, _)| id).collect();
        span.record("conversation_id", conversation_id);
        span.record("run_id", run_id);
        span.record("evicted", evicted.len());
        // Shown until the agent replies (it removes the reaction then). Explicit
        // triggers (check 1) were already reacted to when they were stored.
        if pool.settings.status_reactions
            && conversation_id.is_some()
            && !matches!(check, Check::Reply | Check::Mention)
        {
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

fn has_keyword(content: &str, keywords: &[String]) -> bool {
    content
        .to_lowercase()
        .split(|c: char| !c.is_alphanumeric())
        .any(|word| keywords.iter().any(|k| k == word))
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

    use super::{addresses_you, any_recent, is_stale, names_kiki};

    #[test]
    fn kiki_as_the_first_or_last_word_names_her() {
        let k = ["kiki".to_owned()];
        assert!(names_kiki("kiki this guy hates you", &k));
        assert!(names_kiki("u up Kiki?", &k));
        assert!(!names_kiki("did you see what kiki said", &k));
        assert!(!names_kiki(
            "yeah kiki doesn't, it has intent based detection",
            &k
        ));
    }

    #[test]
    fn second_person_or_a_question_addresses_kiki() {
        assert!(addresses_you(
            "Do I not have to reply to you to get a response"
        ));
        assert!(addresses_you("u up"));
        assert!(addresses_you("you're wrong"));
        assert!(addresses_you("wait what?"));
        assert!(!addresses_you("💀💀💀"));
        assert!(!addresses_you("lmao yall are insane"));
        assert!(!addresses_you("youtube is down"));
    }

    #[test]
    fn staleness_uses_the_chosen_conversation() {
        let active = [
            json!({ "id": 5, "last_active_seconds_ago": 4512 }),
            json!({ "id": 6, "last_active_seconds_ago": 30 }),
        ];
        assert!(is_stale(&active, 5, 1800));
        assert!(!is_stale(&active, 6, 1800));
        assert!(!is_stale(&active, 7, 1800));
        assert!(any_recent(&active, 60));
        assert!(!any_recent(&active[..1], 3600));
        assert!(!any_recent(&[], 3600));
    }
}
