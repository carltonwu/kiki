//! Platform poller: Postgres statistics, Kiki's queues, and Railway's resource
//! metrics, as OpenTelemetry gauges and counters (docs/observability.md,
//! "Platform poller").
//!
//! One task, on the leader only, polls every [`INTERVAL`] into a snapshot; the
//! instruments' callbacks, which the metric reader runs on its own thread at
//! export, only read that snapshot. Nothing runs when telemetry export is off.
//! A failed source is logged (at most every [`WARN_EVERY`]) and reports no
//! samples until it recovers; a refused Railway token turns Railway off until
//! restart.

mod postgres;
mod railway;

use std::{
    collections::HashMap,
    sync::{Arc, Mutex, Weak},
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

use opentelemetry::{
    KeyValue,
    metrics::{AsyncInstrument, Meter},
};
use sqlx::PgPool;
use tokio::{sync::watch, time::MissedTickBehavior};
use tracing::{info, warn};

use self::{
    postgres::{Queues, STATES, Server},
    railway::{Railway, Usage},
};
use crate::telemetry;

const INTERVAL: Duration = Duration::from_secs(60);
/// Bounds a whole poll of one source, above the per-query timeouts.
const POLL_TIMEOUT: Duration = Duration::from_secs(15);
const WARN_EVERY: Duration = Duration::from_secs(600);

#[derive(Default)]
struct Snapshot {
    server: Option<Server>,
    /// Over the last poll interval, from `pg_stat_database`'s block counters.
    cache_hit_ratio: Option<f64>,
    queues: Queues,
    railway: Vec<Usage>,
}

type Shared = Arc<Mutex<Snapshot>>;

/// Polls until shutdown. Spawn it once this instance is the leader, so one
/// instance reports these; returns at once when telemetry export is off.
pub async fn run(db: PgPool, mut shutdown: watch::Receiver<bool>) {
    if !telemetry::exporting() {
        return;
    }
    let mut railway = match Railway::from_env(|name| std::env::var(name).ok()) {
        Ok(railway) => railway,
        Err(e) => {
            warn!(error = %e, "Railway metrics are off");
            None
        }
    };
    let snapshot = Shared::default();
    let _instruments = register(&telemetry::meter(), &snapshot);
    info!(railway = railway.is_some(), "platform poller started");

    let mut server_warn = Throttle::default();
    let mut queue_warns: HashMap<&'static str, Throttle> = HashMap::new();
    let mut railway_warn = Throttle::default();
    let mut previous: Option<Server> = None;
    let mut ticks = tokio::time::interval(INTERVAL);
    ticks.set_missed_tick_behavior(MissedTickBehavior::Delay);
    loop {
        tokio::select! {
            _ = ticks.tick() => {}
            _ = shutdown.wait_for(|stop| *stop) => return,
        }
        let poll = async {
            let (server, queues, usage) = tokio::join!(
                bounded(postgres::server(&db)),
                postgres::queues(&db),
                poll_railway(&mut railway, &mut railway_warn),
            );
            (server, queues, usage)
        };
        let (server, queues, usage) = tokio::select! {
            polled = poll => polled,
            _ = shutdown.wait_for(|stop| *stop) => return,
        };
        let (queues, failed) = queues;
        if let Err(e) = &server {
            server_warn.warn("postgres", e);
        }
        for (query, e) in failed {
            queue_warns.entry(query).or_default().warn(query, &e);
        }
        let server = server.ok();
        let ratio = server
            .as_ref()
            .zip(previous.as_ref())
            .and_then(|(now, before)| cache_hit_ratio(before, now));
        let mut snap = snapshot.lock().unwrap_or_else(|e| e.into_inner());
        snap.cache_hit_ratio = ratio;
        snap.server = server.clone();
        snap.queues = queues;
        snap.railway = usage;
        drop(snap);
        previous = server;
    }
}

async fn bounded<T>(query: impl Future<Output = sqlx::Result<T>>) -> anyhow::Result<T> {
    Ok(tokio::time::timeout(POLL_TIMEOUT, query).await??)
}

/// The latest usage, or none when Railway is off or failed this time.
async fn poll_railway(railway: &mut Option<Railway>, throttle: &mut Throttle) -> Vec<Usage> {
    let Some(client) = railway else {
        return Vec::new();
    };
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs();
    match tokio::time::timeout(POLL_TIMEOUT, client.poll(now)).await {
        Ok(Ok(usage)) => usage,
        Ok(Err(railway::Error::Unauthorized(message))) => {
            warn!(
                error = message,
                "Railway refused RAILWAY_METRICS_TOKEN; Railway metrics are off until restart"
            );
            *railway = None;
            Vec::new()
        }
        Ok(Err(railway::Error::Other(e))) => {
            throttle.warn("railway", &e);
            Vec::new()
        }
        Err(_) => {
            throttle.warn("railway", &anyhow::anyhow!("timed out"));
            Vec::new()
        }
    }
}

/// Hits over all block reads since the previous poll; `None` without reads
/// or across a stats reset.
fn cache_hit_ratio(before: &Server, now: &Server) -> Option<f64> {
    if before.stats_reset != now.stats_reset {
        return None;
    }
    let hit = now.blocks_hit.checked_sub(before.blocks_hit)?;
    let read = now.blocks_read.checked_sub(before.blocks_read)?;
    let total = hit + read;
    (total > 0).then(|| hit as f64 / total as f64)
}

/// Logs a source's failure at most every [`WARN_EVERY`], with how many were
/// left out in between.
#[derive(Default)]
struct Throttle {
    last: Option<Instant>,
    suppressed: u32,
}

impl Throttle {
    fn warn(&mut self, source: &str, error: &anyhow::Error) {
        if self.last.is_some_and(|at| at.elapsed() < WARN_EVERY) {
            self.suppressed += 1;
            return;
        }
        warn!(
            source,
            error = format!("{error:#}"),
            suppressed = self.suppressed,
            "platform poll failed"
        );
        self.last = Some(Instant::now());
        self.suppressed = 0;
    }
}

/// The instruments, held while the poller runs.
type Instruments = Vec<Box<dyn std::any::Any + Send + Sync>>;

fn register(meter: &Meter, snapshot: &Shared) -> Instruments {
    let mut all: Instruments = Vec::new();
    // Each callback reads the snapshot through a Weak: the SDK keeps callbacks
    // after their instruments drop, so once the poller stops (and drops the
    // snapshot) they observe nothing instead of repeating the last values.
    macro_rules! observe {
        ($kind:ident, $name:literal, $unit:literal, $description:literal, |$s:ident, $o:ident| $body:expr) => {{
            let shared: Weak<Mutex<Snapshot>> = Arc::downgrade(snapshot);
            all.push(Box::new(
                meter
                    .$kind($name)
                    .with_unit($unit)
                    .with_description($description)
                    .with_callback(move |$o| {
                        let Some(shared) = shared.upgrade() else {
                            return;
                        };
                        let $s = shared.lock().unwrap_or_else(|e| e.into_inner());
                        $body
                    })
                    .build(),
            ));
        }};
    }
    fn table(name: &'static str) -> [KeyValue; 1] {
        [KeyValue::new("table", name)]
    }

    // Postgres server.
    observe!(
        u64_observable_gauge,
        "kiki.pg.connections",
        "{connection}",
        "Client connections to the Postgres server, by state",
        |s, o| {
            if let Some(server) = &s.server {
                for (state, n) in STATES.iter().zip(server.connections) {
                    o.observe(n, &[KeyValue::new("state", *state)]);
                }
            }
        }
    );
    observe!(
        u64_observable_gauge,
        "kiki.pg.database.size",
        "By",
        "Size of Kiki's database",
        |s, o| {
            if let Some(server) = &s.server {
                o.observe(server.database_bytes, &[]);
            }
        }
    );
    observe!(
        u64_observable_gauge,
        "kiki.pg.table.size",
        "By",
        "Size of a table with its indexes and TOAST",
        |s, o| {
            for (name, bytes, _) in s.server.iter().flat_map(|s| &s.tables) {
                o.observe(*bytes, &table(name));
            }
        }
    );
    observe!(
        u64_observable_gauge,
        "kiki.pg.dead_tuples",
        "{tuple}",
        "Dead tuples awaiting vacuum",
        |s, o| {
            for (name, _, dead) in s.server.iter().flat_map(|s| &s.tables) {
                o.observe(*dead, &table(name));
            }
        }
    );
    observe!(
        u64_observable_counter,
        "kiki.pg.transactions",
        "{transaction}",
        "Transactions committed and rolled back (pg_stat_database)",
        |s, o| {
            if let Some(server) = &s.server {
                o.observe(server.commits, &[KeyValue::new("result", "commit")]);
                o.observe(server.rollbacks, &[KeyValue::new("result", "rollback")]);
            }
        }
    );
    observe!(
        u64_observable_counter,
        "kiki.pg.deadlocks",
        "{deadlock}",
        "Deadlocks detected (pg_stat_database)",
        |s, o| {
            if let Some(server) = &s.server {
                o.observe(server.deadlocks, &[]);
            }
        }
    );
    observe!(
        u64_observable_counter,
        "kiki.pg.temp.written",
        "By",
        "Bytes written to temporary files by queries (pg_stat_database)",
        |s, o| {
            if let Some(server) = &s.server {
                o.observe(server.temp_bytes, &[]);
            }
        }
    );
    observe!(
        f64_observable_gauge,
        "kiki.pg.cache.hit_ratio",
        "1",
        "Share of block reads served from shared buffers over the last minute",
        |s, o| {
            if let Some(ratio) = s.cache_hit_ratio {
                o.observe(ratio, &[]);
            }
        }
    );
    observe!(
        f64_observable_gauge,
        "kiki.pg.oldest_transaction.age",
        "s",
        "Age of the oldest open transaction (0 if none)",
        |s, o| {
            if let Some(server) = &s.server {
                o.observe(server.oldest_transaction_secs, &[]);
            }
        }
    );

    // Kiki's queues. Each query fails alone, so each value is checked.
    fn queue(name: &'static str) -> [KeyValue; 1] {
        [KeyValue::new("queue", name)]
    }
    let count = |n: i64| n.max(0) as u64;
    observe!(
        u64_observable_gauge,
        "kiki.queue.pending",
        "{item}",
        "Agent runs and drawings waiting to start",
        |s, o| {
            if let Some((n, _)) = s.queues.runs {
                o.observe(count(n), &queue("agent_runs"));
            }
            if let Some((n, _)) = s.queues.drawings {
                o.observe(count(n), &queue("drawings"));
            }
        }
    );
    observe!(
        f64_observable_gauge,
        "kiki.queue.oldest_pending.age",
        "s",
        "Age of the oldest waiting item (0 if none)",
        |s, o| {
            if let Some((_, age)) = s.queues.runs {
                o.observe(age, &queue("agent_runs"));
            }
            if let Some((_, age)) = s.queues.drawings {
                o.observe(age, &queue("drawings"));
            }
        }
    );
    observe!(
        u64_observable_gauge,
        "kiki.agent_runs.stuck",
        "{run}",
        "Runs running or delivering for over 10 minutes",
        |s, o| if let Some(n) = s.queues.runs_stuck {
            o.observe(count(n), &[]);
        }
    );
    observe!(
        u64_observable_gauge,
        "kiki.conversations.open",
        "{conversation}",
        "Conversations not yet closed",
        |s, o| if let Some(n) = s.queues.conversations_open {
            o.observe(count(n), &[]);
        }
    );
    observe!(
        u64_observable_gauge,
        "kiki.embeddings.backlog",
        "{message}",
        "Searchable messages from the last 7 days without an embedding",
        |s, o| if let Some(n) = s.queues.embeddings_backlog {
            o.observe(count(n), &[]);
        }
    );

    // Railway.
    fn each(
        usage: &[Usage],
        o: &dyn AsyncInstrument<f64>,
        value: impl Fn(&Usage) -> Option<f64>,
        attributes: &[KeyValue],
    ) {
        for u in usage {
            if let Some(v) = value(u) {
                let mut attrs = vec![KeyValue::new("service", u.service.clone())];
                attrs.extend_from_slice(attributes);
                o.observe(v, &attrs);
            }
        }
    }
    observe!(
        f64_observable_gauge,
        "kiki.railway.cpu",
        "{vCPU}",
        "vCPUs a Railway service uses",
        |s, o| each(&s.railway, o, |u| u.cpu, &[])
    );
    observe!(
        f64_observable_gauge,
        "kiki.railway.memory",
        "By",
        "Memory a Railway service uses",
        |s, o| each(&s.railway, o, |u| u.memory_bytes, &[])
    );
    observe!(
        f64_observable_gauge,
        "kiki.railway.network",
        "By",
        "Public network bytes a Railway service moved in the latest minute",
        |s, o| {
            each(
                &s.railway,
                o,
                |u| u.rx_bytes,
                &[KeyValue::new("direction", "rx")],
            );
            each(
                &s.railway,
                o,
                |u| u.tx_bytes,
                &[KeyValue::new("direction", "tx")],
            );
        }
    );
    observe!(
        f64_observable_gauge,
        "kiki.railway.disk",
        "By",
        "Volume space a Railway service uses",
        |s, o| each(&s.railway, o, |u| u.disk_bytes, &[])
    );

    all
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cache_hit_ratio_is_over_the_interval() {
        let at = |hit, read| Server {
            blocks_hit: hit,
            blocks_read: read,
            ..Server::default()
        };
        assert_eq!(cache_hit_ratio(&at(100, 100), &at(190, 110)), Some(0.9));
        assert_eq!(cache_hit_ratio(&at(100, 100), &at(100, 100)), None);
        // A stats reset: the counters went back.
        assert_eq!(cache_hit_ratio(&at(100, 100), &at(5, 1)), None);
        // A reset followed by enough activity to pass the old counts.
        let reset = Server {
            stats_reset: Some(1.0),
            ..at(500, 200)
        };
        assert_eq!(cache_hit_ratio(&at(100, 100), &reset), None);
    }

    #[test]
    fn throttle_warns_once_per_window() {
        let mut throttle = Throttle::default();
        let e = anyhow::anyhow!("down");
        throttle.warn("pg", &e);
        let first = throttle.last;
        assert!(first.is_some());
        throttle.warn("pg", &e);
        throttle.warn("pg", &e);
        assert_eq!(throttle.suppressed, 2);
        assert_eq!(throttle.last, first);
    }
}
