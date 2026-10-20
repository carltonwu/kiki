//! Private HTTP client. Retries always carry the same durable job ID and input.

use std::time::Duration;

use anyhow::{Context, bail};
use reqwest::{Response, StatusCode, Url, header};
use serde::{Deserialize, Serialize, de::DeserializeOwned};
use tracing::{debug, warn};

use super::people::PersonRef;
use crate::{
    chat::{Message, Usage},
    config::AgentConfig,
};

pub(crate) struct Host {
    http: reqwest::Client,
    endpoint: Url,
    image_endpoint: Url,
    emoji_endpoint: Url,
    timeout: Duration,
}

#[derive(Deserialize, Serialize)]
pub(super) struct Scope {
    pub guild_id: String,
    pub channel_id: String,
    pub conversation_id: String,
}

impl Scope {
    pub fn new(guild_id: i64, channel_id: i64, conversation_id: i64) -> Self {
        Self {
            guild_id: guild_id.to_string(),
            channel_id: channel_id.to_string(),
            conversation_id: conversation_id.to_string(),
        }
    }
}

#[derive(Deserialize, Serialize)]
pub(super) struct JobRequest {
    pub id: String,
    pub kind: String,
    pub scope: Scope,
    pub model: String,
    pub messages: Vec<Message>,
    pub max_output_tokens: u32,
    /// The roster the messages refer to. Absent in requests saved before it existed.
    #[serde(default)]
    pub people: Vec<PersonRef>,
    /// Offers the `generate_image` tool. Omitted when off, so requests saved
    /// before it existed compare equal on retry.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub image_model: Option<String>,
}

/// An image the host saved in `agent_host_images` for this job (the row has
/// its bytes and MIME type).
#[derive(Deserialize)]
pub(super) struct ImageRef {
    pub id: i64,
}

#[derive(Deserialize)]
pub(super) struct JobResult {
    pub text: String,
    pub model: Option<String>,
    pub steps: i32,
    pub usage: Usage,
    /// Drawn during the reply, by hosts from before drawings were queued.
    #[serde(default)]
    pub images: Vec<ImageRef>,
    /// Pictures the reply queued (the host's `Drawing`, passed through as is):
    /// each becomes an image job after the reply is posted.
    #[serde(default)]
    pub drawings: Vec<serde_json::Value>,
}

/// One queued drawing (`POST /v1/images`), ID `image:<run>:<n>`. Snapshotted in
/// `agent_image_jobs` so retries send identical input.
#[derive(Deserialize, Serialize)]
pub(super) struct ImageJobRequest {
    pub id: String,
    pub scope: Scope,
    pub image_model: String,
    /// The reply job that queued it: the host writes a failure note from its prompt.
    pub reply_job: String,
    pub chat_model: String,
    /// What Kiki posted when she queued it.
    pub ack: String,
    pub drawing: serde_json::Value,
}

#[derive(Deserialize)]
pub(super) struct ImageJobResult {
    pub image: Option<ImageRef>,
    #[serde(default)]
    pub caption: String,
    pub cost: Option<f64>,
    /// `refused` or `failed` when there's no image.
    pub error: Option<String>,
    /// Kiki's message about the failure, written by the chat model; empty if
    /// even that failed.
    #[serde(default)]
    pub failure_note: String,
}

/// Describes a server's emoji that lack a description (`emojis.rs`): one
/// bounded batch per call. Not a durable job: the host's writes are idempotent.
#[derive(Serialize)]
pub(crate) struct DescribeEmojis {
    pub guild_id: String,
    pub model: String,
    /// Unix ms (database clock): emoji already tried since then are skipped,
    /// so repeated calls make progress past failures.
    pub attempted_before_ms: i64,
    /// Also replace descriptions written before `attempted_before_ms`.
    pub redescribe: bool,
}

#[derive(Debug, Default, Deserialize)]
pub(crate) struct EmojisDescribed {
    pub described: i64,
    pub failed: i64,
    /// Still to try in this round.
    pub remaining: i64,
    pub cost: Option<f64>,
}

#[derive(Deserialize)]
#[serde(rename_all = "lowercase")]
enum JobStatus {
    Running,
    Completed,
    Failed,
}

#[derive(Deserialize)]
struct JobResponse<T> {
    id: String,
    status: JobStatus,
    result: Option<T>,
    error: Option<String>,
}

impl Host {
    pub fn new(config: &AgentConfig) -> anyhow::Result<Self> {
        let base = config
            .host_url
            .as_deref()
            .context("AGENT_HOST_URL is required")?;
        let endpoint = Url::parse(&format!("{}/v1/jobs", base.trim_end_matches('/')))
            .context("invalid AGENT_HOST_URL")?;
        let emoji_endpoint = endpoint.join("emojis/describe")?;
        let image_endpoint = endpoint.join("images")?;
        if !matches!(endpoint.scheme(), "http" | "https")
            || !endpoint.username().is_empty()
            || endpoint.password().is_some()
            || endpoint.query().is_some()
            || endpoint.fragment().is_some()
        {
            bail!("AGENT_HOST_URL must be an HTTP base URL without credentials, query or fragment");
        }
        let token = config
            .host_token
            .as_deref()
            .context("AGENT_HOST_TOKEN is required")?;
        let mut authorization = header::HeaderValue::from_str(&format!("Bearer {token}"))
            .context("invalid AGENT_HOST_TOKEN")?;
        authorization.set_sensitive(true);
        let mut headers = header::HeaderMap::new();
        headers.insert(header::AUTHORIZATION, authorization);
        let http = reqwest::Client::builder()
            .default_headers(headers)
            .connect_timeout(Duration::from_secs(10))
            .timeout(config.host_timeout)
            .redirect(reqwest::redirect::Policy::none())
            .build()?;
        Ok(Self {
            http,
            endpoint,
            image_endpoint,
            emoji_endpoint,
            timeout: config.host_timeout,
        })
    }

    pub(super) async fn execute(&self, job: &JobRequest) -> anyhow::Result<JobResult> {
        let result: JobResult = tokio::time::timeout(
            self.timeout,
            self.run_with_retries(&self.endpoint, &job.id, &job.kind, job),
        )
        .await
        .context("agent host deadline exceeded (including wake-up retries)")??;
        if result.text.trim().is_empty()
            || result.steps < 0
            || result.usage.prompt_tokens < 0
            || result.usage.completion_tokens < 0
            || result
                .usage
                .cost
                .is_some_and(|cost| !cost.is_finite() || cost < 0.0)
        {
            bail!("agent host returned an invalid result");
        }
        Ok(result)
    }

    /// Draws a queued picture: the same durable job contract as replies, with
    /// its own ID, so a retry gets the saved image instead of drawing again.
    pub(super) async fn draw(&self, job: &ImageJobRequest) -> anyhow::Result<ImageJobResult> {
        let result: ImageJobResult = tokio::time::timeout(
            self.timeout,
            self.run_with_retries(&self.image_endpoint, &job.id, "image", job),
        )
        .await
        .context("agent host deadline exceeded (including wake-up retries)")??;
        if result.image.is_none() == result.error.is_none()
            || result
                .cost
                .is_some_and(|cost| !cost.is_finite() || cost < 0.0)
        {
            bail!("agent host returned an invalid image result");
        }
        Ok(result)
    }

    /// One describe batch, retried like jobs while the host wakes up or is busy.
    pub async fn describe_emojis(
        &self,
        request: &DescribeEmojis,
    ) -> anyhow::Result<EmojisDescribed> {
        tokio::time::timeout(self.timeout, async {
            let mut delay = Duration::from_secs(1);
            loop {
                if let Ok(response) = self.attempt(&self.emoji_endpoint, request).await? {
                    return response
                        .json()
                        .await
                        .context("parsing agent host emoji response");
                }
                debug!(
                    guild_id = request.guild_id,
                    delay_ms = delay.as_millis() as u64,
                    "retrying emoji descriptions"
                );
                tokio::time::sleep(delay).await;
                delay = (delay * 2).min(Duration::from_secs(5));
            }
        })
        .await
        .context("agent host deadline exceeded (including wake-up retries)")?
    }

    /// One POST: the response, why to retry while the host wakes up or is busy
    /// (reason and HTTP status, for logs), or an error retrying won't fix.
    async fn attempt(
        &self,
        url: &Url,
        body: &impl Serialize,
    ) -> anyhow::Result<Result<Response, (&'static str, Option<u16>)>> {
        let mut request = self.http.post(url.clone()).json(body);
        // The agent host continues the current trace (the run's span).
        for (name, value) in crate::telemetry::trace_headers() {
            request = request.header(name, value);
        }
        match request.send().await {
            Err(error) if error.is_timeout() => Ok(Err(("timeout", None))),
            Err(error) if error.is_connect() || error.is_request() => Ok(Err(("connection", None))),
            Err(error) => Err(error).context("calling agent host"),
            Ok(response) if retryable(response.status()) => {
                Ok(Err(("http", Some(response.status().as_u16()))))
            }
            Ok(response) if response.status().is_success() => Ok(Ok(response)),
            Ok(response) if response.status() == StatusCode::NOT_IMPLEMENTED => {
                bail!("agent host has no framework executor configured")
            }
            // Do not log bodies: they may contain prompts or secrets.
            Ok(response) => bail!("agent host returned HTTP {}", response.status()),
        }
    }

    async fn run_with_retries<T: DeserializeOwned>(
        &self,
        url: &Url,
        id: &str,
        kind: &str,
        job: &impl Serialize,
    ) -> anyhow::Result<T> {
        let mut delay = Duration::from_secs(1);
        let mut attempt = 0;
        let mut warned = false;
        loop {
            attempt += 1;
            let (reason, status) = match self.attempt(url, job).await? {
                Err(retry) => retry,
                Ok(response) => {
                    let status = response.status();
                    let response: JobResponse<T> = response
                        .json()
                        .await
                        .context("parsing agent host response")?;
                    if response.id != id {
                        bail!("agent host returned a different job ID");
                    }
                    match response.status {
                        JobStatus::Completed => {
                            return response.result.context("host completed without result");
                        }
                        JobStatus::Failed => bail!(
                            "agent host job failed: {}",
                            response.error.as_deref().unwrap_or("unknown error")
                        ),
                        JobStatus::Running => ("running", Some(status.as_u16())),
                    }
                }
            };
            if reason != "running" && !warned {
                warn!(
                    job_id = id,
                    kind, attempt, reason, status, "agent host unavailable; retrying the same job"
                );
                warned = true;
            }
            debug!(
                job_id = id,
                attempt,
                reason,
                status,
                delay_ms = delay.as_millis() as u64,
                "retrying agent host job"
            );
            tokio::time::sleep(delay).await;
            delay = (delay * 2).min(Duration::from_secs(5));
        }
    }
}

fn retryable(status: StatusCode) -> bool {
    matches!(
        status,
        StatusCode::BAD_GATEWAY
            | StatusCode::SERVICE_UNAVAILABLE
            | StatusCode::GATEWAY_TIMEOUT
            | StatusCode::TOO_MANY_REQUESTS
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::{
        Json, Router, extract::State, http::HeaderMap, response::IntoResponse, routing::post,
    };
    use serde_json::{Value, json};
    use std::sync::{Arc, Mutex};

    async fn server(router: Router) -> (String, tokio::task::JoinHandle<()>) {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let url = format!("http://{}", listener.local_addr().unwrap());
        let task = tokio::spawn(async move { axum::serve(listener, router).await.unwrap() });
        (url, task)
    }

    fn host(url: String, timeout: Duration) -> Host {
        let _ = rustls::crypto::ring::default_provider().install_default();
        Host::new(&AgentConfig {
            host_url: Some(url),
            host_token: Some("test-token".into()),
            host_timeout: timeout,
            chat_model: "test/model".into(),
            topic_model: "test/model".into(),
            image_model: "test/image".into(),
            emoji_model: "test/vision".into(),
            history_messages: 30,
            run_max_age: Duration::from_secs(120),
            topic_every_messages: 4,
        })
        .unwrap()
    }

    fn job() -> JobRequest {
        JobRequest {
            id: "reply:42".into(),
            kind: "reply".into(),
            scope: Scope::new(1, 2, 3),
            model: "test/model".into(),
            messages: vec![Message::user("hello")],
            max_output_tokens: 100,
            people: Vec::new(),
            image_model: None,
        }
    }

    #[tokio::test]
    async fn retries_wake_up_and_running_jobs_with_identical_input() {
        type Requests = Arc<Mutex<Vec<Value>>>;
        async fn handler(
            State(requests): State<Requests>,
            headers: HeaderMap,
            Json(body): Json<Value>,
        ) -> axum::response::Response {
            assert_eq!(headers["authorization"], "Bearer test-token");
            let mut requests = requests.lock().unwrap();
            requests.push(body);
            match requests.len() {
                1 => StatusCode::BAD_GATEWAY.into_response(),
                2 => (
                    StatusCode::ACCEPTED,
                    Json(json!({"id":"reply:42", "status":"running"})),
                )
                    .into_response(),
                _ => Json(json!({"id":"reply:42", "status":"completed", "result": {
                    "text":"hello", "model":"test/model", "steps":1,
                    "usage":{"prompt_tokens":12,"completion_tokens":3,"cost":0.001}
                }}))
                .into_response(),
            }
        }
        let requests = Requests::default();
        let (url, server) = server(
            Router::new()
                .route("/v1/jobs", post(handler))
                .with_state(requests.clone()),
        )
        .await;
        let result = host(url, Duration::from_secs(10))
            .execute(&job())
            .await
            .unwrap();
        server.abort();
        assert_eq!(result.text, "hello");
        assert_eq!(result.usage.prompt_tokens, 12);
        let requests = requests.lock().unwrap();
        assert_eq!(requests.len(), 3);
        assert!(requests.windows(2).all(|pair| pair[0] == pair[1]));
        assert_eq!(requests[0]["scope"]["guild_id"], "1");
    }

    #[test]
    fn image_model_is_omitted_when_off_and_images_default_to_none() {
        let mut request = serde_json::to_value(job()).unwrap();
        assert!(request.get("image_model").is_none());
        let mut with_images = job();
        with_images.image_model = Some("test/image".into());
        request = serde_json::to_value(with_images).unwrap();
        assert_eq!(request["image_model"], "test/image");

        let result: JobResult = serde_json::from_value(json!({
            "text": "hi", "model": null, "steps": 1, "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }))
        .unwrap();
        assert!(result.images.is_empty());
        let result: JobResult = serde_json::from_value(json!({
            "text": "hi", "model": null, "steps": 1, "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            "images": [{"id": 7, "mime_type": "image/png"}],
        }))
        .unwrap();
        assert_eq!(result.images[0].id, 7);
    }

    #[tokio::test]
    async fn drawings_are_durable_jobs_on_their_own_endpoint() {
        type Requests = Arc<Mutex<Vec<Value>>>;
        async fn handler(
            State(requests): State<Requests>,
            Json(body): Json<Value>,
        ) -> axum::response::Response {
            let mut requests = requests.lock().unwrap();
            requests.push(body);
            match requests.len() {
                1 => (
                    StatusCode::ACCEPTED,
                    Json(json!({"id": "image:42:1", "status": "running"})),
                )
                    .into_response(),
                2 => Json(
                    json!({"id": "image:42:1", "status": "completed", "result": {
                        "image": {"id": 7, "mime_type": "image/png"}, "caption": "da beach",
                        "prompt": "…", "references": [], "model": "test/image", "cost": 0.05,
                        "latency_ms": 12000, "error": null, "failure_note": "",
                    }}),
                )
                .into_response(),
                // Neither an image nor a reason: rejected.
                _ => Json(
                    json!({"id": "image:42:1", "status": "completed", "result": {
                        "image": null, "model": "test/image", "error": null,
                    }}),
                )
                .into_response(),
            }
        }
        let requests = Requests::default();
        let (url, server) = server(
            Router::new()
                .route("/v1/images", post(handler))
                .with_state(requests.clone()),
        )
        .await;
        let host = host(url, Duration::from_secs(10));
        let job = ImageJobRequest {
            id: "image:42:1".into(),
            scope: Scope::new(1, 2, 3),
            image_model: "test/image".into(),
            reply_job: "reply:42".into(),
            chat_model: "test/model".into(),
            ack: "brb".into(),
            drawing: json!({"prompt": "Nougie on a beach"}),
        };
        let drawn = host.draw(&job).await.unwrap();
        assert_eq!(drawn.image.unwrap().id, 7);
        assert_eq!(drawn.caption, "da beach");
        assert!(host.draw(&job).await.is_err());
        server.abort();
        let requests = requests.lock().unwrap();
        assert_eq!(requests[0], requests[1], "retries send identical input");
        assert_eq!(requests[0]["drawing"]["prompt"], "Nougie on a beach");
    }

    #[test]
    fn queued_drawings_pass_through_and_default_to_none() {
        let result: JobResult = serde_json::from_value(json!({
            "text": "on it", "model": null, "steps": 1, "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            "drawings": [{"prompt": "x", "subjects": [{"name": "Nougie"}], "caption": "hi"}],
        }))
        .unwrap();
        assert_eq!(result.drawings[0]["subjects"][0]["name"], "Nougie");
        let result: JobResult = serde_json::from_value(json!({
            "text": "hi", "model": null, "steps": 1, "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }))
        .unwrap();
        assert!(result.drawings.is_empty());
    }

    #[tokio::test]
    async fn unconfigured_executor_fails_without_retrying() {
        let (url, server) =
            server(Router::new().route("/v1/jobs", post(|| async { StatusCode::NOT_IMPLEMENTED })))
                .await;
        let error = host(url, Duration::from_secs(1))
            .execute(&job())
            .await
            .err()
            .unwrap();
        server.abort();
        assert!(error.to_string().contains("no framework executor"));
    }

    #[tokio::test]
    async fn cold_start_retries_respect_total_deadline() {
        let (url, server) =
            server(Router::new().route("/v1/jobs", post(|| async { StatusCode::BAD_GATEWAY })))
                .await;
        let error = host(url, Duration::from_millis(50))
            .execute(&job())
            .await
            .err()
            .unwrap();
        server.abort();
        assert!(error.to_string().contains("deadline exceeded"));
    }

    #[tokio::test]
    async fn describe_emojis_retries_wake_up_then_parses() {
        type Calls = Arc<Mutex<usize>>;
        async fn handler(State(calls): State<Calls>) -> axum::response::Response {
            let mut calls = calls.lock().unwrap();
            *calls += 1;
            match *calls {
                1 => StatusCode::SERVICE_UNAVAILABLE.into_response(),
                _ => Json(json!({"described": 2, "failed": 1, "remaining": 0, "cost": 0.01}))
                    .into_response(),
            }
        }
        let request = DescribeEmojis {
            guild_id: "1".into(),
            model: "test/vision".into(),
            attempted_before_ms: 0,
            redescribe: false,
        };
        for (status, calls) in [(None, 2), (Some(StatusCode::NOT_IMPLEMENTED), 1)] {
            let counted = Calls::default();
            let route = match status {
                None => post(handler),
                Some(status) => post(move |State(calls): State<Calls>| async move {
                    *calls.lock().unwrap() += 1;
                    status
                }),
            };
            let (url, server) = server(
                Router::new()
                    .route("/v1/emojis/describe", route)
                    .with_state(counted.clone()),
            )
            .await;
            let result = host(url, Duration::from_secs(10))
                .describe_emojis(&request)
                .await;
            server.abort();
            match result {
                Ok(described) => {
                    assert_eq!((described.described, described.remaining), (2, 0));
                }
                Err(error) => assert!(error.to_string().contains("no framework executor")),
            }
            assert_eq!(*counted.lock().unwrap(), calls);
        }
    }
}
