//! Emoji reactions, written in a reply plan as a line like `react: 😂 <:froge:123>`.
//!
//! Kiki reacts with Unicode emoji or with custom emoji written the way Discord
//! renders them (`<:name:id>`, `<a:name:id>`). A shortcode such as `:huh:` is
//! looked up in the server's emoji catalog ([`crate::emojis`]); Unicode
//! shortcodes like `:skull:` aren't resolved, but never leak into a message.
use std::fmt;

use sqlx::PgPool;
use tracing::debug;
use twilight_http::request::channel::reaction::RequestReactionType;
use twilight_model::id::Id;
use unicode_segmentation::UnicodeSegmentation;

use super::delivery::Plan;
use crate::emojis::Emoji;

/// Reactions per turn; more reads as spam.
pub(super) const MAX_REACTIONS: usize = 3;

#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) enum Reaction {
    Unicode(String),
    Custom {
        name: String,
        id: u64,
        animated: bool,
    },
}

impl Reaction {
    pub fn request(&self) -> RequestReactionType<'_> {
        match self {
            Self::Unicode(name) => RequestReactionType::Unicode { name },
            Self::Custom { name, id, .. } => RequestReactionType::Custom {
                id: Id::new(*id),
                name: Some(name),
            },
        }
    }
}

impl From<&Emoji> for Reaction {
    fn from(emoji: &Emoji) -> Self {
        Self::Custom {
            name: emoji.name.clone(),
            id: emoji.id as u64,
            animated: emoji.animated,
        }
    }
}

/// Adds the plan's `:name:` reactions that name one of the server's usable
/// custom emoji, after the ones written out, up to [`MAX_REACTIONS`].
pub(super) async fn resolve(db: &PgPool, guild_id: i64, plan: &mut Plan) -> sqlx::Result<()> {
    if plan.shortcodes.is_empty() {
        return Ok(());
    }
    let found = crate::emojis::named(db, guild_id, &plan.shortcodes).await?;
    for name in &plan.shortcodes {
        match found.get(&name.to_lowercase()) {
            Some(emoji) => {
                let reaction = Reaction::from(emoji);
                if !plan.reactions.contains(&reaction) {
                    plan.reactions.push(reaction);
                }
            }
            None => debug!(name, "no server emoji for a reaction shortcode"),
        }
    }
    plan.reactions.truncate(MAX_REACTIONS);
    Ok(())
}

/// As written in Discord message text, which is also how it's stored and shown
/// to the model.
impl fmt::Display for Reaction {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Unicode(emoji) => f.write_str(emoji),
            Self::Custom { name, id, animated } => {
                write!(f, "<{}:{name}:{id}>", if *animated { "a" } else { "" })
            }
        }
    }
}

#[derive(Debug, Default, PartialEq)]
pub(super) struct Line {
    pub reactions: Vec<Reaction>,
    /// Shortcode names (`huh` for `:huh:`), for the catalog to resolve.
    pub unresolved: Vec<String>,
}

/// A trimmed line that is `react:` followed only by emoji (separated by spaces
/// or commas, or not at all). Anything else, such as "react: idk", stays text.
pub(super) fn parse_line(line: &str) -> Option<Line> {
    let prefix = line.get(..6)?;
    if !prefix.eq_ignore_ascii_case("react:") || line[6..].trim().is_empty() {
        return None;
    }
    let mut parsed = Line::default();
    let mut rest = &line[6..];
    while let Some(c) = rest.chars().next() {
        let len = if c.is_whitespace() || c == ',' {
            c.len_utf8()
        } else if c == '<' {
            let (reaction, len) = custom(rest)?;
            parsed.reactions.push(reaction);
            len
        } else if c == ':' {
            let end = rest[1..].find(':')? + 1;
            let name = &rest[1..end];
            if !valid_name(name) {
                return None;
            }
            parsed.unresolved.push(name.to_owned());
            end + 1
        } else {
            let grapheme = rest.graphemes(true).next()?;
            if !is_emoji(grapheme) {
                return None;
            }
            parsed
                .reactions
                .push(Reaction::Unicode(grapheme.to_owned()));
            grapheme.len()
        };
        rest = &rest[len..];
    }
    Some(parsed)
}

/// `<:name:id>` or `<a:name:id>` at the start of `text`, tolerating the
/// `<:name:id:>` models often write; returns its length in bytes.
pub(super) fn custom(text: &str) -> Option<(Reaction, usize)> {
    let end = text.find('>')?;
    let inner = &text[1..end];
    let (animated, inner) = match inner.strip_prefix("a:") {
        Some(inner) => (true, inner),
        None => (false, inner.strip_prefix(':')?),
    };
    let (name, id) = inner.split_once(':')?;
    let id = id.strip_suffix(':').unwrap_or(id);
    if !valid_name(name) || id.is_empty() || !id.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    let id = id.parse().ok().filter(|&id| id > 0)?;
    let name = name.to_owned();
    Some((Reaction::Custom { name, id, animated }, end + 1))
}

fn valid_name(name: &str) -> bool {
    (1..=32).contains(&name.len()) && name.chars().all(|c| c.is_ascii_alphanumeric() || c == '_')
}

/// Pictographs and symbols (with any modifiers, joiners and flags in the same
/// grapheme), and keycaps. Discord rejects the odd invalid one; that's logged.
fn is_emoji(grapheme: &str) -> bool {
    let first = grapheme.chars().next();
    let keycap = grapheme.ends_with('\u{20e3}') && matches!(first, Some('0'..='9' | '#' | '*'));
    keycap
        || first.is_some_and(|c| {
            matches!(c as u32,
                0xa9 | 0xae | 0x203c | 0x2049 | 0x2122 | 0x2139 | 0x2190..=0x21ff
                | 0x2300..=0x27bf | 0x2900..=0x297f | 0x2b00..=0x2bff | 0x3030 | 0x303d
                | 0x3297 | 0x3299 | 0x1f000..=0x1faff)
        })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn reactions(line: &str) -> Option<Vec<String>> {
        parse_line(line).map(|l| l.reactions.iter().map(ToString::to_string).collect())
    }

    #[test]
    fn parses_unicode_and_custom_emoji() {
        assert_eq!(reactions("react: 😂").unwrap(), ["😂"]);
        assert_eq!(reactions("React:😂💀, 👍🏽").unwrap(), ["😂", "💀", "👍🏽"]);
        assert_eq!(
            reactions("react: 👩‍👩‍👧 🏳️‍🌈 🇨🇦 1️⃣ ❤️").unwrap(),
            ["👩‍👩‍👧", "🏳️‍🌈", "🇨🇦", "1️⃣", "❤️"]
        );
        assert_eq!(
            reactions("react: <:froge:814672652526420008> <a:ape:1444819174172922043:>").unwrap(),
            ["<:froge:814672652526420008>", "<a:ape:1444819174172922043>"]
        );
        let custom = &parse_line("react: <:froge:81>").unwrap().reactions[0];
        assert_eq!(
            *custom,
            Reaction::Custom {
                name: "froge".into(),
                id: 81,
                animated: false
            }
        );
    }

    #[test]
    fn shortcodes_are_markers_without_reactions() {
        let line = parse_line("react: :skull: 😂").unwrap();
        assert_eq!(line.unresolved, ["skull"]);
        assert_eq!(line.reactions, [Reaction::Unicode("😂".into())]);
    }

    #[test]
    fn ordinary_text_is_not_a_marker() {
        for line in [
            "react: idk",
            "react: 😂 lol",
            "react:",
            "react:   ",
            "reaction: 😂",
            "i'd react: 😂",
            "react: <:froge:abc>",
            "react: <:fro ge:81>",
            "react: :not a code:",
            "react: …",
            "react: 1",
            "réact: 😂",
        ] {
            assert_eq!(parse_line(line), None, "{line}");
        }
    }
}
