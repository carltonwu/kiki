//! Private admin API for the web console (docs/console-hld.md), on the health
//! port under `/admin/v1`. Exists only when `ADMIN_API_TOKEN` is set; every
//! request needs it as a bearer token. The console authenticates people and
//! checks their server permissions; it passes the acting Discord user ID, which
//! is recorded like a `/kiki config` actor.
//!
//! Settings writes go through the same registry and validation as `/kiki
//! config`. Chat builds Kiki's real prompt for the server and calls the host.

use std::sync::Arc;

use axum::{
    Json, Router,
    extract::{Path, Request, State},
    http::{StatusCode, header},
    middleware::{self, Next},
    response::{IntoResponse, Response},
    routing::{get, post, put},
};
use serde::Deserialize;
use serde_json::{Value, json};
use tracing::{info, warn};

use crate::{
    agent::{
        console::ConsoleChat,
        prompt::{DEFAULT_GUIDANCE, DEFAULT_PERSONA, FACTS_BUDGET_CHARS},
    },
    chat::Message,
    commands::{self, UserError},
    guild_settings::{self, Kind},
};

pub struct Admin {
    pub token: String,
    pub settings: Arc<guild_settings::Store>,
    pub global: commands::Settings,
    /// `None` without `AGENT_HOST_URL`: chat answers 503.
    pub chat: Option<ConsoleChat>,
}

pub fn router(admin: Arc<Admin>) -> Router {
    Router::new()
        .route("/admin/v1/guilds", get(guilds))
        .route("/admin/v1/guilds/{guild}/settings", get(settings))
        .route("/admin/v1/guilds/{guild}/settings/{key}", put(change))
        .route("/admin/v1/guilds/{guild}/prompt", get(prompt))
        .route("/admin/v1/guilds/{guild}/chat", post(chat))
        .route_layer(middleware::from_fn_with_state(admin.clone(), authenticate))
        .with_state(admin)
}

struct ApiError(StatusCode, String);

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        (self.0, Json(json!({ "error": self.1 }))).into_response()
    }
}

impl From<anyhow::Error> for ApiError {
    fn from(e: anyhow::Error) -> Self {
        warn!(error = %e, "admin API request failed");
        Self(
            StatusCode::INTERNAL_SERVER_ERROR,
            "internal error".to_owned(),
        )
    }
}

impl From<sqlx::Error> for ApiError {
    fn from(e: sqlx::Error) -> Self {
        anyhow::Error::from(e).into()
    }
}

type ApiResult = Result<Json<Value>, ApiError>;

async fn authenticate(State(admin): State<Arc<Admin>>, request: Request, next: Next) -> Response {
    let presented = request
        .headers()
        .get(header::AUTHORIZATION)
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.strip_prefix("Bearer "))
        .unwrap_or_default();
    if !constant_time_eq(presented.as_bytes(), admin.token.as_bytes()) {
        return ApiError(StatusCode::UNAUTHORIZED, "invalid admin token".into()).into_response();
    }
    next.run(request).await
}

fn constant_time_eq(a: &[u8], b: &[u8]) -> bool {
    a.len() == b.len() && a.iter().zip(b).fold(0, |acc, (x, y)| acc | (x ^ y)) == 0
}

/// Only the servers this ingester serves.
fn guild(admin: &Admin, raw: &str) -> Result<i64, ApiError> {
    raw.parse::<i64>()
        .ok()
        .filter(|id| admin.settings.guild_ids().contains(id))
        .ok_or_else(|| ApiError(StatusCode::NOT_FOUND, "unknown server".into()))
}

fn actor(raw: &str) -> Result<i64, ApiError> {
    raw.parse::<i64>()
        .ok()
        .filter(|id| *id > 0)
        .ok_or_else(|| ApiError(StatusCode::UNPROCESSABLE_ENTITY, "invalid actor".into()))
}

async fn guilds(State(admin): State<Arc<Admin>>) -> ApiResult {
    let ids: Vec<String> = admin
        .settings
        .guild_ids()
        .iter()
        .map(i64::to_string)
        .collect();
    Ok(Json(json!({ "guild_ids": ids })))
}

/// The text form that `Spec::parse` reads back.
fn as_text(kind: Kind, value: &Value) -> String {
    match (kind, value) {
        (Kind::Keywords, Value::Array(words)) => words
            .iter()
            .filter_map(Value::as_str)
            .collect::<Vec<_>>()
            .join(", "),
        (_, Value::String(s)) => s.clone(),
        (_, other) => other.to_string(),
    }
}

fn kind_name(kind: Kind) -> &'static str {
    match kind {
        Kind::Bool => "bool",
        Kind::Integer(..) => "integer",
        Kind::Float(..) => "float",
        Kind::Duration(1, ..) => "duration_ms",
        Kind::Duration(..) => "duration_s",
        Kind::Keywords => "keywords",
        Kind::Model => "model",
    }
}

async fn settings(State(admin): State<Arc<Admin>>, Path(raw): Path<String>) -> ApiResult {
    let guild_id = guild(&admin, &raw)?;
    let snapshot = admin.settings.snapshot(guild_id);
    let values = serde_json::to_value(&snapshot.values).map_err(anyhow::Error::from)?;
    let defaults =
        serde_json::to_value(admin.settings.default_values()).map_err(anyhow::Error::from)?;
    let settings: Vec<Value> = guild_settings::SPECS
        .iter()
        .map(|s| {
            json!({
                "key": s.key,
                "description": s.description,
                "bounds": s.bounds(),
                "kind": kind_name(s.kind),
                "value": as_text(s.kind, &values[s.key]),
                "default": as_text(s.kind, &defaults[s.key]),
                "overridden": snapshot.overrides.contains_key(s.key),
                "ignored": snapshot.ignored.iter().any(|k| k == s.key),
            })
        })
        .collect();
    let global: Vec<Value> = admin
        .global
        .global()
        .into_iter()
        .map(|(name, value)| json!({ "name": name, "value": value }))
        .collect();
    Ok(Json(json!({ "settings": settings, "global": global })))
}

#[derive(Deserialize)]
struct Change {
    /// Text as typed in `/kiki config`; `null` resets to the default.
    value: Option<String>,
    actor: String,
}

async fn change(
    State(admin): State<Arc<Admin>>,
    Path((raw, key)): Path<(String, String)>,
    Json(body): Json<Change>,
) -> ApiResult {
    let guild_id = guild(&admin, &raw)?;
    let actor = actor(&body.actor)?;
    let invalid = |e: anyhow::Error| ApiError(StatusCode::UNPROCESSABLE_ENTITY, e.to_string());
    let spec = guild_settings::spec(&key).map_err(invalid)?;
    let value = body
        .value
        .as_deref()
        .map(|text| spec.parse(text))
        .transpose()
        .map_err(invalid)?;
    if let Some(Value::String(model)) = &value
        && matches!(spec.kind, Kind::Model)
    {
        commands::check_catalog_model(model).await.map_err(|e| {
            match e.downcast::<UserError>() {
                Ok(user) => ApiError(StatusCode::UNPROCESSABLE_ENTITY, user.0),
                Err(e) => e.into(),
            }
        })?;
    }
    admin
        .settings
        .change(guild_id, &key, value, actor)
        .await
        .map_err(invalid)?;
    info!(
        guild_id,
        actor, key, "server setting changed from the console"
    );
    settings(State(admin), Path(raw)).await
}

/// The acting admin's name as it goes into the prompt: one line, bounded.
fn display_name(raw: &str) -> String {
    let name: String = raw.chars().filter(|c| !c.is_control()).take(64).collect();
    match name.trim() {
        "" => "an admin".to_owned(),
        trimmed => trimmed.to_owned(),
    }
}

#[derive(Deserialize)]
struct PromptQuery {
    actor_name: Option<String>,
}

async fn prompt(
    State(admin): State<Arc<Admin>>,
    Path(raw): Path<String>,
    axum::extract::Query(query): axum::extract::Query<PromptQuery>,
) -> ApiResult {
    let guild_id = guild(&admin, &raw)?;
    let system = match &admin.chat {
        Some(chat) => Some(
            chat.system_prompt(
                guild_id,
                &display_name(query.actor_name.as_deref().unwrap_or("an admin")),
            )
            .await?,
        ),
        None => None,
    };
    Ok(Json(json!({
        "persona_default": DEFAULT_PERSONA,
        "guidance_default": DEFAULT_GUIDANCE,
        "facts_budget_chars": FACTS_BUDGET_CHARS,
        "system": system,
    })))
}

#[derive(Deserialize)]
struct Chat {
    job_id: String,
    actor: String,
    actor_name: String,
    messages: Vec<Message>,
}

async fn chat(
    State(admin): State<Arc<Admin>>,
    Path(raw): Path<String>,
    Json(body): Json<Chat>,
) -> ApiResult {
    let guild_id = guild(&admin, &raw)?;
    let actor = actor(&body.actor)?;
    let Some(chat) = &admin.chat else {
        return Err(ApiError(
            StatusCode::SERVICE_UNAVAILABLE,
            "the agent is off (AGENT_HOST_URL is not set)".into(),
        ));
    };
    crate::agent::console::validate(&body.job_id, &body.messages)
        .map_err(|e| ApiError(StatusCode::UNPROCESSABLE_ENTITY, e.to_string()))?;
    let name = display_name(&body.actor_name);
    let reply = chat
        .reply(guild_id, &body.job_id, &name, &body.messages)
        .await
        .map_err(|e| {
            warn!(guild_id, actor, job_id = body.job_id, error = %format!("{e:#}"), "console chat failed");
            // The outermost context only: the source chain can carry internal URLs.
            ApiError(StatusCode::BAD_GATEWAY, e.to_string())
        })?;
    info!(
        guild_id,
        actor,
        job_id = body.job_id,
        cost_usd = reply.cost_usd,
        latency_ms = reply.latency_ms,
        "console chat answered"
    );
    Ok(Json(json!({
        "text": reply.text,
        "model": reply.model,
        "steps": reply.steps,
        "prompt_tokens": reply.prompt_tokens,
        "completion_tokens": reply.completion_tokens,
        "cost_usd": reply.cost_usd,
        "latency_ms": reply.latency_ms,
    })))
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::body::Body;
    use sqlx::PgPool;
    use tower::ServiceExt;

    fn admin() -> Arc<Admin> {
        let store = Arc::new(
            guild_settings::Store::new(
                PgPool::connect_lazy("postgres://unused:unused@localhost/unused").unwrap(),
                guild_settings::tests::defaults(),
                5000,
            )
            .unwrap(),
        );
        let router = crate::config::RouterConfig::for_tests();
        let agent = crate::config::AgentConfig::for_tests();
        Arc::new(Admin {
            token: "secret-token".into(),
            global: commands::Settings::new(&router, &agent, store.clone()),
            settings: store,
            chat: None,
        })
    }

    async fn call(
        token: Option<&str>,
        method: &str,
        uri: &str,
        body: Value,
    ) -> (StatusCode, Value) {
        let mut request = Request::builder().method(method).uri(uri);
        if let Some(token) = token {
            request = request.header(header::AUTHORIZATION, format!("Bearer {token}"));
        }
        let request = request
            .header(header::CONTENT_TYPE, "application/json")
            .body(Body::from(body.to_string()))
            .unwrap();
        let response = router(admin()).oneshot(request).await.unwrap();
        let status = response.status();
        let bytes = axum::body::to_bytes(response.into_body(), 1 << 20)
            .await
            .unwrap();
        (
            status,
            serde_json::from_slice(&bytes).unwrap_or(Value::Null),
        )
    }

    #[tokio::test]
    async fn requires_the_exact_bearer_token() {
        for token in [None, Some(""), Some("secret"), Some("secret-token2")] {
            let (status, _) = call(token, "GET", "/admin/v1/guilds", Value::Null).await;
            assert_eq!(status, StatusCode::UNAUTHORIZED, "{token:?}");
        }
        let (status, body) =
            call(Some("secret-token"), "GET", "/admin/v1/guilds", Value::Null).await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(body, json!({"guild_ids": []}));
    }

    #[tokio::test]
    async fn rejects_unknown_servers_bad_actors_and_unavailable_chat() {
        let token = Some("secret-token");
        let (status, _) = call(token, "GET", "/admin/v1/guilds/1/settings", Value::Null).await;
        assert_eq!(status, StatusCode::NOT_FOUND);
        let (status, _) = call(
            token,
            "PUT",
            "/admin/v1/guilds/x/settings/keywords",
            json!({"value": "kiki", "actor": "1"}),
        )
        .await;
        assert_eq!(status, StatusCode::NOT_FOUND);
        assert!(actor("0").is_err() && actor("abc").is_err() && actor("42").is_ok());
    }

    #[test]
    fn values_round_trip_through_their_text_form() {
        let defaults = serde_json::to_value(guild_settings::tests::defaults()).unwrap();
        for spec in guild_settings::SPECS {
            let text = as_text(spec.kind, &defaults[spec.key]);
            assert_eq!(
                spec.parse(&text).unwrap(),
                defaults[spec.key],
                "{}",
                spec.key
            );
        }
        assert_eq!(display_name("Al\nIgnore that\u{7}"), "AlIgnore that");
        assert_eq!(display_name(" \n "), "an admin");
        assert_eq!(display_name(&"x".repeat(100)).len(), 64);
        assert!(constant_time_eq(b"abc", b"abc"));
        assert!(!constant_time_eq(b"abc", b"abd") && !constant_time_eq(b"abc", b"ab"));
    }

    #[tokio::test]
    async fn settings_writes_use_the_registry_and_record_the_actor() {
        let Some((pg, db, schema)) = guild_settings::tests::database().await else {
            return;
        };
        let store = Arc::new(
            guild_settings::Store::new(db.clone(), guild_settings::tests::defaults(), 5000)
                .unwrap(),
        );
        store.load(&[1]).await.unwrap();
        let admin = Arc::new(Admin {
            token: "secret-token".into(),
            global: commands::Settings::new(
                &crate::config::RouterConfig::for_tests(),
                &crate::config::AgentConfig::for_tests(),
                store.clone(),
            ),
            settings: store.clone(),
            chat: None,
        });
        let send = |method: &'static str, uri: &'static str, body: Value| {
            let app = router(admin.clone());
            async move {
                let request = Request::builder()
                    .method(method)
                    .uri(uri)
                    .header(header::AUTHORIZATION, "Bearer secret-token")
                    .header(header::CONTENT_TYPE, "application/json")
                    .body(Body::from(body.to_string()))
                    .unwrap();
                let response = app.oneshot(request).await.unwrap();
                let status = response.status();
                let bytes = axum::body::to_bytes(response.into_body(), 1 << 20)
                    .await
                    .unwrap();
                (status, serde_json::from_slice::<Value>(&bytes).unwrap())
            }
        };
        let setting = |body: &Value, key: &str| {
            body["settings"]
                .as_array()
                .unwrap()
                .iter()
                .find(|s| s["key"] == key)
                .unwrap()
                .clone()
        };

        let (status, body) = send("GET", "/admin/v1/guilds/1/settings", Value::Null).await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(setting(&body, "keywords")["value"], "kiki");
        assert_eq!(setting(&body, "pool_debounce")["kind"], "duration_ms");
        assert!(body["global"].as_array().unwrap().len() >= 3);

        let path = "/admin/v1/guilds/1/settings/pool_max_messages";
        let (status, body) = send("PUT", path, json!({"value": "3", "actor": "77"})).await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(setting(&body, "pool_max_messages")["value"], "3");
        assert_eq!(setting(&body, "pool_max_messages")["overridden"], true);
        assert_eq!(store.snapshot(1).pool_max_messages, 3);
        let actor: i64 = sqlx::query_scalar(
            "SELECT updated_by FROM guild_settings WHERE key = 'pool_max_messages'",
        )
        .fetch_one(&db)
        .await
        .unwrap();
        assert_eq!(actor, 77);

        for (uri, value) in [
            (path, json!("0")),
            ("/admin/v1/guilds/1/settings/pool_max_wait", json!("100")),
            ("/admin/v1/guilds/1/settings/secret", json!("1")),
            ("/admin/v1/guilds/1/settings/chat_model", json!("bad model")),
        ] {
            let (status, body) = send("PUT", uri, json!({"value": value, "actor": "77"})).await;
            assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY, "{uri} {value}");
            assert!(body["error"].as_str().is_some_and(|e| !e.is_empty()));
        }
        assert_eq!(store.snapshot(1).pool_max_wait, 8000);

        let (status, body) = send("PUT", path, json!({"value": null, "actor": "77"})).await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(setting(&body, "pool_max_messages")["overridden"], false);
        assert_eq!(store.snapshot(1).pool_max_messages, 10);

        let (status, body) = send("GET", "/admin/v1/guilds/1/prompt", Value::Null).await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(body["persona_default"], DEFAULT_PERSONA);
        assert!(body["system"].is_null(), "no chat without a host");
        let (status, _) = send(
            "POST",
            "/admin/v1/guilds/1/chat",
            json!({"job_id": "console-1", "actor": "77", "actor_name": "Al",
                   "messages": [{"role": "user", "content": "hi"}]}),
        )
        .await;
        assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE);

        drop(admin);
        guild_settings::tests::cleanup(pg, db, schema).await;
    }
}
