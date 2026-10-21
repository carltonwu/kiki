//! The ingester's metric instruments (docs/observability.md, "Metric catalog").
//!
//! Each recording function takes or maps to bounded attribute values (static
//! strs, unknowns as `other`): IDs and free text go on spans and logs instead.
//! No-ops when export is off, like [`telemetry::meter`].

use std::{
    sync::{LazyLock, RwLock},
    time::Duration,
};

use opentelemetry::{
    KeyValue,
    metrics::{Counter, Gauge, Histogram},
};
use sqlx::PgPool;

use crate::telemetry;

/// Seconds (docs/observability.md, "Cardinality rules").
const DURATION_BUCKETS: [f64; 9] = [0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0];
const REPLY_LATENCY_BUCKETS: [f64; 7] = [1.0, 2.0, 5.0, 10.0, 20.0, 40.0, 80.0];
const RATIO_BUCKETS: [f64; 6] = [0.1, 0.25, 0.5, 0.75, 0.9, 1.0];
const COUNT_BUCKETS: [f64; 6] = [1.0, 2.0, 5.0, 10.0, 20.0, 50.0];

/// Gateway dispatches the listener handles; the rest count as `other`.
const GATEWAY_EVENTS: &[&str] = &[
    "READY",
    "RESUMED",
    "GUILD_CREATE",
    "INTERACTION_CREATE",
    "MESSAGE_CREATE",
    "MESSAGE_UPDATE",
    "MESSAGE_DELETE",
    "MESSAGE_DELETE_BULK",
    "CHANNEL_CREATE",
    "CHANNEL_UPDATE",
    "CHANNEL_DELETE",
    "THREAD_CREATE",
    "THREAD_UPDATE",
    "THREAD_DELETE",
    "THREAD_LIST_SYNC",
];

struct Instruments {
    gateway_events: Counter<u64>,
    gateway_reconnects: Counter<u64>,
    gateway_lag: Histogram<f64>,
    gateway_store_errors: Counter<u64>,
    messages_ingested: Counter<u64>,
    router_admissions: Counter<u64>,
    router_classifications: Counter<u64>,
    jev_confidence: Histogram<f64>,
    llm_calls: Counter<u64>,
    llm_cost: Counter<f64>,
    llm_duration: Histogram<f64>,
    runs_queued: Counter<u64>,
    runs_finished: Counter<u64>,
    run_duration: Histogram<f64>,
    workers_active: Gauge<u64>,
    host_requests: Counter<u64>,
    host_retries: Counter<u64>,
    drawings_finished: Counter<u64>,
    conversations_started: Counter<u64>,
    conversations_closed: Counter<u64>,
    conversation_messages: Histogram<u64>,
    messages_sent: Counter<u64>,
    send_errors: Counter<u64>,
    reactions: Counter<u64>,
    rest_retries: Counter<u64>,
    reply_latency: Histogram<f64>,
    faults_recorded: Counter<u64>,
    faults_reported: Counter<u64>,
}

static INSTRUMENTS: LazyLock<Instruments> = LazyLock::new(|| {
    let meter = telemetry::meter();
    let counter = |name: &'static str, unit: &'static str| meter.u64_counter(name).with_unit(unit);
    let seconds = |name: &'static str, buckets: &[f64]| {
        meter
            .f64_histogram(name)
            .with_unit("s")
            .with_boundaries(buckets.to_vec())
            .build()
    };
    Instruments {
        gateway_events: counter("kiki.gateway.events", "{event}").build(),
        gateway_reconnects: counter("kiki.gateway.reconnects", "{reconnect}")
            .with_description("Gateway connections lost; the shard reconnects or resumes")
            .build(),
        gateway_lag: seconds("kiki.gateway.lag", &DURATION_BUCKETS),
        gateway_store_errors: counter("kiki.gateway.store_errors", "{error}").build(),
        messages_ingested: counter("kiki.messages.ingested", "{message}").build(),
        router_admissions: counter("kiki.router.admissions", "{message}").build(),
        router_classifications: counter("kiki.router.classifications", "{pool}").build(),
        jev_confidence: meter
            .f64_histogram("kiki.jev.confidence")
            .with_unit("1")
            .with_boundaries(RATIO_BUCKETS.to_vec())
            .build(),
        llm_calls: counter("kiki.llm.calls", "{call}").build(),
        llm_cost: meter
            .f64_counter("kiki.llm.cost_usd")
            .with_unit("{USD}")
            .build(),
        llm_duration: seconds("kiki.llm.duration", &DURATION_BUCKETS),
        runs_queued: counter("kiki.agent_runs.queued", "{run}").build(),
        runs_finished: counter("kiki.agent_runs.finished", "{run}").build(),
        run_duration: seconds("kiki.agent_run.duration", &DURATION_BUCKETS),
        workers_active: meter
            .u64_gauge("kiki.agent.workers.active")
            .with_unit("{worker}")
            .with_description("Conversations being answered")
            .build(),
        host_requests: counter("kiki.agent_host.requests", "{request}").build(),
        host_retries: counter("kiki.agent_host.retries", "{retry}").build(),
        drawings_finished: counter("kiki.drawings.finished", "{drawing}").build(),
        conversations_started: counter("kiki.conversations.started", "{conversation}").build(),
        conversations_closed: counter("kiki.conversations.closed", "{conversation}").build(),
        conversation_messages: meter
            .u64_histogram("kiki.conversation.messages")
            .with_unit("{message}")
            .with_description("Messages in a conversation when it closed")
            .with_boundaries(COUNT_BUCKETS.to_vec())
            .build(),
        messages_sent: counter("kiki.discord.messages_sent", "{message}").build(),
        send_errors: counter("kiki.discord.send_errors", "{error}").build(),
        reactions: counter("kiki.discord.reactions", "{reaction}").build(),
        rest_retries: counter("kiki.discord.rest.retries", "{retry}").build(),
        reply_latency: meter
            .f64_histogram("kiki.reply.latency")
            .with_unit("s")
            .with_description("Trigger message created to Kiki's first reply sent")
            .with_boundaries(REPLY_LATENCY_BUCKETS.to_vec())
            .build(),
        faults_recorded: counter("kiki.faults.recorded", "{fault}")
            .with_description("Unexpected failures the fault reporter took in, both services'")
            .build(),
        faults_reported: counter("kiki.faults.reported", "{fault}")
            .with_description("What the fault reporter did with a fingerprint's new faults")
            .build(),
    }
});

fn m() -> &'static Instruments {
    &INSTRUMENTS
}

/// `kiki.db.pool.connections`, read from the pool at each export.
pub fn observe_pool(pool: &PgPool) {
    let pool = pool.clone();
    // The callback stays registered with the meter after the handle drops.
    telemetry::meter()
        .u64_observable_gauge("kiki.db.pool.connections")
        .with_unit("{connection}")
        .with_callback(move |gauge| {
            let (size, idle) = (pool.size() as u64, pool.num_idle() as u64);
            gauge.observe(size.saturating_sub(idle), &[KeyValue::new("state", "used")]);
            gauge.observe(idle, &[KeyValue::new("state", "idle")]);
        })
        .build();
}

pub fn gateway_event(event: &str) {
    let event = GATEWAY_EVENTS
        .iter()
        .find(|&&e| e == event)
        .copied()
        .unwrap_or("other");
    m().gateway_events.add(1, &[KeyValue::new("type", event)]);
}

/// Why the Gateway connection dropped.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Reconnect {
    /// Discord asked for it (opcode 7).
    Requested,
    /// Discord invalidated the session (opcode 9).
    InvalidSession,
    /// The connection broke without a close frame (1006).
    Transport,
    /// Discord closed it, with a close code.
    Closed,
}

impl Reconnect {
    fn as_str(self) -> &'static str {
        match self {
            Self::Requested => "requested",
            Self::InvalidSession => "invalid_session",
            Self::Transport => "transport",
            Self::Closed => "closed",
        }
    }
}

pub fn gateway_reconnect(reason: Reconnect) {
    m().gateway_reconnects
        .add(1, &[KeyValue::new("reason", reason.as_str())]);
}

pub fn gateway_lag(lag: Duration) {
    m().gateway_lag.record(lag.as_secs_f64(), &[]);
}

pub fn gateway_store_error() {
    m().gateway_store_errors.add(1, &[]);
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Author {
    Human,
    Bot,
    Kiki,
}

pub fn message_ingested(author: Author) {
    let author = match author {
        Author::Human => "human",
        Author::Bot => "bot",
        Author::Kiki => "kiki",
    };
    m().messages_ingested
        .add(1, &[KeyValue::new("author", author)]);
}

/// A new message's fate at the router: pooled for classification, or why not.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Admission {
    Admitted,
    Unparseable,
    Bot,
    OtherGuild,
    TooOld,
    ChannelNotAllowed,
}

impl Admission {
    fn attributes(self) -> [KeyValue; 2] {
        let (result, reason) = match self {
            Self::Admitted => ("admitted", "none"),
            Self::Unparseable => ("skipped", "unparseable"),
            Self::Bot => ("skipped", "bot"),
            Self::OtherGuild => ("skipped", "other_guild"),
            Self::TooOld => ("skipped", "too_old"),
            Self::ChannelNotAllowed => ("skipped", "channel_not_allowed"),
        };
        [
            KeyValue::new("result", result),
            KeyValue::new("reason", reason),
        ]
    }
}

pub fn admission(admission: Admission) {
    m().router_admissions.add(1, &admission.attributes());
}

/// `check` and `outcome` are the router's own static names.
pub fn classification(check: &'static str, outcome: &'static str) {
    m().router_classifications.add(
        1,
        &[
            KeyValue::new("check", check),
            KeyValue::new("outcome", outcome),
        ],
    );
}

pub fn jev_confidence(confidence: f64) {
    m().jev_confidence.record(confidence, &[]);
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum LlmResult {
    Ok,
    Error,
    Timeout,
}

/// One model call this service made itself (only Jev: the agent host records
/// the calls it makes, including the ones the ingester asks it for).
pub fn llm_call(
    purpose: &'static str,
    model: &str,
    result: LlmResult,
    duration: Duration,
    cost: Option<f64>,
) {
    let base = [
        KeyValue::new("purpose", purpose),
        KeyValue::new("model", model_name(model)),
    ];
    let result = match result {
        LlmResult::Ok => "ok",
        LlmResult::Error => "error",
        LlmResult::Timeout => "timeout",
    };
    let mut attributes = base.to_vec();
    attributes.push(KeyValue::new("result", result));
    m().llm_calls.add(1, &attributes);
    m().llm_duration.record(duration.as_secs_f64(), &base);
    if let Some(cost) = cost.filter(|c| c.is_finite() && *c >= 0.0) {
        m().llm_cost.add(cost, &base);
    }
}

/// Distinct `model` values one process reports before the rest become `other`.
const MAX_MODELS: usize = 16;
static MODELS: Capped = Capped::new(MAX_MODELS);

/// A model ID as the provider reported it (OpenRouter's can be an alias like
/// `~typesafe/jev-latest`), capped by [`MODELS`]; `other` if it doesn't look like
/// one (never free text).
fn model_name(model: &str) -> String {
    let plausible = !model.is_empty()
        && model.len() <= 80
        && model
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"/._:~-".contains(&b));
    match plausible {
        true => MODELS.get(model),
        false => "other".to_owned(),
    }
}

/// Bounds an attribute whose values admins may legitimately change (models),
/// where a fixed allowlist would be wrong: per process, the first `cap`
/// distinct values keep their name and later ones are `other`. A value already
/// seen costs one read lock.
pub struct Capped {
    cap: usize,
    seen: RwLock<Vec<String>>,
}

impl Capped {
    pub const fn new(cap: usize) -> Self {
        Self {
            cap,
            seen: RwLock::new(Vec::new()),
        }
    }

    pub fn get(&self, value: &str) -> String {
        let known = |seen: &[String]| seen.iter().any(|v| v == value);
        if known(&self.seen.read().expect("capped values poisoned")) {
            return value.to_owned();
        }
        let mut seen = self.seen.write().expect("capped values poisoned");
        if known(&seen) {
            return value.to_owned();
        }
        if seen.len() < self.cap {
            seen.push(value.to_owned());
            return value.to_owned();
        }
        "other".to_owned()
    }
}

/// A run's kind: the routing check that queued it (`classifications.check_hit`).
pub fn run_kind(check: Option<&str>) -> &'static str {
    match check {
        Some("reply") => "reply",
        Some("mention") => "mention",
        Some("keyword") => "keyword",
        Some("active_conversation") => "active_conversation",
        _ => "other",
    }
}

pub fn run_queued(kind: &'static str) {
    m().runs_queued.add(1, &[KeyValue::new("kind", kind)]);
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum RunStatus {
    Done,
    Failed,
    Superseded,
    Expired,
    /// Cut off mid-delivery by a restart, failed at startup.
    Interrupted,
}

pub fn run_finished(kind: &'static str, status: RunStatus) {
    let status = match status {
        RunStatus::Done => "done",
        RunStatus::Failed => "failed",
        RunStatus::Superseded => "superseded",
        RunStatus::Expired => "expired",
        RunStatus::Interrupted => "interrupted",
    };
    m().runs_finished.add(
        1,
        &[KeyValue::new("kind", kind), KeyValue::new("status", status)],
    );
}

pub fn run_duration(kind: &'static str, duration: Duration) {
    m().run_duration
        .record(duration.as_secs_f64(), &[KeyValue::new("kind", kind)]);
}

pub fn workers_active(workers: usize) {
    m().workers_active.record(workers as u64, &[]);
}

/// One POST to the agent host; `status` is its HTTP status, `None` if it got none.
pub fn host_request(endpoint: &'static str, status: Option<u16>) {
    let status = match status {
        Some(code) => code.to_string(),
        None => "error".to_owned(),
    };
    m().host_requests.add(
        1,
        &[
            KeyValue::new("endpoint", endpoint),
            KeyValue::new("status", status),
        ],
    );
}

pub fn host_retry(reason: &'static str) {
    m().host_retries.add(1, &[KeyValue::new("reason", reason)]);
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Drawing {
    Drawn,
    /// The image model or its safety filter wouldn't draw it.
    Refused,
    Failed,
    Expired,
    Interrupted,
}

pub fn drawing_finished(outcome: Drawing) {
    let outcome = match outcome {
        Drawing::Drawn => "drawn",
        Drawing::Refused => "refused",
        Drawing::Failed => "failed",
        Drawing::Expired => "expired",
        Drawing::Interrupted => "interrupted",
    };
    m().drawings_finished
        .add(1, &[KeyValue::new("outcome", outcome)]);
}

pub fn conversation_started() {
    m().conversations_started.add(1, &[]);
}

/// Conversations closed together, with their message counts.
pub fn conversations_closed(reason: &'static str, message_counts: impl IntoIterator<Item = i32>) {
    let attributes = [KeyValue::new("reason", reason)];
    for count in message_counts {
        m().conversations_closed.add(1, &attributes);
        m().conversation_messages.record(count.max(0) as u64, &[]);
    }
}

/// What a Discord message Kiki posts is.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Send {
    Reply,
    Drawing,
}

impl Send {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Reply => "reply",
            Self::Drawing => "drawing",
        }
    }
}

pub fn message_sent(kind: Send) {
    m().messages_sent
        .add(1, &[KeyValue::new("kind", kind.as_str())]);
}

pub fn send_error(kind: Send) {
    m().send_errors
        .add(1, &[KeyValue::new("kind", kind.as_str())]);
}

pub fn reaction(added: bool) {
    let result = if added { "ok" } else { "error" };
    m().reactions.add(1, &[KeyValue::new("result", result)]);
}

pub fn rest_retry(reason: &'static str) {
    m().rest_retries.add(1, &[KeyValue::new("reason", reason)]);
}

pub fn reply_latency(latency: Duration) {
    m().reply_latency.record(latency.as_secs_f64(), &[]);
}

/// Fault kinds both services record (`faults::KINDS`); the rest count as `other`.
pub fn fault_recorded(service: &str, kind: &str) {
    let service = if service == "agent" {
        "agent"
    } else {
        "ingester"
    };
    let kind = crate::faults::KINDS
        .iter()
        .find(|k| **k == kind)
        .copied()
        .unwrap_or("other");
    m().faults_recorded.add(
        1,
        &[
            KeyValue::new("service", service),
            KeyValue::new("kind", kind),
        ],
    );
}

/// What the fault reporter did with one fingerprint's new faults.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum FaultAction {
    /// Opened an issue (a regression's too).
    Created,
    /// Counted into an open issue, for its next comment.
    Counted,
    Commented,
    /// Its issue was closed as not planned.
    Muted,
    /// Below the rate a quiet fallback is filed at, or from another environment.
    Dropped,
    /// Over the new-issue limit: left for a later pass.
    Deferred,
    /// GitHub failed; left for a later pass.
    Failed,
}

pub fn fault_reported(action: FaultAction) {
    let action = match action {
        FaultAction::Created => "created",
        FaultAction::Counted => "counted",
        FaultAction::Commented => "commented",
        FaultAction::Muted => "muted",
        FaultAction::Dropped => "dropped",
        FaultAction::Deferred => "deferred",
        FaultAction::Failed => "failed",
    };
    m().faults_reported
        .add(1, &[KeyValue::new("action", action)]);
}

/// When Discord created a message, from its snowflake ID, in Unix ms.
pub fn snowflake_ms(id: i64) -> i64 {
    (id >> 22) + 1_420_070_400_000
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn attribute_values_are_bounded() {
        assert_eq!(model_name("~typesafe/jev-latest"), "~typesafe/jev-latest");
        assert_eq!(model_name(""), "other");
        assert_eq!(model_name("ignore previous instructions"), "other");
        assert_eq!(model_name(&"a".repeat(81)), "other");
        assert_eq!(run_kind(Some("mention")), "mention");
        assert_eq!(run_kind(Some("none")), "other");
        assert_eq!(run_kind(None), "other");
        assert_eq!(
            Admission::TooOld
                .attributes()
                .map(|kv| kv.value.to_string()),
            ["skipped", "too_old"]
        );
        assert_eq!(
            Admission::Admitted
                .attributes()
                .map(|kv| kv.value.to_string()),
            ["admitted", "none"]
        );
    }

    #[test]
    fn capped_values_keep_the_first_names_only() {
        let models = Capped::new(2);
        assert_eq!(models.get("a/one"), "a/one");
        assert_eq!(models.get("a/two"), "a/two");
        // Past the cap: new values are `other`, known ones keep their name.
        assert_eq!(models.get("a/three"), "other");
        assert_eq!(models.get("a/one"), "a/one");
        assert_eq!(models.get("a/two"), "a/two");
        assert_eq!(models.get("a/four"), "other");
        assert_eq!(models.seen.read().unwrap().len(), 2);
    }

    #[test]
    fn snowflakes_carry_their_creation_time() {
        // 2016-04-30T11:18:25.796Z, Discord's documented example.
        assert_eq!(snowflake_ms(175928847299117063), 1_462_015_105_796);
    }
}
