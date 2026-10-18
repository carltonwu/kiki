//! Natural message boundaries; ordinary text from older jobs still works.
use std::time::Duration;

use super::{
    clean_reply,
    discord::{MAX_MESSAGE_CHARS, split_message},
    is_no_reply,
};
use crate::guild_settings::GuildSettings;
use anyhow::{bail, ensure};

#[derive(Debug)]
pub(super) struct Message {
    pub text: String,
    pub delay: Duration,
}

pub(super) fn plan(text: &str, settings: &GuildSettings) -> anyhow::Result<Vec<Message>> {
    ensure!(
        !text.trim().is_empty(),
        "agent host returned an empty reply"
    );
    let mut parts = Vec::<Message>::new();
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
        let pause = fence
            .is_none()
            .then(|| separator(trimmed, settings.chat_max_delay))
            .flatten();
        if let Some(next_delay) = pause {
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
    if parts.iter().any(|p| is_no_reply(&p.text)) {
        if parts.len() == 1 && settings.chat_allow_silence {
            return Ok(vec![]);
        }
        bail!("invalid silence marker in reply plan");
    }
    parts.retain(|p| !p.text.is_empty());
    ensure!(!parts.is_empty(), "agent returned no message content");
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
    Ok(messages)
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
        .unwrap();
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
            plan("Kiki: hey\n---\nthere", &settings).unwrap()[0].text,
            "hey\nthere"
        );
        assert_eq!(plan("```\n---\n```", &settings).unwrap().len(), 1);
        assert_eq!(plan(&"é".repeat(2000), &settings).unwrap().len(), 2);
        assert_eq!(plan("plain text", &settings).unwrap()[0].text, "plain text");
        settings.chat_max_messages = 3;
        assert_eq!(plan("```ls```\n---\nok", &settings).unwrap().len(), 2);
        assert_eq!(plan("~~~\n```\n---\n~~~", &settings).unwrap().len(), 1);
        assert_eq!(plan("----\nhr stays", &settings).unwrap().len(), 1);
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
        assert!(plan("NO_REPLY", &settings).unwrap().is_empty());
        assert!(plan("NO_REPLY\n---\nhello", &settings).is_err());
        settings.chat_allow_silence = false;
        assert!(plan("NO_REPLY", &settings).is_err());
        assert!(plan(" ", &settings).is_err());
    }
}
