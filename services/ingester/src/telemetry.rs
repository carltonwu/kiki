//! Logging, plus OpenTelemetry traces, metrics and logs over OTLP/HTTP
//! (docs/observability.md).
//!
//! Export is off unless `OTEL_EXPORTER_OTLP_ENDPOINT` is set, so local runs, tests
//! and the simulator only log to stdout, as before. The exporters read the other
//! standard variables themselves (`OTEL_EXPORTER_OTLP_HEADERS`, percent-decoded,
//! and the timeouts).

use std::{
    collections::HashMap,
    sync::atomic::{AtomicBool, Ordering},
    time::Duration,
};

use futures_util::future::join_all;
use opentelemetry::{KeyValue, global, metrics::Meter, trace::TracerProvider as _};
use opentelemetry_appender_tracing::layer::OpenTelemetryTracingBridge;
use opentelemetry_otlp::{LogExporter, MetricExporter, SpanExporter, WithExportConfig};
use opentelemetry_sdk::{
    Resource,
    logs::SdkLoggerProvider,
    metrics::{PeriodicReader, SdkMeterProvider, Temporality},
    propagation::TraceContextPropagator,
    trace::SdkTracerProvider,
};
use tracing::{Metadata, warn};
use tracing_opentelemetry::OpenTelemetrySpanExt;
use tracing_subscriber::{
    EnvFilter, Layer, filter::filter_fn, fmt, layer::SubscriberExt, util::SubscriberInitExt,
};

/// Instrumentation scope of every span and instrument the ingester makes.
const SCOPE: &str = "kiki-ingester";
const METRIC_INTERVAL: Duration = Duration::from_secs(60);
/// Per request, so one stalled export can't use up [`SHUTDOWN_TIMEOUT`].
const EXPORT_TIMEOUT: Duration = Duration::from_secs(3);
/// Bounds the final flush of all three signals together.
const SHUTDOWN_TIMEOUT: Duration = Duration::from_secs(5);

/// Set once export is on, for work that only feeds telemetry (the platform poller).
static EXPORTING: AtomicBool = AtomicBool::new(false);

/// Holds the providers until [`Telemetry::shutdown`] flushes them.
pub struct Telemetry {
    providers: Option<Providers>,
}

struct Providers {
    tracer: SdkTracerProvider,
    meter: SdkMeterProvider,
    logger: SdkLoggerProvider,
}

/// Installs the global `tracing` subscriber: stdout logs (JSON with
/// `LOG_FORMAT=json`), plus OpenTelemetry export when `export` is set and the
/// environment enables it. An exporter that fails to build is logged and skipped:
/// telemetry never stops the bot.
pub fn init(json_logs: bool, export: bool) -> Telemetry {
    let filter = EnvFilter::try_from_default_env()
        .unwrap_or_else(|_| "info,sqlx=warn,twilight_http_ratelimiting=warn".into());
    let (providers, error) = match export && enabled(|name| std::env::var(name).ok()) {
        true => match Providers::build() {
            Ok(providers) => (Some(providers), None),
            Err(e) => (None, Some(e)),
        },
        false => (None, None),
    };
    let spans = providers.as_ref().map(|p| {
        tracing_opentelemetry::layer()
            .with_tracer(p.tracer.tracer(SCOPE))
            .with_filter(filter_fn(|m| m.is_span() && ours(m)))
    });
    let logs = providers
        .as_ref()
        .map(|p| OpenTelemetryTracingBridge::new(&p.logger).with_filter(filter_fn(not_exporter)));
    tracing_subscriber::registry()
        .with(filter)
        // Stdout shows events only, as before there were spans: no span names
        // or fields on the lines, in either format.
        .with(json_logs.then(|| {
            fmt::layer()
                .json()
                .flatten_event(true)
                .with_filter(filter_fn(|m| !m.is_span()))
        }))
        .with((!json_logs).then(|| fmt::layer().with_filter(filter_fn(|m| !m.is_span()))))
        .with(spans)
        .with(logs)
        .init();
    if let Some(e) = error {
        warn!(error = %e, "OpenTelemetry export is off: exporter setup failed");
    }
    EXPORTING.store(providers.is_some(), Ordering::Relaxed);
    Telemetry { providers }
}

/// The meter for the ingester's instruments. A no-op when export is off.
pub fn meter() -> Meter {
    global::meter(SCOPE)
}

/// Whether telemetry is exported. Off, metrics only some poll would feed aren't worth polling.
pub fn exporting() -> bool {
    EXPORTING.load(Ordering::Relaxed)
}

/// W3C trace context (`traceparent`) of the current span, as request headers.
/// Only for the agent host, which continues the trace; empty when export is off.
pub fn trace_headers() -> HashMap<String, String> {
    let mut headers = HashMap::new();
    let context = tracing::Span::current().context();
    global::get_text_map_propagator(|propagator| propagator.inject_context(&context, &mut headers));
    headers
}

impl Telemetry {
    /// Flushes and stops the exporters, giving up after [`SHUTDOWN_TIMEOUT`].
    pub async fn shutdown(self) {
        let Some(providers) = self.providers else {
            return;
        };
        // Concurrently, so a stalled signal doesn't starve the others (the final
        // kiki.leader.held = 0 among them). Plain threads, not spawn_blocking:
        // the runtime waits for blocking tasks on exit, and a stuck export must
        // not hold the process up.
        let Providers {
            tracer,
            meter,
            logger,
        } = providers;
        let stops: [(&str, Box<dyn FnOnce() -> _ + Send>); 3] = [
            (
                "traces",
                Box::new(move || tracer.shutdown_with_timeout(SHUTDOWN_TIMEOUT)),
            ),
            (
                "metrics",
                Box::new(move || meter.shutdown_with_timeout(SHUTDOWN_TIMEOUT)),
            ),
            (
                "logs",
                Box::new(move || logger.shutdown_with_timeout(SHUTDOWN_TIMEOUT)),
            ),
        ];
        let stops = stops.into_iter().map(|(signal, stop)| {
            let (done_tx, done_rx) = tokio::sync::oneshot::channel();
            std::thread::spawn(move || {
                if let Err(e) = stop() {
                    warn!(signal, error = %e, "OpenTelemetry flush failed");
                }
                let _ = done_tx.send(());
            });
            done_rx
        });
        if tokio::time::timeout(SHUTDOWN_TIMEOUT, join_all(stops))
            .await
            .is_err()
        {
            warn!("OpenTelemetry flush timed out");
        }
    }
}

impl Providers {
    fn build() -> anyhow::Result<Self> {
        let resource = resource(|name| std::env::var(name).ok());
        // The batch processors and the metric reader each run on their own
        // thread, so the blocking HTTP client never blocks the runtime.
        let tracer = SdkTracerProvider::builder()
            .with_batch_exporter(
                SpanExporter::builder()
                    .with_http()
                    .with_timeout(EXPORT_TIMEOUT)
                    .build()?,
            )
            .with_resource(resource.clone())
            .build();
        // Cumulative, as Prometheus-style backends (Grafana Mimir) expect.
        let metrics = MetricExporter::builder()
            .with_http()
            .with_timeout(EXPORT_TIMEOUT)
            .with_temporality(Temporality::Cumulative)
            .build()?;
        let meter = SdkMeterProvider::builder()
            .with_reader(
                PeriodicReader::builder(metrics)
                    .with_interval(METRIC_INTERVAL)
                    .build(),
            )
            .with_resource(resource.clone())
            .build();
        let logger = SdkLoggerProvider::builder()
            .with_batch_exporter(
                LogExporter::builder()
                    .with_http()
                    .with_timeout(EXPORT_TIMEOUT)
                    .build()?,
            )
            .with_resource(resource)
            .build();
        global::set_tracer_provider(tracer.clone());
        global::set_meter_provider(meter.clone());
        // W3C `traceparent`, for carrying a trace into the agent host.
        global::set_text_map_propagator(TraceContextPropagator::new());
        Ok(Self {
            tracer,
            meter,
            logger,
        })
    }
}

/// Export is on when an endpoint is set and the SDK isn't disabled.
fn enabled(env: impl Fn(&str) -> Option<String>) -> bool {
    let endpoint = env("OTEL_EXPORTER_OTLP_ENDPOINT").is_some_and(|v| !v.trim().is_empty());
    let disabled = env("OTEL_SDK_DISABLED").is_some_and(|v| v.trim().eq_ignore_ascii_case("true"));
    endpoint && !disabled
}

fn resource(env: impl Fn(&str) -> Option<String>) -> Resource {
    let env = |name: &str| env(name).filter(|v| !v.trim().is_empty());
    let name = env("OTEL_SERVICE_NAME").unwrap_or_else(|| "ingester".into());
    let mut attributes = vec![
        KeyValue::new("service.namespace", "kiki"),
        KeyValue::new(
            "deployment.environment.name",
            env("RAILWAY_ENVIRONMENT_NAME").unwrap_or_else(|| "local".into()),
        ),
        // The `instance` label. Each service runs as one instance, so the name:
        // a restart or a new replica continues the same series.
        KeyValue::new("service.instance.id", name.clone()),
    ];
    if let Some(sha) = env("RAILWAY_GIT_COMMIT_SHA") {
        attributes.push(KeyValue::new("service.version", sha));
    }
    Resource::builder()
        .with_service_name(name)
        .with_attributes(attributes)
        .build()
}

/// Only the ingester's own spans are exported as spans. Dependencies' spans (one
/// per twilight REST call, say) would each start a trace of their own, and events
/// already go out as logs carrying the span's trace ID.
fn ours(metadata: &Metadata<'_>) -> bool {
    metadata.target().split("::").next() == Some(env!("CARGO_CRATE_NAME"))
}

/// Keeps the exporter's own logs and spans (the SDK, its HTTP client) out of the
/// export, where each export would log another one.
fn not_exporter(metadata: &Metadata<'_>) -> bool {
    let krate = metadata.target().split("::").next().unwrap_or_default();
    !(krate.starts_with("opentelemetry")
        || matches!(
            krate,
            "reqwest" | "hyper" | "hyper_util" | "h2" | "tower" | "rustls"
        ))
}

#[cfg(test)]
mod tests {
    use std::collections::HashMap;

    use opentelemetry::{Key, Value};

    use super::*;

    fn env(vars: &[(&str, &str)]) -> impl Fn(&str) -> Option<String> {
        let vars: HashMap<String, String> = vars
            .iter()
            .map(|(k, v)| (k.to_string(), v.to_string()))
            .collect();
        move |name| vars.get(name).cloned()
    }

    #[test]
    fn export_needs_an_endpoint_and_no_kill_switch() {
        let endpoint = ("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318");
        assert!(!enabled(env(&[])));
        assert!(!enabled(env(&[("OTEL_EXPORTER_OTLP_ENDPOINT", " ")])));
        assert!(enabled(env(&[endpoint])));
        assert!(enabled(env(&[endpoint, ("OTEL_SDK_DISABLED", "false")])));
        assert!(!enabled(env(&[endpoint, ("OTEL_SDK_DISABLED", "TRUE")])));
    }

    #[test]
    fn resource_describes_the_railway_deployment() {
        let get = |resource: &Resource, key: &'static str| resource.get(&Key::new(key));
        let local = resource(env(&[]));
        assert_eq!(get(&local, "service.name"), Some(Value::from("ingester")));
        assert_eq!(get(&local, "service.namespace"), Some(Value::from("kiki")));
        assert_eq!(
            get(&local, "deployment.environment.name"),
            Some(Value::from("local"))
        );
        assert_eq!(get(&local, "service.version"), None);
        assert_eq!(
            get(&local, "service.instance.id"),
            Some(Value::from("ingester"))
        );

        let railway = resource(env(&[
            ("OTEL_SERVICE_NAME", "ingester-staging"),
            ("RAILWAY_ENVIRONMENT_NAME", "production"),
            ("RAILWAY_GIT_COMMIT_SHA", "abc123"),
        ]));
        assert_eq!(
            get(&railway, "service.name"),
            Some(Value::from("ingester-staging"))
        );
        assert_eq!(
            get(&railway, "deployment.environment.name"),
            Some(Value::from("production"))
        );
        assert_eq!(
            get(&railway, "service.version"),
            Some(Value::from("abc123"))
        );
        assert_eq!(
            get(&railway, "service.instance.id"),
            Some(Value::from("ingester-staging"))
        );
    }
}
