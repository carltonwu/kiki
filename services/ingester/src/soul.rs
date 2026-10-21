//! Each server's SOUL: who Kiki is there, after OpenClaw's `SOUL.md`
//! (<https://docs.openclaw.ai/concepts/soul>). Voice, stance and the room she's
//! in; tools, people handling and delivery stay in the fixed prompt. Server
//! admins edit it with `/kiki soul`. It sits in the system prompt, so a change
//! applies from the next run.

use sqlx::PgPool;

/// For a server that hasn't written its own.
pub const DEFAULT: &str = include_str!("soul.md");
/// Discord's limit on a modal text input; short beats long anyway.
pub const MAX_CHARS: usize = 4000;

/// The current SOUL: the server's own, if set.
pub struct Soul {
    pub text: String,
    /// Who last set it and when (unix seconds); `None` for the default.
    pub set: Option<(Option<i64>, i64)>,
}

pub async fn get(db: &PgPool, guild_id: i64) -> sqlx::Result<Soul> {
    let row: Option<(Option<String>, Option<i64>, i64)> = sqlx::query_as(
        "SELECT soul, updated_by, extract(epoch FROM updated_at)::BIGINT \
         FROM guild_soul_revisions WHERE guild_id = $1 ORDER BY id DESC LIMIT 1",
    )
    .bind(guild_id)
    .fetch_optional(db)
    .await?;
    Ok(match row {
        Some((Some(text), by, at)) => Soul {
            text,
            set: Some((by, at)),
        },
        _ => Soul {
            text: DEFAULT.trim().to_owned(),
            set: None,
        },
    })
}

/// Sets the server's SOUL, or with `None` returns it to the default.
pub async fn set(db: &PgPool, guild_id: i64, soul: Option<&str>, actor: i64) -> sqlx::Result<()> {
    sqlx::query(
        "INSERT INTO guild_soul_revisions (guild_id, soul, updated_by) VALUES ($1, $2, $3)",
    )
    .bind(guild_id)
    .bind(soul)
    .bind(actor)
    .execute(db)
    .await?;
    Ok(())
}

/// Normalizes an edit: trimmed, CRLF to LF, non-empty and within [`MAX_CHARS`].
pub fn validate(text: &str) -> Result<String, String> {
    let text = text.replace("\r\n", "\n").trim().to_owned();
    if text.is_empty() {
        return Err("The SOUL can't be empty. Use `reset:true` to go back to the default.".into());
    }
    let chars = text.chars().count();
    if chars > MAX_CHARS {
        return Err(format!(
            "The SOUL is {chars} characters; keep it to {MAX_CHARS}."
        ));
    }
    Ok(text)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn validates_edits() {
        assert_eq!(validate("  a\r\nb \n").unwrap(), "a\nb");
        assert!(validate(" \n ").is_err());
        assert!(validate(&"é".repeat(MAX_CHARS)).is_ok());
        assert!(validate(&"é".repeat(MAX_CHARS + 1)).is_err());
        assert!(DEFAULT.trim().chars().count() <= MAX_CHARS);
    }
}
