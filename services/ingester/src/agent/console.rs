//! Chat with Kiki from the web console (docs/console-hld.md): the server's real
//! prompt, model and tools through the agent host, with nothing posted to
//! Discord or stored in `messages`, so the router never sees it.

use std::{sync::Arc, time::Instant};

use anyhow::{Context, ensure};
use sqlx::PgPool;

use super::{
    host::{Host, JobRequest, Scope},
    prompt::{self, GuildPrompt},
};
use crate::{chat::Message, config::AgentConfig, guild_settings};

pub const MAX_TURNS: usize = 60;
pub const MAX_TURN_CHARS: usize = 8000;

pub struct ConsoleChat {
    db: PgPool,
    host: Host,
    settings: Arc<guild_settings::Store>,
}

pub struct Reply {
    pub text: String,
    pub model: String,
    pub steps: i32,
    pub prompt_tokens: i32,
    pub completion_tokens: i32,
    pub cost_usd: Option<f64>,
    pub latency_ms: i64,
}

impl ConsoleChat {
    pub fn new(
        db: PgPool,
        config: &AgentConfig,
        settings: Arc<guild_settings::Store>,
    ) -> anyhow::Result<Self> {
        Ok(Self {
            db,
            host: Host::new(config)?,
            settings,
        })
    }

    /// The system prompt a console chat in this server gets.
    pub async fn system_prompt(&self, guild_id: i64, actor_name: &str) -> anyhow::Result<String> {
        let (guild_name,): (Option<String>,) =
            sqlx::query_as("SELECT (SELECT name FROM guilds WHERE id = $1)")
                .bind(guild_id)
                .fetch_one(&self.db)
                .await?;
        let guild_prompt = GuildPrompt::load(&self.db, guild_id).await?;
        let place = match guild_name {
            Some(name) => format!(" in the Discord server \"{name}\""),
            None => " on Discord".to_owned(),
        };
        let mut system = prompt::instructions(&place, &guild_prompt);
        system.push_str(&format!(
            "\nRight now you're in the Kiki console: a private web chat with {actor_name}, an admin of this server, outside Discord. Nobody else in the server sees it, and their messages here have no sender prefix. Reply in one message under 1900 characters, and always answer."
        ));
        Ok(system)
    }

    /// `turns` alternate between `user` and `assistant` and end with `user`.
    pub async fn reply(
        &self,
        guild_id: i64,
        job_id: &str,
        actor_name: &str,
        turns: &[Message],
    ) -> anyhow::Result<Reply> {
        validate(job_id, turns)?;
        let settings = self.settings.snapshot(guild_id);
        let now: String = sqlx::query_scalar(
            "SELECT to_char(now() AT TIME ZONE 'America/New_York', 'Dy YYYY-MM-DD HH24:MI')",
        )
        .fetch_one(&self.db)
        .await?;
        let mut messages = vec![Message::system(
            self.system_prompt(guild_id, actor_name).await?,
        )];
        messages.extend(turns.iter().cloned());
        let last = messages.last_mut().context("no turns")?;
        last.content = format!(
            "(Context for this reply:)\nConversational time: {now} US Eastern (America/New_York; DST-aware).\n\n(Messages:)\n{}",
            last.content
        );
        let job = JobRequest {
            id: job_id.to_owned(),
            kind: "reply".into(),
            scope: Scope::new(guild_id, 0, 0),
            model: settings.chat_model.clone(),
            messages,
            max_output_tokens: settings.reply_max_tokens,
            people: vec![],
        };
        let started = Instant::now();
        let result = self.host.execute(&job).await?;
        Ok(Reply {
            text: result.text,
            model: result.model.unwrap_or(job.model),
            steps: result.steps,
            prompt_tokens: result.usage.prompt_tokens,
            completion_tokens: result.usage.completion_tokens,
            cost_usd: result.usage.cost,
            latency_ms: started.elapsed().as_millis() as i64,
        })
    }
}

/// Checks a request before any work: the caller answers 422 on failure.
pub fn validate(job_id: &str, turns: &[Message]) -> anyhow::Result<()> {
    ensure!(
        job_id
            .strip_prefix("console-")
            .is_some_and(|n| !n.is_empty()
                && n.len() <= 40
                && n.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-')),
        "job_id must look like console-<id>"
    );
    validate_turns(turns)
}

fn validate_turns(turns: &[Message]) -> anyhow::Result<()> {
    ensure!(
        (1..=MAX_TURNS).contains(&turns.len()),
        "send 1 to {MAX_TURNS} messages"
    );
    ensure!(
        turns.len() % 2 == 1,
        "messages must start and end with the user"
    );
    for (i, turn) in turns.iter().enumerate() {
        let expected = if (turns.len() - i) % 2 == 1 {
            "user"
        } else {
            "assistant"
        };
        ensure!(
            turn.role == expected,
            "messages must alternate user/assistant and end with user"
        );
        ensure!(
            !turn.content.trim().is_empty() && turn.content.chars().count() <= MAX_TURN_CHARS,
            "each message needs 1 to {MAX_TURN_CHARS} characters"
        );
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn turns_alternate_and_end_with_the_user() {
        let u = |t: &str| Message::user(t);
        let a = |t: &str| Message::assistant(t);
        assert!(validate_turns(&[u("hi")]).is_ok());
        assert!(validate_turns(&[u("hi"), a("hey"), u("sup")]).is_ok());
        assert!(validate_turns(&[]).is_err());
        assert!(validate_turns(&[u("hi"), a("hey")]).is_err());
        assert!(validate_turns(&[a("hey"), u("hi")]).is_err());
        assert!(validate_turns(&[u("hi"), u("again")]).is_err());
        assert!(validate_turns(&[Message::system("x"), a("y"), u("z")]).is_err());
        assert!(validate_turns(&[u("  ")]).is_err());
        assert!(validate_turns(&[u(&"x".repeat(MAX_TURN_CHARS + 1))]).is_err());
        assert!(validate("console-12", &[u("hi")]).is_ok());
        for id in ["reply:12", "console-", "console-1/2", "console-1 "] {
            assert!(validate(id, &[u("hi")]).is_err(), "{id}");
        }
    }
}
