//! The `/kiki` slash command: inspection and server settings.
//!
//! Registered as a guild command at startup (guild commands update instantly,
//! global ones don't) and answered from `INTERACTION_CREATE` Gateway events.
//! Only members with Manage Server see it by default, since it shows costs and
//! topics from every channel; server admins can grant it to other roles under
//! Server Settings → Integrations. Replies are ephemeral and scoped to the
//! guild the command ran in.
//!
//! Discord markdown has no tables, so row-by-column data goes in code blocks:
//! the one layout that lines up on every client. Mentions and `<t:…>`
//! timestamps don't render inside code blocks, so those go in embed fields.

use std::{fmt::Write, sync::Arc, time::Duration};

use anyhow::Context;
use serde_json::Value;
use sqlx::{FromRow, PgPool};
use tracing::{info, warn};
use twilight_http::Client;
use twilight_model::{
    application::command::CommandType,
    channel::message::{AllowedMentions, Embed, MessageFlags},
    guild::Permissions,
    http::interaction::{InteractionResponse, InteractionResponseData, InteractionResponseType},
    id::{
        Id,
        marker::{ApplicationMarker, GuildMarker},
    },
    util::Timestamp,
};
use twilight_util::builder::{
    command::{
        BooleanBuilder, CommandBuilder, IntegerBuilder, StringBuilder, SubCommandBuilder,
        UserBuilder,
    },
    embed::{EmbedBuilder, EmbedFieldBuilder, EmbedFooterBuilder, ImageSource},
};

use crate::{
    catch_up::snowflake,
    config::{AgentConfig, RouterConfig},
    conversation, guild_settings,
    rest::get_json,
    router::unix_millis,
};

const APPLICATION_COMMAND: u64 = 2;
const COLOR: u32 = 0xF4_72_B6;
const ERROR_COLOR: u32 = 0xED_42_45;
/// Embed limits (Discord rejects the whole message past any of them).
const MAX_FIELDS: usize = 25;
const MAX_FIELD_CHARS: usize = 1024;
const MAX_DESCRIPTION_CHARS: usize = 4096;
const RUN_STATUSES: [&str; 6] = [
    "pending",
    "running",
    "delivering",
    "done",
    "failed",
    "superseded",
];

/// `(option value, column header)` of the time windows, oldest cutoff last.
/// The SQL numbers them 0, 1, 2 in this order.
const WINDOWS: [(&str, &str); 3] = [("24h", "24 h"), ("7d", "7 d"), ("all", "All")];
const WINDOW_COLUMNS: [(&str, Align); 4] = [
    ("", Align::Left),
    (WINDOWS[0].1, Align::Right),
    (WINDOWS[1].1, Align::Right),
    (WINDOWS[2].1, Align::Right),
];
const MAX_USER_ROWS: usize = 20;
/// How per-person cost is attributed.
const COST_NOTE: &str = "Cost: replies split across the messages that triggered them, plus Decide; topic summaries excluded";

/// One person's activity in one time window.
#[derive(FromRow)]
struct UserStats {
    #[sqlx(rename = "win")]
    window: i32,
    user_id: i64,
    name: Option<String>,
    messages: i64,
    /// Messages tagged into a conversation with Kiki.
    kiki_messages: i64,
    conversations: i64,
    cost: f64,
}

pub struct Commands {
    db: PgPool,
    http: Arc<Client>,
    application_id: Id<ApplicationMarker>,
    bot_user_id: i64,
    settings: Settings,
}

pub struct Settings {
    chat_model: Option<String>,
    topic_model: Option<String>,
    decide_model: String,
    decide_enabled: bool,
    global: Vec<(&'static str, String)>,
    guilds: Arc<guild_settings::Store>,
}

impl Settings {
    pub fn new(
        router: &RouterConfig,
        agent: &AgentConfig,
        guilds: Arc<guild_settings::Store>,
    ) -> Self {
        let enabled = agent.host_url.is_some();
        Self {
            chat_model: enabled.then(|| agent.chat_model.clone()),
            topic_model: enabled.then(|| agent.topic_model.clone()),
            decide_model: router.jev_model.clone(),
            decide_enabled: router.jev_api_key.is_some(),
            global: vec![
                (
                    "Pending emoji",
                    if router.pending_emoji.is_empty() {
                        "off".into()
                    } else {
                        router.pending_emoji.clone()
                    },
                ),
                ("Message age", duration(router.max_message_age)),
                ("Decide timeout", duration(router.jev_timeout)),
                ("Host timeout", duration(agent.host_timeout)),
                ("Run max age", duration(agent.run_max_age)),
            ],
            guilds,
        }
    }
}

struct Invocation<'a> {
    guild_id: i64,
    member: &'a Value,
    subcommand: &'a str,
    /// The subcommand's options: `[{name, type, value}]`.
    options: &'a Value,
    /// `data.resolved`: users and members referenced by options.
    resolved: &'a Value,
}

impl<'a> Invocation<'a> {
    fn option(&self, name: &str) -> Option<&'a Value> {
        self.options
            .as_array()?
            .iter()
            .find(|o| o["name"] == name)
            .map(|o| &o["value"])
    }
}

/// A failure the person running the command can fix, shown as-is.
#[derive(Debug)]
struct UserError(String);

impl std::fmt::Display for UserError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for UserError {}

impl Commands {
    pub async fn new(
        db: PgPool,
        http: Arc<Client>,
        bot_user_id: i64,
        settings: Settings,
    ) -> anyhow::Result<Self> {
        let app = get_json("current application", || http.current_user_application())
            .await?
            .context("can't read the bot's application")?;
        let application_id =
            Id::new(snowflake(&app["id"]).context("application has no id")? as u64);
        Ok(Self {
            db,
            http,
            application_id,
            bot_user_id,
            settings,
        })
    }

    /// Replaces the guild's commands with ours. Failure (e.g. the bot was invited
    /// without the `applications.commands` scope) is logged, not fatal.
    pub async fn register(&self, guild_ids: &[Id<GuildMarker>]) {
        let commands = [definition().build()];
        let interaction = self.http.interaction(self.application_id);
        for &guild_id in guild_ids {
            match interaction.set_guild_commands(guild_id, &commands).await {
                Ok(_) => info!(guild_id = guild_id.get(), "slash commands registered"),
                Err(e) => {
                    warn!(guild_id = guild_id.get(), error = %e, "couldn't register slash commands")
                }
            }
        }
    }

    /// Answers an `INTERACTION_CREATE` payload in the background. Defers first:
    /// Discord drops interactions not acknowledged within 3 seconds, and the
    /// stats queries can take longer on a cold database.
    pub fn spawn_handle(self: &Arc<Self>, interaction: Value) {
        if interaction["type"].as_u64() != Some(APPLICATION_COMMAND)
            || interaction["data"]["name"].as_str() != Some("kiki")
        {
            return;
        }
        let commands = self.clone();
        tokio::spawn(async move {
            let subcommand = interaction["data"]["options"][0]["name"]
                .as_str()
                .unwrap_or_default()
                .to_owned();
            if let Err(e) = commands.handle(&interaction).await {
                warn!(
                    subcommand,
                    guild_id = interaction["guild_id"].as_str(),
                    interaction_id = interaction["id"].as_str(),
                    error = format!("{e:#}"),
                    "couldn't answer slash command"
                );
            }
        });
    }

    async fn handle(&self, interaction: &Value) -> anyhow::Result<()> {
        let id = snowflake(&interaction["id"]).context("interaction has no id")?;
        let token = interaction["token"]
            .as_str()
            .context("interaction has no token")?;
        let client = self.http.interaction(self.application_id);
        let deferred = InteractionResponse {
            kind: InteractionResponseType::DeferredChannelMessageWithSource,
            data: Some(InteractionResponseData {
                flags: Some(MessageFlags::EPHEMERAL),
                ..Default::default()
            }),
        };
        client
            .create_response(Id::new(id as u64), token, &deferred)
            .await
            .context("deferring interaction response")?;

        let subcommand = &interaction["data"]["options"][0];
        let embeds = match snowflake(&interaction["guild_id"]) {
            None => vec![error_embed("`/kiki` only works in a server.")],
            Some(guild_id) => {
                let invocation = Invocation {
                    guild_id,
                    member: &interaction["member"],
                    subcommand: subcommand["name"].as_str().unwrap_or_default(),
                    options: &subcommand["options"],
                    resolved: &interaction["data"]["resolved"],
                };
                match self.run(&invocation).await {
                    Ok(embeds) => embeds,
                    Err(e) => match e.downcast_ref::<UserError>() {
                        Some(e) => vec![error_embed(&e.0)],
                        None => {
                            warn!(
                                subcommand = invocation.subcommand,
                                guild_id = invocation.guild_id,
                                interaction_id = interaction["id"].as_str(),
                                error = format!("{e:#}"),
                                "slash command failed"
                            );
                            vec![error_embed(
                                "Something went wrong. The ingester logs have the details.",
                            )]
                        }
                    },
                }
            }
        };
        let no_pings = AllowedMentions::default();
        client
            .update_response(token)
            .embeds(Some(&embeds))
            .allowed_mentions(Some(&no_pings))
            .await
            .context("sending interaction response")?;
        Ok(())
    }

    async fn run(&self, invocation: &Invocation<'_>) -> anyhow::Result<Vec<Embed>> {
        let embed = match invocation.subcommand {
            "conversations" => self.conversations(invocation.guild_id).await?,
            "clear" => self.clear(invocation).await?,
            "stats" => self.stats(invocation.guild_id).await?,
            "runs" => self.runs(invocation).await?,
            "config" => self.config(invocation).await?,
            "users" => self.users(invocation).await?,
            "whois" => self.whois(invocation).await?,
            other => error_embed(&format!("Unknown subcommand `{other}`.")),
        };
        Ok(vec![embed])
    }

    async fn conversations(&self, guild_id: i64) -> anyhow::Result<Embed> {
        let pool = conversation::active(&self.db, self.bot_user_id).await?;
        let here: Vec<_> = pool
            .into_iter()
            .filter(|c| c.guild_id == guild_id)
            .collect();
        let max = self
            .settings
            .guilds
            .snapshot(guild_id)
            .max_active_conversations;
        let footer = format!("{} of {max} server slots in use", here.len());
        let embed = EmbedBuilder::new()
            .color(COLOR)
            .title("Active conversations")
            .footer(EmbedFooterBuilder::new(footer))
            .timestamp(now());
        if here.is_empty() {
            return Ok(embed.description("No active conversations.").build());
        }

        let rows: Vec<Vec<String>> = here
            .iter()
            .enumerate()
            .map(|(i, c)| {
                vec![
                    (i + 1).to_string(),
                    channel_label(c.channel_name.as_deref(), c.channel_id),
                    c.message_count.to_string(),
                    c.participants.len().to_string(),
                    ago(c.last_active_at_ms),
                    usd(c.cost_usd),
                ]
            })
            .collect();
        let mut embed = embed.description(table(
            &[
                ("#", Align::Right),
                ("Channel", Align::Left),
                ("Msgs", Align::Right),
                ("People", Align::Right),
                ("Active", Align::Right),
                ("Cost", Align::Right),
            ],
            &rows,
        ));
        for (i, c) in here.iter().enumerate().take(MAX_FIELDS) {
            let people = if c.participants.is_empty() {
                "nobody yet".to_owned()
            } else {
                c.participants
                    .iter()
                    .map(|p| format!("<@{}> ({})", p.id, p.messages))
                    .collect::<Vec<_>>()
                    .join(", ")
            };
            let value = format!(
                "**Topic:** {}\n**People:** {people}\n**Started:** <t:{}:R> · **Last message:** <t:{}:R>",
                c.topic.as_deref().unwrap_or("*not summarized yet*"),
                c.created_at_ms / 1000,
                c.last_active_at_ms / 1000,
            );
            embed = embed.field(EmbedFieldBuilder::new(
                format!(
                    "{}. {}",
                    i + 1,
                    channel_label(c.channel_name.as_deref(), c.channel_id)
                ),
                truncate(&value, MAX_FIELD_CHARS),
            ));
        }
        Ok(embed.build())
    }

    /// Closes every active conversation in the server, so the next message
    /// starts fresh. History and costs stay.
    async fn clear(&self, invocation: &Invocation<'_>) -> anyhow::Result<Embed> {
        let actor = require_manage_server(invocation, "clear conversations")?;
        let mut tx = self.db.begin().await?;
        conversation::lock_pool(&mut tx).await?;
        let closed = conversation::clear(&mut tx, invocation.guild_id).await?;
        tx.commit().await?;
        info!(
            guild_id = invocation.guild_id,
            actor,
            ?closed,
            "conversations cleared"
        );
        let description = match closed.len() {
            0 => "No active conversations to clear.".to_owned(),
            1 => "Cleared 1 conversation. The next message starts a new one.".to_owned(),
            n => format!("Cleared {n} conversations. The next message starts a new one."),
        };
        Ok(EmbedBuilder::new()
            .color(COLOR)
            .title("Conversations cleared")
            .description(description)
            .timestamp(now())
            .build())
    }

    async fn stats(&self, guild_id: i64) -> anyhow::Result<Embed> {
        #[derive(FromRow)]
        struct Window {
            conversations: i64,
            messages: i64,
            replies: i64,
            failed: i64,
            p50_ms: Option<f64>,
            p95_ms: Option<f64>,
            via_reply: i64,
            via_mention: i64,
            via_keyword: i64,
            via_active: i64,
            via_none: i64,
            routed: i64,
            chat_cost: f64,
            decide_cost: f64,
        }
        #[derive(FromRow)]
        struct Now {
            pending: i64,
            running: i64,
            topic_cost: f64,
        }
        let windows: Vec<Window> = sqlx::query_as(
            r#"
            WITH w(n, since) AS (VALUES (0, now() - interval '24 hours'),
                                        (1, now() - interval '7 days'),
                                        (2, '-infinity'::timestamptz)),
            conv AS (SELECT id, created_at FROM conversations WHERE guild_id = $1),
            runs AS (SELECT r.status, r.latency_ms, r.cost_usd,
                            coalesce(r.finished_at, r.created_at) AS at
                     FROM agent_runs r JOIN conv ON conv.id = r.conversation_id),
            -- Decide cost is stored on every message of a pool; `newest` counts it once.
            cls AS (SELECT c.check_hit, c.outcome, c.jev_cost_usd, c.classified_at AS at,
                           c.message_id = c.pool_id AS newest
                    FROM classifications c JOIN messages m ON m.id = c.message_id
                    WHERE m.guild_id = $1),
            tagged AS (SELECT cm.tagged_at AS at
                       FROM conversation_messages cm JOIN conv ON conv.id = cm.conversation_id)
            SELECT
                (SELECT count(*) FROM conv WHERE created_at > w.since) AS conversations,
                (SELECT count(*) FROM tagged WHERE at > w.since) AS messages,
                (SELECT count(*) FROM runs WHERE status = 'done' AND at > w.since) AS replies,
                (SELECT count(*) FROM runs WHERE status = 'failed' AND at > w.since) AS failed,
                (SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms)
                 FROM runs WHERE status = 'done' AND at > w.since) AS p50_ms,
                (SELECT percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms)
                 FROM runs WHERE status = 'done' AND at > w.since) AS p95_ms,
                (SELECT count(*) FROM cls WHERE check_hit = 'reply' AND at > w.since) AS via_reply,
                (SELECT count(*) FROM cls WHERE check_hit = 'mention' AND at > w.since) AS via_mention,
                (SELECT count(*) FROM cls WHERE check_hit = 'keyword' AND at > w.since) AS via_keyword,
                (SELECT count(*) FROM cls WHERE check_hit = 'active_conversation' AND at > w.since)
                    AS via_active,
                (SELECT count(*) FROM cls WHERE check_hit = 'none' AND at > w.since) AS via_none,
                (SELECT count(*) FROM cls WHERE outcome <> 'no' AND at > w.since) AS routed,
                (SELECT coalesce(sum(cost_usd), 0) FROM runs WHERE at > w.since) AS chat_cost,
                (SELECT coalesce(sum(jev_cost_usd), 0) FROM cls WHERE newest AND at > w.since)
                    AS decide_cost
            FROM w ORDER BY w.n
            "#,
        )
        .bind(guild_id)
        .fetch_all(&self.db)
        .await?;
        let current: Now = sqlx::query_as(
            r#"
            SELECT
                count(*) FILTER (WHERE r.status = 'pending') AS pending,
                count(*) FILTER (WHERE r.status IN ('running', 'delivering')) AS running,
                (SELECT coalesce(sum(topic_cost_usd), 0) FROM conversations WHERE guild_id = $1)
                    AS topic_cost
            FROM agent_runs r JOIN conversations c ON c.id = r.conversation_id
            WHERE c.guild_id = $1 AND r.status IN ('pending', 'running', 'delivering')
            "#,
        )
        .bind(guild_id)
        .fetch_one(&self.db)
        .await?;

        let row = |label: &str, cell: &dyn Fn(&Window) -> String| -> Vec<String> {
            std::iter::once(label.to_owned())
                .chain(windows.iter().map(cell))
                .collect()
        };
        let activity = [
            row("Conversations", &|w| w.conversations.to_string()),
            row("Messages", &|w| w.messages.to_string()),
            row("Replies", &|w| w.replies.to_string()),
            row("Failed runs", &|w| w.failed.to_string()),
            row("Reply p50", &|w| seconds(w.p50_ms)),
            row("Reply p95", &|w| seconds(w.p95_ms)),
        ];
        let routing = [
            row("Reply to Kiki", &|w| w.via_reply.to_string()),
            row("@Kiki", &|w| w.via_mention.to_string()),
            row("Keyword", &|w| w.via_keyword.to_string()),
            row("Active chat", &|w| w.via_active.to_string()),
            row("No match", &|w| w.via_none.to_string()),
            row("Routed", &|w| w.routed.to_string()),
        ];
        // Topic summaries aren't timestamped, so their cost only has an all-time total.
        let topic_cell = |i: usize| {
            if i == 2 {
                usd(current.topic_cost)
            } else {
                "–".to_owned()
            }
        };
        let mut cost = vec![
            row("Chat", &|w| usd(w.chat_cost)),
            row("Decide", &|w| usd(w.decide_cost)),
        ];
        cost.push(
            std::iter::once("Topics".to_owned())
                .chain((0..windows.len()).map(topic_cell))
                .collect(),
        );
        cost.push(
            std::iter::once("Total".to_owned())
                .chain(windows.iter().enumerate().map(|(i, w)| {
                    let topics = if i == 2 { current.topic_cost } else { 0.0 };
                    usd(w.chat_cost + w.decide_cost + topics)
                }))
                .collect(),
        );

        let description = format!(
            "**Activity**\n{}**Routing** · messages by the first check they matched\n{}**Cost** · USD\n{}\
             Topic summaries have no timestamp, so their cost counts toward All only.",
            table(&WINDOW_COLUMNS, &activity),
            table(&WINDOW_COLUMNS, &routing),
            table(&WINDOW_COLUMNS, &cost),
        );
        Ok(EmbedBuilder::new()
            .color(COLOR)
            .title("Kiki stats")
            .description(truncate(&description, MAX_DESCRIPTION_CHARS))
            .field(EmbedFieldBuilder::new("Queued runs", current.pending.to_string()).inline())
            .field(EmbedFieldBuilder::new("Running", current.running.to_string()).inline())
            .footer(EmbedFooterBuilder::new("This server only"))
            .timestamp(now())
            .build())
    }

    async fn runs(&self, invocation: &Invocation<'_>) -> anyhow::Result<Embed> {
        #[derive(FromRow)]
        struct Run {
            id: i64,
            channel_id: i64,
            channel_name: Option<String>,
            status: String,
            latency_ms: Option<i32>,
            cost_usd: Option<f64>,
            error: Option<String>,
            created_at_ms: i64,
        }
        let status = invocation.option("status").and_then(Value::as_str);
        if status.is_some_and(|s| !RUN_STATUSES.contains(&s)) {
            return Err(
                UserError(format!("Unknown status `{}`.", status.unwrap_or_default())).into(),
            );
        }
        let limit = invocation
            .option("limit")
            .and_then(Value::as_i64)
            .unwrap_or(10)
            .clamp(1, 25);
        let runs: Vec<Run> = sqlx::query_as(
            r#"
            SELECT r.id, c.channel_id, ch.name AS channel_name, r.status, r.latency_ms, r.cost_usd,
                   r.error, (extract(epoch FROM r.created_at) * 1000)::bigint AS created_at_ms
            FROM agent_runs r
            JOIN conversations c ON c.id = r.conversation_id
            LEFT JOIN channels ch ON ch.id = c.channel_id
            WHERE c.guild_id = $1 AND ($2::text IS NULL OR r.status = $2)
            ORDER BY r.id DESC
            LIMIT $3
            "#,
        )
        .bind(invocation.guild_id)
        .bind(status)
        .bind(limit)
        .fetch_all(&self.db)
        .await?;

        let title = match status {
            Some(s) => format!("Agent runs · {s}"),
            None => "Agent runs".to_owned(),
        };
        let embed = EmbedBuilder::new()
            .color(COLOR)
            .title(title)
            .timestamp(now());
        if runs.is_empty() {
            return Ok(embed.description("No runs.").build());
        }
        let rows: Vec<Vec<String>> = runs
            .iter()
            .map(|r| {
                vec![
                    r.id.to_string(),
                    channel_label(r.channel_name.as_deref(), r.channel_id),
                    r.status.clone(),
                    seconds(r.latency_ms.map(f64::from)),
                    r.cost_usd.map_or("–".to_owned(), usd),
                    ago(r.created_at_ms),
                ]
            })
            .collect();
        let mut embed = embed.description(table(
            &[
                ("Run", Align::Right),
                ("Channel", Align::Left),
                ("Status", Align::Left),
                ("Time", Align::Right),
                ("Cost", Align::Right),
                ("Age", Align::Right),
            ],
            &rows,
        ));
        for r in runs.iter().filter(|r| r.error.is_some()).take(MAX_FIELDS) {
            let error = r.error.as_deref().unwrap_or_default();
            embed = embed.field(EmbedFieldBuilder::new(
                format!("Run {} · {}", r.id, r.status),
                code_block(&truncate(error, 300)),
            ));
        }
        Ok(embed.build())
    }

    async fn config(&self, invocation: &Invocation<'_>) -> anyhow::Result<Embed> {
        if let Some((actor, key, value)) = config_change(invocation)? {
            if matches!(key, "chat_model" | "image_model")
                && let Some(model) = value.as_ref().and_then(Value::as_str)
            {
                check_catalog_model(key, model).await?;
            }
            self.settings
                .guilds
                .change(invocation.guild_id, key, value, actor)
                .await
                .map_err(|e| UserError(e.to_string()))?;
            info!(
                guild_id = invocation.guild_id,
                actor, key, "server setting changed"
            );
        }
        let snapshot = self.settings.guilds.snapshot(invocation.guild_id);
        let key = invocation.option("key").and_then(Value::as_str);
        if let Some(key) = key {
            guild_settings::spec(key).map_err(|e| UserError(e.to_string()))?;
        }
        let global = format!(
            "Chat agent: {} · Decide: {}\nTopic model (global): {} · Decide model (global): {}\n{}",
            if self.settings.chat_model.is_some() {
                "on"
            } else {
                "off"
            },
            if self.settings.decide_enabled {
                "on"
            } else {
                "off"
            },
            self.settings.topic_model.as_deref().unwrap_or("off"),
            self.settings.decide_model,
            self.settings
                .global
                .iter()
                .map(|(k, v)| format!("{k}: {v}"))
                .collect::<Vec<_>>()
                .join(" · ")
        );
        Ok(config_embed(
            &snapshot,
            self.settings.guilds.default_values(),
            key,
            &global,
        ))
    }

    async fn whois(&self, invocation: &Invocation<'_>) -> anyhow::Result<Embed> {
        #[derive(FromRow)]
        struct Person {
            username: Option<String>,
            global_name: Option<String>,
            nick: Option<String>,
            bot: bool,
            joined_at_s: Option<i64>,
        }
        #[derive(FromRow)]
        struct Name {
            kind: String,
            name: String,
            first_seen_ms: i64,
            last_seen_ms: i64,
        }
        #[derive(FromRow)]
        struct Span {
            first_message_s: Option<i64>,
            last_message_s: Option<i64>,
        }
        let user_id = invocation
            .option("user")
            .and_then(snowflake)
            .ok_or_else(|| UserError("Pick a user.".into()))?;
        let guild_id = invocation.guild_id;

        let person: Option<Person> = sqlx::query_as(
            r#"
            SELECT username, global_name, nick, bot,
                   extract(epoch FROM joined_at)::bigint AS joined_at_s
            FROM people WHERE guild_id = $1 AND user_id = $2
            "#,
        )
        .bind(guild_id)
        .bind(user_id)
        .fetch_optional(&self.db)
        .await?;
        let Some(person) = person else {
            return Ok(EmbedBuilder::new()
                .color(COLOR)
                .description(format!(
                    "Kiki hasn't seen <@{user_id}> post or get mentioned in this server."
                ))
                .build());
        };
        let names: Vec<Name> = sqlx::query_as(
            r#"
            SELECT kind, name,
                   (extract(epoch FROM first_seen) * 1000)::bigint AS first_seen_ms,
                   (extract(epoch FROM last_seen) * 1000)::bigint AS last_seen_ms
            FROM user_names WHERE guild_id = $1 AND user_id = $2
            ORDER BY last_seen DESC, kind
            LIMIT 15
            "#,
        )
        .bind(guild_id)
        .bind(user_id)
        .fetch_all(&self.db)
        .await?;
        let span: Span = sqlx::query_as(
            r#"
            SELECT extract(epoch FROM min(created_at))::bigint AS first_message_s,
                   extract(epoch FROM max(created_at))::bigint AS last_message_s
            FROM messages WHERE guild_id = $1 AND author_id = $2 AND deleted_at IS NULL
            "#,
        )
        .bind(guild_id)
        .bind(user_id)
        .fetch_one(&self.db)
        .await?;
        let stats = self.user_stats(guild_id, Some(user_id), &[0, 1, 2]).await?;
        let window = |i: i32| stats.iter().find(|s| s.window == i);
        let row = |label: &str, cell: &dyn Fn(&UserStats) -> String| -> Vec<String> {
            std::iter::once(label.to_owned())
                .chain((0..3).map(|i| window(i).map_or("0".to_owned(), cell)))
                .collect()
        };
        let activity = [
            row("Messages", &|s| s.messages.to_string()),
            row("With Kiki", &|s| s.kiki_messages.to_string()),
            row("Conversations", &|s| s.conversations.to_string()),
            row("Cost", &|s| usd(s.cost)),
        ];

        let title = person
            .nick
            .as_deref()
            .or(person.global_name.as_deref())
            .or(person.username.as_deref())
            .unwrap_or("Unknown user");
        let mut description = format!("<@{user_id}> · `{user_id}`");
        if person.bot {
            description.push_str(" · bot");
        }
        let name_rows: Vec<Vec<String>> = names
            .iter()
            .map(|n| {
                vec![
                    match n.kind.as_str() {
                        "global_name" => "display".to_owned(),
                        other => other.to_owned(),
                    },
                    n.name.clone(),
                    ago(n.first_seen_ms),
                    ago(n.last_seen_ms),
                ]
            })
            .collect();
        let at = |s: Option<i64>, style: char| {
            s.map_or("–".to_owned(), |s| format!("<t:{s}:{style}>"))
        };

        let mut embed = EmbedBuilder::new()
            .color(COLOR)
            .title(title)
            .description(description)
            .field(EmbedFieldBuilder::new(
                "Activity",
                table(&WINDOW_COLUMNS, &activity),
            ))
            .field(EmbedFieldBuilder::new("Joined", at(person.joined_at_s, 'D')).inline())
            .field(EmbedFieldBuilder::new("First message", at(span.first_message_s, 'R')).inline())
            .field(EmbedFieldBuilder::new("Last message", at(span.last_message_s, 'R')).inline())
            .field(EmbedFieldBuilder::new(
                "Names seen",
                truncate(
                    &table(
                        &[
                            ("Kind", Align::Left),
                            ("Name", Align::Left),
                            ("First", Align::Right),
                            ("Last", Align::Right),
                        ],
                        &name_rows,
                    ),
                    MAX_FIELD_CHARS,
                ),
            ))
            .footer(EmbedFooterBuilder::new(COST_NOTE));
        if let Some(url) = avatar_url(invocation.resolved, user_id)
            && let Ok(source) = ImageSource::url(url)
        {
            embed = embed.thumbnail(source);
        }
        Ok(embed.build())
    }

    /// `/kiki users [period] [sort]`: everyone active in the period, ranked.
    async fn users(&self, invocation: &Invocation<'_>) -> anyhow::Result<Embed> {
        let period = invocation
            .option("period")
            .and_then(Value::as_str)
            .unwrap_or("7d");
        let window = WINDOWS
            .iter()
            .position(|(key, _)| *key == period)
            .ok_or_else(|| UserError(format!("Unknown period `{period}`.")))?;
        let sort = invocation
            .option("sort")
            .and_then(Value::as_str)
            .unwrap_or("cost");
        let mut stats = self
            .user_stats(invocation.guild_id, None, &[window as i32])
            .await?;
        // Ties fall back to messages, then to user id so the order is stable.
        stats.sort_by(|a, b| {
            let primary = match sort {
                "messages" => b.messages.cmp(&a.messages),
                "conversations" => b.conversations.cmp(&a.conversations),
                _ => b.cost.total_cmp(&a.cost),
            };
            primary
                .then(b.messages.cmp(&a.messages))
                .then(a.user_id.cmp(&b.user_id))
        });

        let label = WINDOWS[window].1;
        let embed = EmbedBuilder::new()
            .color(COLOR)
            .title(format!(
                "People · {}",
                if window == 2 { "all time" } else { label }
            ))
            .timestamp(now());
        if stats.is_empty() {
            return Ok(embed.description("Nobody posted in this period.").build());
        }
        let rows: Vec<Vec<String>> = stats
            .iter()
            .take(MAX_USER_ROWS)
            .enumerate()
            .map(|(i, s)| {
                vec![
                    (i + 1).to_string(),
                    truncate(s.name.as_deref().unwrap_or(&s.user_id.to_string()), 16),
                    s.messages.to_string(),
                    s.kiki_messages.to_string(),
                    s.conversations.to_string(),
                    usd(s.cost),
                ]
            })
            .collect();
        let mut description = table(
            &[
                ("#", Align::Right),
                ("Person", Align::Left),
                ("Msgs", Align::Right),
                ("Kiki", Align::Right),
                ("Convos", Align::Right),
                ("Cost", Align::Right),
            ],
            &rows,
        );
        if stats.len() > MAX_USER_ROWS {
            let _ = writeln!(description, "…and {} more.", stats.len() - MAX_USER_ROWS);
        }
        description.push_str(
            "**Msgs:** all messages · **Kiki:** messages in a conversation with Kiki · \
             **Convos:** conversations with Kiki",
        );
        Ok(embed
            .description(truncate(&description, MAX_DESCRIPTION_CHARS))
            .footer(EmbedFooterBuilder::new(format!(
                "{} people · sorted by {sort} · {COST_NOTE}",
                stats.len()
            )))
            .build())
    }

    /// Per-person messages, conversations and cost for each of `windows`
    /// (indexes into [`WINDOWS`]), for one person or everyone but Kiki.
    async fn user_stats(
        &self,
        guild_id: i64,
        user_id: Option<i64>,
        windows: &[i32],
    ) -> sqlx::Result<Vec<UserStats>> {
        sqlx::query_as(
            r#"
            WITH w(n, since) AS (VALUES (0, now() - interval '24 hours'),
                                        (1, now() - interval '7 days'),
                                        (2, '-infinity'::timestamptz)),
            mine AS (SELECT id, author_id, created_at FROM messages
                     WHERE guild_id = $1 AND author_id <> $2 AND deleted_at IS NULL
                       AND ($3::bigint IS NULL OR author_id = $3)),
            events AS (
                SELECT author_id AS user_id, created_at AS at, 1 AS messages, 0 AS kiki_messages,
                       NULL::bigint AS conversation_id, 0::float8 AS cost
                FROM mine
                UNION ALL
                SELECT m.author_id, m.created_at, 0, 1, cm.conversation_id, 0
                FROM mine m JOIN conversation_messages cm ON cm.message_id = m.id
                UNION ALL
                -- A reply's cost, split evenly across the messages that triggered it.
                SELECT m.author_id, coalesce(r.finished_at, r.created_at), 0, 0, NULL,
                       r.cost_usd / cardinality(r.trigger_message_ids)
                FROM agent_runs r
                CROSS JOIN unnest(r.trigger_message_ids) t(id)
                JOIN mine m ON m.id = t.id
                WHERE r.cost_usd IS NOT NULL
                UNION ALL
                -- Decide cost is stored on every message of a pool (one person's);
                -- count it once, on the newest.
                SELECT m.author_id, c.classified_at, 0, 0, NULL, c.jev_cost_usd
                FROM classifications c JOIN mine m ON m.id = c.message_id
                WHERE c.message_id = c.pool_id AND c.jev_cost_usd IS NOT NULL
            )
            SELECT s.*, coalesce(p.nick, p.global_name, p.username) AS name
            FROM (SELECT w.n AS win, e.user_id,
                         sum(e.messages)::bigint AS messages,
                         sum(e.kiki_messages)::bigint AS kiki_messages,
                         count(DISTINCT e.conversation_id) AS conversations,
                         sum(e.cost)::float8 AS cost
                  FROM w JOIN events e ON e.at > w.since
                  WHERE w.n = ANY($4)
                  GROUP BY w.n, e.user_id) s
            LEFT JOIN people p ON p.guild_id = $1 AND p.user_id = s.user_id
            "#,
        )
        .bind(guild_id)
        .bind(self.bot_user_id)
        .bind(user_id)
        .bind(windows)
        .fetch_all(&self.db)
        .await
    }
}

fn require_manage_server(invocation: &Invocation<'_>, action: &str) -> Result<i64, UserError> {
    let permissions = snowflake(&invocation.member["permissions"])
        .map(|bits| Permissions::from_bits_truncate(bits as u64))
        .unwrap_or_else(Permissions::empty);
    if !permissions.intersects(Permissions::MANAGE_GUILD | Permissions::ADMINISTRATOR) {
        return Err(UserError(format!(
            "You need Manage Server permission to {action}."
        )));
    }
    snowflake(&invocation.member["user"]["id"])
        .ok_or_else(|| UserError("Couldn't identify who is changing the setting.".into()))
}

type ConfigChange<'a> = (i64, &'a str, Option<Value>);

fn config_change<'a>(invocation: &Invocation<'a>) -> Result<Option<ConfigChange<'a>>, UserError> {
    let value = invocation.option("value");
    let reset = invocation
        .option("reset")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    if value.is_none() && !reset {
        return Ok(None);
    }
    let actor = require_manage_server(invocation, "change server settings")?;
    let key = invocation
        .option("key")
        .and_then(Value::as_str)
        .ok_or_else(|| UserError("Choose a key to change or reset.".into()))?;
    let spec = guild_settings::spec(key).map_err(|e| UserError(e.to_string()))?;
    if reset && value.is_some() {
        return Err(UserError("Use either value or reset, not both.".into()));
    }
    let value = if reset {
        None
    } else {
        Some(
            spec.parse(
                value
                    .and_then(Value::as_str)
                    .ok_or_else(|| UserError("Use a text value.".into()))?,
            )
            .map_err(|e| UserError(e.to_string()))?,
        )
    };
    Ok(Some((actor, key, value)))
}

async fn check_catalog_model(key: &str, model: &str) -> anyhow::Result<()> {
    let catalog: Value = reqwest::Client::new()
                    .get(format!("{}/v1/models", crate::config::openrouter_base()))
                    .timeout(Duration::from_secs(10))
                    .send().await
                    .and_then(reqwest::Response::error_for_status)
                    .map_err(|_| UserError("Couldn't check OpenRouter's model catalog. Try again; your setting has not changed.".into()))?
                    .json().await
                    .map_err(|_| UserError("Couldn't read OpenRouter's model catalog. Try again; your setting has not changed.".into()))?;
    validate_catalog_model(&catalog, key, model)?;
    Ok(())
}

fn config_embed(
    settings: &guild_settings::Resolved,
    defaults: &guild_settings::GuildSettings,
    key: Option<&str>,
    global: &str,
) -> Embed {
    let values = serde_json::to_value(&settings.values).expect("settings serialize");
    let mut embed = EmbedBuilder::new().color(COLOR).title("Server config")
        .description(truncate(global, 1000))
        .footer(EmbedFooterBuilder::new("Use key + value to edit, or key + reset:true. New pools and jobs use the new settings."));
    if key.is_none() {
        let rows = guild_settings::SPECS
            .iter()
            .map(|s| {
                let source = if settings.overrides.contains_key(s.key) {
                    " *"
                } else {
                    ""
                };
                format!(
                    "{} = {}{}",
                    s.key,
                    truncate(&values[s.key].to_string(), 85),
                    source
                )
            })
            .collect::<Vec<_>>()
            .join("\n");
        embed = embed.description(format!(
            "{}\n* marks server overrides. Inspect a key for details.\n{}",
            truncate(global, 800),
            code_block(&rows)
        ));
    }
    for spec in guild_settings::SPECS.iter().filter(|s| key == Some(s.key)) {
        let source = if settings.ignored.iter().any(|k| k == spec.key) {
            "default (invalid override ignored)"
        } else if settings.overrides.contains_key(spec.key) {
            "server"
        } else {
            "default"
        };
        let value = format!(
            "{}\n{}\n{} · {}",
            code_block(&truncate(&values[spec.key].to_string(), 700)),
            spec.description,
            source,
            spec.bounds()
        );
        embed = embed.field(EmbedFieldBuilder::new(
            spec.key,
            truncate(&value, MAX_FIELD_CHARS),
        ));
    }
    if let Some(key) = key {
        let defaults = serde_json::to_value(defaults).expect("defaults serialize");
        embed = embed.field(EmbedFieldBuilder::new(
            "Reset value",
            code_block(&truncate(&defaults[key].to_string(), 700)),
        ));
    }
    if !settings.ignored.is_empty() {
        embed = embed.field(EmbedFieldBuilder::new(
            "Ignored overrides",
            truncate(&settings.ignored.join(", "), MAX_FIELD_CHARS),
        ));
    }
    embed.build()
}

fn validate_catalog_model(catalog: &Value, key: &str, model: &str) -> Result<(), UserError> {
    let models = catalog["data"].as_array().ok_or_else(|| {
        UserError("Couldn't read OpenRouter's model catalog. Your setting has not changed.".into())
    })?;
    let entry = models
        .iter()
        .find(|entry| entry["id"].as_str() == Some(model))
        .ok_or_else(|| {
            UserError(
                "That model ID isn't in OpenRouter's catalog. Check the ID and try again.".into(),
            )
        })?;
    let outputs = |modality: &str| {
        entry["architecture"]["output_modalities"]
            .as_array()
            .is_some_and(|values| values.iter().any(|v| v == modality))
    };
    if key == "image_model" {
        if !outputs("image") {
            return Err(UserError(
                "Choose a model that outputs images, such as google/gemini-2.5-flash-image.".into(),
            ));
        }
        return Ok(());
    }
    let tools = entry["supported_parameters"]
        .as_array()
        .is_some_and(|values| values.iter().any(|v| v == "tools"));
    if !outputs("text") || !tools {
        return Err(UserError(
            "Choose a text model that supports tools; Kiki needs tools to search channel history."
                .into(),
        ));
    }
    Ok(())
}

fn definition() -> CommandBuilder {
    CommandBuilder::new("kiki", "Inspect Kiki", CommandType::ChatInput)
        .default_member_permissions(Permissions::MANAGE_GUILD)
        .option(SubCommandBuilder::new(
            "conversations",
            "Active conversations: channel, topic, people, messages and cost",
        ))
        .option(SubCommandBuilder::new(
            "clear",
            "Close every active conversation in this server",
        ))
        .option(SubCommandBuilder::new(
            "stats",
            "Activity, routing and LLM cost over 24 hours, 7 days and all time",
        ))
        .option(
            SubCommandBuilder::new("runs", "Latest agent runs, with timing, cost and errors")
                .option(
                    StringBuilder::new("status", "Only runs with this status")
                        .choices(RUN_STATUSES.map(|s| (s, s))),
                )
                .option(
                    IntegerBuilder::new("limit", "How many runs to show (default 10)")
                        .min_value(1)
                        .max_value(25),
                ),
        )
        .option(
            SubCommandBuilder::new("config", "View or change this server's settings")
                .option(
                    StringBuilder::new(
                        "key",
                        "Setting to inspect, change or reset; see /kiki config",
                    )
                    .max_length(100),
                )
                .option(
                    StringBuilder::new(
                        "value",
                        "New value; durations accept ms, s, m, h; empty keywords disables triggers",
                    )
                    .max_length(1000),
                )
                .option(BooleanBuilder::new(
                    "reset",
                    "Reset the selected key to its current default",
                )),
        )
        .option(
            SubCommandBuilder::new("users", "Messages, conversations and LLM cost per person")
                .option(
                    StringBuilder::new("period", "Time period (default 7 days)")
                        .choices(WINDOWS.map(|(key, label)| (label, key))),
                )
                .option(
                    StringBuilder::new("sort", "Rank by (default cost)").choices([
                        ("Cost", "cost"),
                        ("Messages", "messages"),
                        ("Conversations", "conversations"),
                    ]),
                ),
        )
        .option(
            SubCommandBuilder::new("whois", "Someone's names, messages, conversations and cost")
                .option(UserBuilder::new("user", "Who to look up").required(true)),
        )
}

fn error_embed(message: &str) -> Embed {
    EmbedBuilder::new()
        .color(ERROR_COLOR)
        .description(message)
        .build()
}

fn avatar_url(resolved: &Value, user_id: i64) -> Option<String> {
    let user = &resolved["users"][user_id.to_string()];
    let hash = user["avatar"].as_str()?;
    let ext = if hash.starts_with("a_") { "gif" } else { "png" };
    Some(format!(
        "https://cdn.discordapp.com/avatars/{user_id}/{hash}.{ext}?size=128"
    ))
}

fn now() -> Timestamp {
    Timestamp::from_secs(unix_millis() / 1000).expect("current time is a valid timestamp")
}

#[derive(Clone, Copy)]
enum Align {
    Left,
    Right,
}

fn table(columns: &[(&str, Align)], rows: &[Vec<String>]) -> String {
    const GAP: &str = "  ";
    let cell = |s: &str| s.replace('`', "ˋ").replace('\n', " ");
    let rows: Vec<Vec<String>> = rows
        .iter()
        .map(|r| r.iter().map(|c| cell(c)).collect())
        .collect();
    let widths: Vec<usize> = columns
        .iter()
        .enumerate()
        .map(|(i, (header, _))| {
            rows.iter()
                .filter_map(|r| r.get(i))
                .map(|c| c.chars().count())
                .chain([header.chars().count()])
                .max()
                .unwrap_or(0)
        })
        .collect();
    let line = |cells: &mut dyn Iterator<Item = &str>| {
        let mut s = String::new();
        for (i, c) in cells.enumerate() {
            let (width, align) = (widths[i], columns[i].1);
            if i > 0 {
                s.push_str(GAP);
            }
            let _ = match align {
                Align::Left => write!(s, "{c:<width$}"),
                Align::Right => write!(s, "{c:>width$}"),
            };
        }
        s.trim_end().to_owned()
    };
    let mut out = String::from("```\n");
    out.push_str(&line(&mut columns.iter().map(|(h, _)| *h)));
    out.push('\n');
    for r in &rows {
        out.push_str(&line(&mut r.iter().map(String::as_str)));
        out.push('\n');
    }
    out.push_str("```\n");
    out
}

fn code_block(text: &str) -> String {
    format!("```\n{}\n```", text.replace("```", "ˋˋˋ"))
}

fn channel_label(name: Option<&str>, id: i64) -> String {
    match name {
        Some(name) => truncate(&format!("#{name}"), 16),
        None => id.to_string(),
    }
}

fn usd(amount: f64) -> String {
    if amount > 0.0 && amount < 0.01 {
        format!("${amount:.4}")
    } else {
        format!("${amount:.2}")
    }
}

fn seconds(ms: Option<f64>) -> String {
    match ms {
        None => "–".to_owned(),
        Some(ms) if ms < 10_000.0 => format!("{:.1}s", ms / 1000.0),
        Some(ms) => format!("{:.0}s", ms / 1000.0),
    }
}

fn duration(d: Duration) -> String {
    let ms = d.as_millis();
    match ms {
        _ if ms < 1000 || !ms.is_multiple_of(1000) => format!("{ms} ms"),
        _ if ms.is_multiple_of(3_600_000) => format!("{} h", ms / 3_600_000),
        _ if ms.is_multiple_of(60_000) => format!("{} min", ms / 60_000),
        _ => format!("{} s", ms / 1000),
    }
}

fn ago(unix_ms: i64) -> String {
    let secs = (unix_millis() - unix_ms).max(0) / 1000;
    match secs {
        0..60 => format!("{secs}s"),
        60..3600 => format!("{}m", secs / 60),
        3600..86400 => format!("{}h", secs / 3600),
        _ => format!("{}d", secs / 86400),
    }
}

fn truncate(text: &str, max: usize) -> String {
    if text.chars().count() <= max {
        return text.to_owned();
    }
    let mut s: String = text.chars().take(max - 1).collect();
    s.push('…');
    s
}

#[cfg(test)]
mod tests {
    use std::time::Duration;

    use super::{Align, definition, duration, seconds, table, truncate, usd};

    #[test]
    fn formats_cost() {
        assert_eq!(usd(0.0), "$0.00");
        assert_eq!(usd(0.000_123_4), "$0.0001");
        assert_eq!(usd(1.234), "$1.23");
    }

    #[test]
    fn formats_durations() {
        assert_eq!(duration(Duration::from_millis(500)), "500 ms");
        assert_eq!(duration(Duration::from_millis(1500)), "1500 ms");
        assert_eq!(duration(Duration::from_secs(8)), "8 s");
        assert_eq!(duration(Duration::from_secs(120)), "2 min");
        assert_eq!(duration(Duration::from_secs(3600)), "1 h");
        assert_eq!(seconds(Some(4_210.0)), "4.2s");
        assert_eq!(seconds(Some(12_400.0)), "12s");
        assert_eq!(seconds(None), "–");
    }

    #[test]
    fn aligns_tables() {
        let rows = vec![
            vec!["Chat".to_owned(), "$1.20".to_owned()],
            vec!["Decide".to_owned(), "$0.05".to_owned()],
        ];
        assert_eq!(
            table(&[("", Align::Left), ("Cost", Align::Right)], &rows),
            "```\n         Cost\nChat    $1.20\nDecide  $0.05\n```\n"
        );
    }

    #[test]
    fn table_cells_cannot_close_the_code_block() {
        let rows = vec![vec!["a```b".to_owned()]];
        assert!(!table(&[("x", Align::Left)], &rows).contains("a```"));
    }

    #[test]
    fn truncates_on_char_boundaries() {
        assert_eq!(truncate("héllo", 5), "héllo");
        assert_eq!(truncate("héllo", 3), "hé…");
    }

    #[test]
    fn command_is_valid() {
        let command = definition().validate().unwrap().build();
        assert!(!command.options.iter().any(|option| option.name == "model"));
        let config = command
            .options
            .iter()
            .find(|option| option.name == "config")
            .unwrap();
        let key = config
            .options
            .as_ref()
            .unwrap()
            .iter()
            .find(|option| option.name == "key")
            .unwrap();
        assert!(
            key.choices
                .as_ref()
                .is_none_or(|choices| choices.is_empty())
        );
    }

    #[test]
    fn config_writes_require_manage_server_and_validate_option_combinations() {
        use super::{Invocation, Permissions, config_change};
        use serde_json::{Value, json};
        fn invoke<'a>(member: &'a Value, options: &'a Value) -> Invocation<'a> {
            Invocation {
                guild_id: 1,
                member,
                subcommand: "config",
                options,
                resolved: &Value::Null,
            }
        }
        let denied = json!({"user": {"id": "42"}, "permissions": "0"});
        let allowed = json!({"user": {"id": "42"}, "permissions": Permissions::MANAGE_GUILD.bits().to_string()});
        let options =
            json!([{"name": "key", "value": "pool_debounce"}, {"name": "value", "value": "2s"}]);
        assert!(config_change(&invoke(&denied, &options)).is_err());
        assert_eq!(
            config_change(&invoke(&allowed, &options)).unwrap(),
            Some((42, "pool_debounce", Some(json!(2000))))
        );
        let model = json!([{"name": "key", "value": "chat_model"}, {"name": "value", "value": "test/model"}]);
        assert!(config_change(&invoke(&denied, &model)).is_err());
        for permissions in [Permissions::MANAGE_GUILD, Permissions::ADMINISTRATOR] {
            let member =
                json!({"user": {"id": "42"}, "permissions": permissions.bits().to_string()});
            assert_eq!(
                config_change(&invoke(&member, &model)).unwrap(),
                Some((42, "chat_model", Some(json!("test/model"))))
            );
        }
        for value in [
            "bad",
            "test/",
            "/model",
            "test/model with spaces",
            "test/`model",
            "",
            "default",
        ] {
            let options =
                json!([{"name": "key", "value": "chat_model"}, {"name": "value", "value": value}]);
            assert!(config_change(&invoke(&allowed, &options)).is_err());
        }
        let read = json!([{"name": "key", "value": "chat_model"}]);
        assert!(config_change(&invoke(&denied, &read)).unwrap().is_none());
        let reset =
            json!([{"name": "key", "value": "chat_model"}, {"name": "reset", "value": true}]);
        assert!(config_change(&invoke(&denied, &reset)).is_err());
        assert_eq!(
            config_change(&invoke(&allowed, &reset)).unwrap(),
            Some((42, "chat_model", None))
        );
        for options in [
            json!([{"name": "value", "value": "10"}]),
            json!([{"name": "reset", "value": true}]),
            json!([{"name": "key", "value": "history_messages"}, {"name": "value", "value": "191"}]),
            json!([{"name": "key", "value": "unknown"}, {"name": "value", "value": "1"}]),
            json!([{"name": "key", "value": "chat_model"}, {"name": "reset", "value": true}, {"name": "value", "value": "test/model"}]),
        ] {
            assert!(config_change(&invoke(&allowed, &options)).is_err());
        }
    }

    #[test]
    fn config_display_fits_discord_limits_with_maximum_length_values() {
        use super::config_embed;
        use crate::guild_settings::{Resolved, SPECS, tests::defaults};
        let mut values = defaults();
        values.chat_model = format!("test/{}", "m".repeat(195));
        values.keywords = vec!["a".repeat(32); 20];
        let encoded = serde_json::to_value(&values).unwrap();
        let settings = Resolved {
            overrides: SPECS
                .iter()
                .map(|s| (s.key.to_owned(), encoded[s.key].clone()))
                .collect(),
            values: values.clone(),
            ignored: vec!["unknown".into()],
        };
        for key in [None, Some("keywords"), Some("chat_model")] {
            let embed = config_embed(&settings, &values, key, &"x".repeat(1000));
            assert!(embed.fields.len() <= 25);
            assert!(
                embed
                    .description
                    .as_ref()
                    .is_none_or(|d| d.chars().count() <= 4096)
            );
            assert!(
                embed
                    .fields
                    .iter()
                    .all(|f| f.name.chars().count() <= 256 && f.value.chars().count() <= 1024)
            );
            let size = embed.title.as_deref().unwrap_or_default().chars().count()
                + embed
                    .description
                    .as_deref()
                    .unwrap_or_default()
                    .chars()
                    .count()
                + embed.footer.as_ref().map_or(0, |f| f.text.chars().count())
                + embed
                    .fields
                    .iter()
                    .map(|f| f.name.chars().count() + f.value.chars().count())
                    .sum::<usize>();
            assert!(size <= 6000, "embed has {size} characters");
        }
    }

    #[test]
    fn model_catalog_rejects_unknown_and_incompatible_models() {
        use super::validate_catalog_model;
        use serde_json::json;
        let catalog = json!({"data": [
            {"id": "test/chat", "architecture": {"output_modalities": ["text"]}, "supported_parameters": ["tools"]},
            {"id": "test/no-tools", "architecture": {"output_modalities": ["text"]}, "supported_parameters": []},
            {"id": "test/image", "architecture": {"output_modalities": ["image"]}, "supported_parameters": ["tools"]}
        ]});
        assert!(validate_catalog_model(&catalog, "chat_model", "test/chat").is_ok());
        for model in ["test/unknown", "test/no-tools", "test/image"] {
            assert!(validate_catalog_model(&catalog, "chat_model", model).is_err());
        }
        assert!(validate_catalog_model(&json!({}), "chat_model", "test/chat").is_err());
        assert!(validate_catalog_model(&catalog, "image_model", "test/image").is_ok());
        for model in ["test/unknown", "test/chat"] {
            assert!(validate_catalog_model(&catalog, "image_model", model).is_err());
        }
    }
}
