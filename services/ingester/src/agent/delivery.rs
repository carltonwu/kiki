//! Natural message boundaries and reactions; ordinary text from older jobs
//! still works.
use std::time::Duration;

use tracing::debug;

use super::{
    clean_reply,
    discord::{MAX_MESSAGE_CHARS, split_message},
    emoji::{self, MAX_REACTIONS, Reaction},
    is_no_reply,
};
use crate::guild_settings::GuildSettings;
use anyhow::{bail, ensure};

#[derive(Debug)]
pub(super) struct Message {
    pub text: String,
    pub delay: Duration,
}

/// What Kiki does this turn. Both empty means silence; reactions alone are a
/// reaction-only answer, which counts as sitting out.
#[derive(Debug, Default)]
pub(super) struct Plan {
    pub messages: Vec<Message>,
    /// For the newest trigger message, added before any message is sent.
    pub reactions: Vec<Reaction>,
}

pub(super) fn plan(text: &str, settings: &GuildSettings) -> anyhow::Result<Plan> {
    ensure!(
        !text.trim().is_empty(),
        "agent host returned an empty reply"
    );
    let mut parts = Vec::<Message>::new();
    let mut reactions = Vec::<Reaction>::new();
    let mut reacted = false;
    let mut body = String::new();
    let mut delay = Duration::ZERO;
    let mut fence = None;
    for line in text.lines() {
        let trimmed = line.trim();
        // A line like ```ls``` opens and closes inline; it isn't a fence.
        let marker = ['`', '~'].into_iter().find(|&c| {
            let run = String::from(c).repeat(3);
            trimmed.starts_with(&run) && !trimmed[3..].contains(&run)
        });
        if let Some(marker) = marker {
            if fence == Some(marker) {
                fence = None;
            } else if fence.is_none() {
                fence = Some(marker);
            }
        }
        let reaction = fence
            .is_none()
            .then(|| emoji::parse_line(trimmed))
            .flatten();
        let pause = fence
            .is_none()
            .then(|| separator(trimmed, settings.chat_max_delay))
            .flatten();
        if let Some(line) = reaction {
            reacted = true;
            if !line.unresolved.is_empty() {
                debug!(shortcodes = ?line.unresolved, "skipped unresolved emoji shortcodes");
            }
            for reaction in line.reactions {
                if !reactions.contains(&reaction) {
                    reactions.push(reaction);
                }
            }
        } else if let Some(next_delay) = pause {
            if !body.trim().is_empty() {
                parts.push(Message {
                    text: clean_reply(&body),
                    delay,
                });
                body.clear();
            }
            delay = next_delay;
        } else {
            body.push_str(line);
            body.push('\n');
        }
    }
    if !body.trim().is_empty() {
        parts.push(Message {
            text: clean_reply(&body),
            delay,
        });
    }
    reactions.truncate(MAX_REACTIONS);
    if !settings.chat_reactions {
        reactions.clear();
    }
    if parts.iter().any(|p| is_no_reply(&p.text)) {
        if parts.len() == 1 && settings.chat_allow_silence {
            return Ok(Plan {
                messages: vec![],
                reactions,
            });
        }
        bail!("invalid silence marker in reply plan");
    }
    parts.retain(|p| !p.text.is_empty());
    if parts.is_empty() {
        ensure!(reacted, "agent returned no message content");
        ensure!(
            settings.chat_allow_silence,
            "reaction without a message, but this server disabled silence"
        );
        return Ok(Plan {
            messages: vec![],
            reactions,
        });
    }
    // Preserve all content when the model exceeds the requested message count.
    if parts.len() > settings.chat_max_messages {
        let extra = parts.split_off(settings.chat_max_messages);
        let last = parts.last_mut().expect("positive configured maximum");
        for message in extra {
            last.text.push('\n');
            last.text.push_str(&message.text);
        }
    }
    let mut budget = Duration::from_secs(8);
    let mut messages = Vec::new();
    for part in parts {
        for (i, text) in split_message(&part.text, MAX_MESSAGE_CHARS)
            .into_iter()
            .enumerate()
        {
            let delay = if messages.is_empty() || i > 0 {
                Duration::ZERO
            } else {
                part.delay.min(budget)
            };
            budget -= delay;
            messages.push(Message { text, delay });
        }
    }
    Ok(Plan {
        messages,
        reactions,
    })
}

/// `---` (short default pause) or `--- 1.5s` / `--- 1500ms` / `--- 2`
/// (seconds); malformed pauses still separate so markers never leak.
fn separator(line: &str, max_delay_ms: u64) -> Option<Duration> {
    let rest = line.strip_prefix("---")?.trim();
    let max = Duration::from_millis(max_delay_ms);
    if rest.is_empty() {
        return Some(max.min(Duration::from_secs(1)));
    }
    let (number, scale) = match rest.strip_suffix("ms") {
        Some(ms) => (ms, 0.001),
        None => (rest.strip_suffix('s').unwrap_or(rest), 1.0),
    };
    let seconds = number.trim().parse::<f64>().ok()? * scale;
    Some(if seconds.is_finite() && seconds >= 0.0 {
        Duration::from_secs_f64(seconds.min(max.as_secs_f64()))
    } else {
        max.min(Duration::from_secs(1))
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::guild_settings::tests::defaults;

    #[test]
    fn parses_beats_and_clamps_pauses_and_total() {
        let mut settings = defaults();
        settings.chat_max_messages = 8;
        settings.chat_max_delay = 10_000;
        let messages = plan(
            "hey\n--- 999999999999999999s\nthere\n--- 10s\ngirl",
            &settings,
        )
        .unwrap()
        .messages;
        assert_eq!(
            messages.iter().map(|m| m.text.as_str()).collect::<Vec<_>>(),
            ["hey", "there", "girl"]
        );
        assert_eq!(messages[0].delay, Duration::ZERO);
        assert_eq!(messages[1].delay, Duration::from_secs(8));
        assert_eq!(messages[2].delay, Duration::ZERO);
    }

    #[test]
    fn preserves_code_and_content_and_legacy_text() {
        let mut settings = defaults();
        settings.chat_max_messages = 1;
        assert_eq!(
            plan("Kiki: hey\n---\nthere", &settings).unwrap().messages[0].text,
            "hey\nthere"
        );
        assert_eq!(plan("```\n---\n```", &settings).unwrap().messages.len(), 1);
        assert_eq!(
            plan(&"é".repeat(2000), &settings).unwrap().messages.len(),
            2
        );
        assert_eq!(
            plan("plain text", &settings).unwrap().messages[0].text,
            "plain text"
        );
        settings.chat_max_messages = 3;
        assert_eq!(
            plan("```ls```\n---\nok", &settings).unwrap().messages.len(),
            2
        );
        assert_eq!(
            plan("~~~\n```\n---\n~~~", &settings)
                .unwrap()
                .messages
                .len(),
            1
        );
        assert_eq!(plan("----\nhr stays", &settings).unwrap().messages.len(), 1);
    }

    #[test]
    fn separator_forms() {
        let s = |line| separator(line, 3000);
        assert_eq!(s("---"), Some(Duration::from_secs(1)));
        assert_eq!(s("--- 1.5s"), Some(Duration::from_millis(1500)));
        assert_eq!(s("---2 s"), Some(Duration::from_secs(2)));
        assert_eq!(s("--- 500ms"), Some(Duration::from_millis(500)));
        assert_eq!(s("--- 9s"), Some(Duration::from_secs(3)));
        assert_eq!(s("--- -1s"), Some(Duration::from_secs(1)));
        assert_eq!(s("----"), None);
        assert_eq!(s("--- later"), None);
        assert_eq!(separator("---", 0), Some(Duration::ZERO));
    }

    #[test]
    fn silence_requires_permission_and_cannot_be_mixed_with_messages() {
        let mut settings = defaults();
        assert!(plan("NO_REPLY", &settings).unwrap().messages.is_empty());
        assert!(plan("NO_REPLY\n---\nhello", &settings).is_err());
        settings.chat_allow_silence = false;
        assert!(plan("NO_REPLY", &settings).is_err());
        assert!(plan(" ", &settings).is_err());
    }

    #[test]
    fn reactions_alone_or_with_messages() {
        let mut settings = defaults();
        let texts = |plan: &Plan| {
            plan.messages
                .iter()
                .map(|m| m.text.clone())
                .collect::<Vec<_>>()
        };
        let emoji = |plan: &Plan| {
            plan.reactions
                .iter()
                .map(ToString::to_string)
                .collect::<Vec<_>>()
        };

        let only = plan("react: 💀", &settings).unwrap();
        assert!(only.messages.is_empty());
        assert_eq!(emoji(&only), ["💀"]);

        let both = plan(
            "react: 😂 <:froge:814672652526420008:>\nstop it\n---\nreact: 😂 💀\nbye",
            &settings,
        )
        .unwrap();
        assert_eq!(texts(&both), ["stop it", "bye"]);
        assert_eq!(
            emoji(&both),
            ["😂", "<:froge:814672652526420008>", "💀"],
            "deduplicated, in order"
        );
        let many = plan("react: 😂 💀 👍 🔥\nok", &settings).unwrap();
        assert_eq!(emoji(&many), ["😂", "💀", "👍"]);

        // Silence with a reaction is a reaction; unresolved ones are still hidden.
        assert_eq!(
            emoji(&plan("NO_REPLY\nreact: 👀", &settings).unwrap()),
            ["👀"]
        );
        let hidden = plan("react: :skull:", &settings).unwrap();
        assert!(hidden.messages.is_empty() && hidden.reactions.is_empty());
        // Not markers: kept as text, and code is left alone.
        assert_eq!(
            texts(&plan("react: idk", &settings).unwrap()),
            ["react: idk"]
        );
        assert_eq!(
            texts(&plan("```\nreact: 😂\n```", &settings).unwrap()),
            ["```\nreact: 😂\n```"]
        );

        settings.chat_reactions = false;
        let off = plan("react: 😂\nhey", &settings).unwrap();
        assert_eq!(
            (texts(&off), off.reactions.len()),
            (vec!["hey".to_owned()], 0)
        );
        assert!(plan("react: 😂", &settings).unwrap().reactions.is_empty());

        settings.chat_reactions = true;
        settings.chat_allow_silence = false;
        assert!(plan("react: 😂", &settings).is_err());
        assert!(plan("NO_REPLY\nreact: 😂", &settings).is_err());
        assert_eq!(emoji(&plan("react: 😂\nhaha", &settings).unwrap()), ["😂"]);
    }
}
