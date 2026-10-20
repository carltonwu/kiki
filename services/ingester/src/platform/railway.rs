//! Railway's resource metrics for the project's services, from its public
//! GraphQL API (the same `metrics` query the Railway CLI makes).
//!
//! Optional: on only with `RAILWAY_METRICS_TOKEN`, a project token for this
//! environment. Railway injects `RAILWAY_PROJECT_ID` and
//! `RAILWAY_ENVIRONMENT_ID` into every service.

use std::{
    collections::HashMap,
    time::{Duration, Instant},
};

use anyhow::{Context, anyhow};
use reqwest::{StatusCode, header};
use serde::{Deserialize, de::DeserializeOwned};
use serde_json::{Value, json};
use twilight_model::util::Timestamp;

const ENDPOINT: &str = "https://backboard.railway.com/graphql/v2";
const TIMEOUT: Duration = Duration::from_secs(10);
/// How far back each poll asks for samples; the newest one is kept.
const WINDOW_SECS: u64 = 300;
/// One sample a minute, the poll interval: network samples are the bytes
/// moved in that minute.
const SAMPLE_RATE_SECS: u32 = 60;
/// Railway reports sizes in decimal GB (samples are whole 4 KiB pages × 10⁻⁹).
const BYTES_PER_GB: f64 = 1e9;
/// Service names are fetched again this often, and whenever a sample names
/// an unknown service, so a rename shows up without a restart.
const NAMES_MAX_AGE: Duration = Duration::from_secs(3600);

const METRICS: &str = r#"
query Metrics($projectId: String!, $environmentId: String!, $startDate: DateTime!,
              $sampleRateSeconds: Int!) {
  metrics(projectId: $projectId, environmentId: $environmentId, startDate: $startDate,
          sampleRateSeconds: $sampleRateSeconds, groupBy: [SERVICE_ID],
          measurements: [CPU_USAGE, MEMORY_USAGE_GB, NETWORK_RX_GB, NETWORK_TX_GB, DISK_USAGE_GB]) {
    measurement
    tags { serviceId }
    values { ts value }
  }
}"#;

const SERVICES: &str = r#"
query Services($projectId: String!) {
  project(id: $projectId) { services { edges { node { id name } } } }
}"#;

/// The newest sample of each measurement for one service.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Usage {
    /// The Railway service name (`ingester`, `agent`, `postgres`).
    pub service: String,
    /// vCPUs in use.
    pub cpu: Option<f64>,
    pub memory_bytes: Option<f64>,
    /// Public network bytes received and sent in the sample's minute.
    pub rx_bytes: Option<f64>,
    pub tx_bytes: Option<f64>,
    /// Volume usage; 0 for a service without one.
    pub disk_bytes: Option<f64>,
}

#[derive(Debug)]
pub enum Error {
    /// The token was refused: polling it again won't help.
    Unauthorized(String),
    Other(anyhow::Error),
}

impl From<anyhow::Error> for Error {
    fn from(e: anyhow::Error) -> Self {
        Self::Other(e)
    }
}

pub struct Railway {
    http: reqwest::Client,
    project_id: String,
    environment_id: String,
    /// Service ID → name, and when it was fetched.
    names: HashMap<String, String>,
    names_at: Option<Instant>,
}

impl Railway {
    /// `None` without a token. Errors if the token is set but unusable.
    pub fn from_env(env: impl Fn(&str) -> Option<String>) -> anyhow::Result<Option<Self>> {
        let env = |name: &str| {
            env(name)
                .map(|v| v.trim().to_owned())
                .filter(|v| !v.is_empty())
        };
        let Some(token) = env("RAILWAY_METRICS_TOKEN") else {
            return Ok(None);
        };
        let (Some(project_id), Some(environment_id)) =
            (env("RAILWAY_PROJECT_ID"), env("RAILWAY_ENVIRONMENT_ID"))
        else {
            anyhow::bail!(
                "RAILWAY_METRICS_TOKEN is set but RAILWAY_PROJECT_ID or RAILWAY_ENVIRONMENT_ID isn't"
            );
        };
        // A project token goes in its own header, not `Authorization: Bearer`.
        let mut value = header::HeaderValue::from_str(&token)
            .context("RAILWAY_METRICS_TOKEN is not a valid header value")?;
        value.set_sensitive(true);
        let mut headers = header::HeaderMap::new();
        headers.insert("Project-Access-Token", value);
        let http = reqwest::Client::builder()
            .default_headers(headers)
            .timeout(TIMEOUT)
            .redirect(reqwest::redirect::Policy::none())
            .build()?;
        Ok(Some(Self {
            http,
            project_id,
            environment_id,
            names: HashMap::new(),
            names_at: None,
        }))
    }

    pub async fn poll(&mut self, now_secs: u64) -> Result<Vec<Usage>, Error> {
        let start = Timestamp::from_secs(now_secs.saturating_sub(WINDOW_SECS) as i64)
            .context("timestamp out of range")?;
        let data: MetricsData = self
            .query(
                METRICS,
                json!({
                    "projectId": self.project_id,
                    "environmentId": self.environment_id,
                    "startDate": start.iso_8601().to_string(),
                    "sampleRateSeconds": SAMPLE_RATE_SECS,
                }),
            )
            .await?;
        let unknown = data
            .metrics
            .iter()
            .filter_map(|s| s.tags.service_id.as_ref())
            .any(|id| !self.names.contains_key(id));
        let stale = self.names_at.is_none_or(|at| at.elapsed() >= NAMES_MAX_AGE);
        if unknown || stale {
            let services: ServicesData = self
                .query(SERVICES, json!({ "projectId": self.project_id }))
                .await?;
            self.names = services
                .project
                .services
                .edges
                .into_iter()
                .map(|e| (e.node.id, e.node.name))
                .collect();
            self.names_at = Some(Instant::now());
        }
        Ok(latest(data, &self.names))
    }

    async fn query<T: DeserializeOwned>(&self, query: &str, variables: Value) -> Result<T, Error> {
        let response = self
            .http
            .post(ENDPOINT)
            .json(&json!({ "query": query, "variables": variables }))
            .send()
            .await
            .context("Railway API request failed")?;
        let status = response.status();
        if matches!(status, StatusCode::UNAUTHORIZED | StatusCode::FORBIDDEN) {
            return Err(Error::Unauthorized(format!("HTTP {status}")));
        }
        let body = response
            .text()
            .await
            .context("reading the Railway API response")?;
        if !status.is_success() {
            return Err(anyhow!("Railway API returned HTTP {status}").into());
        }
        parse(&body)
    }
}

/// A GraphQL response's data, or its first error. Railway answers a refused
/// token with HTTP 200 and `Not Authorized` (or `Project Token not found`).
fn parse<T: DeserializeOwned>(body: &str) -> Result<T, Error> {
    #[derive(Deserialize)]
    struct Response<T> {
        data: Option<T>,
        #[serde(default)]
        errors: Vec<GraphQlError>,
    }
    #[derive(Deserialize)]
    struct GraphQlError {
        message: String,
    }
    let response: Response<T> =
        serde_json::from_str(body).context("unexpected Railway API response")?;
    if let Some(error) = response.errors.into_iter().next() {
        let message = error.message;
        let lower = message.to_lowercase();
        return Err(
            if lower.contains("not authorized")
                || lower.contains("unauthorized")
                || lower.contains("token not found")
            {
                Error::Unauthorized(message)
            } else {
                Error::Other(anyhow!("Railway API error: {message}"))
            },
        );
    }
    response
        .data
        .ok_or_else(|| anyhow!("Railway API response has no data").into())
}

#[derive(Deserialize)]
struct MetricsData {
    metrics: Vec<Series>,
}

#[derive(Deserialize)]
struct Series {
    measurement: String,
    tags: Tags,
    values: Vec<Sample>,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct Tags {
    service_id: Option<String>,
}

#[derive(Deserialize)]
struct Sample {
    ts: i64,
    value: f64,
}

#[derive(Deserialize)]
struct ServicesData {
    project: Project,
}

#[derive(Deserialize)]
struct Project {
    services: Connection,
}

#[derive(Deserialize)]
struct Connection {
    edges: Vec<Edge>,
}

#[derive(Deserialize)]
struct Edge {
    node: Node,
}

#[derive(Deserialize)]
struct Node {
    id: String,
    name: String,
}

/// The newest sample per service and measurement, by service name. Series
/// without a known service are dropped.
fn latest(data: MetricsData, names: &HashMap<String, String>) -> Vec<Usage> {
    let mut usage: Vec<Usage> = Vec::new();
    for series in data.metrics {
        let Some(name) = series.tags.service_id.and_then(|id| names.get(&id)) else {
            continue;
        };
        let Some(newest) = series.values.iter().max_by_key(|s| s.ts) else {
            continue;
        };
        let i = match usage.iter().position(|u| &u.service == name) {
            Some(i) => i,
            None => {
                usage.push(Usage {
                    service: name.clone(),
                    ..Usage::default()
                });
                usage.len() - 1
            }
        };
        let entry = &mut usage[i];
        let value = newest.value.max(0.0);
        match series.measurement.as_str() {
            "CPU_USAGE" => entry.cpu = Some(value),
            "MEMORY_USAGE_GB" => entry.memory_bytes = Some(value * BYTES_PER_GB),
            "NETWORK_RX_GB" => entry.rx_bytes = Some(value * BYTES_PER_GB),
            "NETWORK_TX_GB" => entry.tx_bytes = Some(value * BYTES_PER_GB),
            "DISK_USAGE_GB" => entry.disk_bytes = Some(value * BYTES_PER_GB),
            _ => {}
        }
    }
    usage.sort_by(|a, b| a.service.cmp(&b.service));
    usage
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Shaped like a real response (from the metrics of this project's
    /// production services), trimmed to a few samples.
    const METRICS_RESPONSE: &str = r#"{"data":{"metrics":[
        {"measurement":"CPU_USAGE","tags":{"serviceId":"svc-pg"},
         "values":[{"ts":1760860800,"value":0.0004},{"ts":1760860920,"value":0.000415},{"ts":1760860860,"value":0.0007}]},
        {"measurement":"MEMORY_USAGE_GB","tags":{"serviceId":"svc-pg"},
         "values":[{"ts":1760860860,"value":0.084340736},{"ts":1760860920,"value":0.087969792}]},
        {"measurement":"DISK_USAGE_GB","tags":{"serviceId":"svc-pg"},
         "values":[{"ts":1760860920,"value":1.8530304}]},
        {"measurement":"NETWORK_RX_GB","tags":{"serviceId":"svc-ingester"},
         "values":[{"ts":1760860860,"value":0.000004586},{"ts":1760860920,"value":0.000002}]},
        {"measurement":"NETWORK_TX_GB","tags":{"serviceId":"svc-ingester"},
         "values":[{"ts":1760860920,"value":0.000002456}]},
        {"measurement":"CPU_USAGE","tags":{"serviceId":"svc-ingester"},"values":[]},
        {"measurement":"CPU_USAGE","tags":{"serviceId":"svc-gone"},
         "values":[{"ts":1760860920,"value":1.0}]},
        {"measurement":"CPU_USAGE","tags":{},"values":[{"ts":1760860920,"value":1.0}]}
    ]}}"#;

    const SERVICES_RESPONSE: &str = r#"{"data":{"project":{"services":{"edges":[
        {"node":{"id":"svc-agent","name":"agent"}},
        {"node":{"id":"svc-ingester","name":"ingester"}},
        {"node":{"id":"svc-pg","name":"postgres"}}
    ]}}}}"#;

    fn names() -> HashMap<String, String> {
        let services: ServicesData = parse(SERVICES_RESPONSE).unwrap();
        services
            .project
            .services
            .edges
            .into_iter()
            .map(|e| (e.node.id, e.node.name))
            .collect()
    }

    #[test]
    fn keeps_the_newest_sample_per_service_and_measurement() {
        let data: MetricsData = parse(METRICS_RESPONSE).unwrap();
        let usage = latest(data, &names());
        assert_eq!(
            usage,
            vec![
                Usage {
                    service: "ingester".into(),
                    rx_bytes: Some(2000.0),
                    tx_bytes: Some(2456.0),
                    ..Usage::default()
                },
                Usage {
                    service: "postgres".into(),
                    cpu: Some(0.000415),
                    memory_bytes: Some(87_969_792.0),
                    disk_bytes: Some(1_853_030_400.0),
                    ..Usage::default()
                },
            ]
        );
    }

    #[test]
    fn a_refused_token_is_unauthorized() {
        // What Railway answers a bad or unknown token (HTTP 200).
        let body = r#"{"errors":[{"message":"Not Authorized","locations":[{"line":1,"column":44}],
            "path":["metrics"],"extensions":{"code":"INTERNAL_SERVER_ERROR"}}],"data":null}"#;
        assert!(matches!(
            parse::<MetricsData>(body),
            Err(Error::Unauthorized(m)) if m == "Not Authorized"
        ));
        let body = r#"{"errors":[{"message":"Project Token not found"}],"data":null}"#;
        assert!(matches!(
            parse::<MetricsData>(body),
            Err(Error::Unauthorized(_))
        ));
        let body = r#"{"errors":[{"message":"Problem processing request"}],"data":null}"#;
        assert!(matches!(parse::<MetricsData>(body), Err(Error::Other(_))));
        assert!(matches!(
            parse::<MetricsData>("<html>"),
            Err(Error::Other(_))
        ));
    }

    #[test]
    fn needs_the_token_and_railway_ids() {
        // Building the client needs a crypto provider, as in main.
        let _ = rustls::crypto::ring::default_provider().install_default();
        let env = |vars: &'static [(&'static str, &'static str)]| {
            move |name: &str| {
                vars.iter()
                    .find(|(k, _)| *k == name)
                    .map(|(_, v)| v.to_string())
            }
        };
        assert!(Railway::from_env(env(&[])).unwrap().is_none());
        assert!(
            Railway::from_env(env(&[("RAILWAY_METRICS_TOKEN", " ")]))
                .unwrap()
                .is_none()
        );
        assert!(Railway::from_env(env(&[("RAILWAY_METRICS_TOKEN", "t")])).is_err());
        let railway = Railway::from_env(env(&[
            ("RAILWAY_METRICS_TOKEN", "t"),
            ("RAILWAY_PROJECT_ID", "p"),
            ("RAILWAY_ENVIRONMENT_ID", "e"),
        ]))
        .unwrap()
        .unwrap();
        assert_eq!(
            (railway.project_id.as_str(), railway.environment_id.as_str()),
            ("p", "e")
        );
    }

    #[test]
    fn start_date_is_iso_8601() {
        let start = Timestamp::from_secs(1_760_860_800).unwrap();
        assert_eq!(
            start.iso_8601().to_string(),
            "2025-10-19T08:00:00.000000+00:00"
        );
    }
}
