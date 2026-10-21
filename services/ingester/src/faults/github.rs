//! GitHub's REST API, for the issues the fault reporter files.
//!
//! On with either credential (docs/observability.md, "Fault issues"):
//! - `GITHUB_TOKEN`: a fine-grained personal access token for the repo, with
//!   Issues read and write.
//! - `GITHUB_APP_ID`, `GITHUB_APP_INSTALLATION_ID` and `GITHUB_APP_PRIVATE_KEY`
//!   (the PEM, or the PEM in base64): a GitHub App with Issues read and write,
//!   installed on the repo. Installation tokens are made as needed.
//!
//! `GITHUB_ISSUES_REPO` (`owner/name`) is where issues go; `GITHUB_API_URL` is
//! overridden only by tests and the simulator.

use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use anyhow::{Context, anyhow};
use base64::{
    Engine,
    engine::general_purpose::{STANDARD, URL_SAFE_NO_PAD},
};
use reqwest::{Method, StatusCode, header};
use ring::{
    rand::SystemRandom,
    signature::{self, RsaKeyPair},
};
use serde::Deserialize;
use serde_json::{Value, json};

const TIMEOUT: Duration = Duration::from_secs(10);
const DEFAULT_API: &str = "https://api.github.com";
const DEFAULT_REPO: &str = "carltonwu/kiki";
/// Installation tokens last an hour; a new one is made before that.
const INSTALLATION_TOKEN_TTL: Duration = Duration::from_secs(50 * 60);

#[derive(Debug)]
pub enum Error {
    /// The credential was refused or lacks a permission: trying again won't help.
    Unauthorized(String),
    /// Rate limited, with how long GitHub asked to wait.
    RateLimited(Option<Duration>),
    /// No such issue (deleted, or moved to another repo).
    NotFound,
    /// The request was refused as invalid (422), with GitHub's reason.
    Invalid(String),
    Other(anyhow::Error),
}

impl From<anyhow::Error> for Error {
    fn from(e: anyhow::Error) -> Self {
        Self::Other(e)
    }
}

impl std::fmt::Display for Error {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Unauthorized(m) => write!(f, "GitHub refused the credential: {m}"),
            Self::RateLimited(_) => write!(f, "GitHub rate limited the request"),
            Self::NotFound => write!(f, "GitHub has no such issue"),
            Self::Invalid(m) => write!(f, "GitHub refused the request: {m}"),
            Self::Other(e) => write!(f, "{e:#}"),
        }
    }
}

#[derive(Debug, Clone, Deserialize, PartialEq)]
pub struct Issue {
    pub number: i32,
    /// `open` or `closed`.
    pub state: String,
    /// `completed`, `not_planned`, `reopened` or `duplicate`, if any.
    pub state_reason: Option<String>,
}

impl Issue {
    pub fn not_planned(&self) -> bool {
        self.state == "closed"
            && matches!(
                self.state_reason.as_deref(),
                Some("not_planned" | "duplicate")
            )
    }
}

enum Credential {
    Token(header::HeaderValue),
    App {
        app_id: String,
        installation_id: String,
        key: Box<RsaKeyPair>,
        /// The current installation token, and when to make a new one.
        token: Option<(header::HeaderValue, Instant)>,
    },
}

pub struct Github {
    http: reqwest::Client,
    api: String,
    repo: String,
    credential: Credential,
}

impl Github {
    /// `None` without a credential. Errors if one is set but unusable.
    pub fn from_env(env: impl Fn(&str) -> Option<String>) -> anyhow::Result<Option<Self>> {
        let env = |name: &str| {
            env(name)
                .map(|v| v.trim().to_owned())
                .filter(|v| !v.is_empty())
        };
        let app = (
            env("GITHUB_APP_ID"),
            env("GITHUB_APP_INSTALLATION_ID"),
            env("GITHUB_APP_PRIVATE_KEY"),
        );
        let credential = match (env("GITHUB_TOKEN"), app) {
            (Some(token), _) => Credential::Token(bearer(&token).context("invalid GITHUB_TOKEN")?),
            (None, (Some(app_id), Some(installation_id), Some(key))) => Credential::App {
                app_id,
                installation_id,
                key: Box::new(private_key(&key)?),
                token: None,
            },
            (None, (None, None, None)) => return Ok(None),
            (None, _) => anyhow::bail!(
                "set all of GITHUB_APP_ID, GITHUB_APP_INSTALLATION_ID and GITHUB_APP_PRIVATE_KEY, or GITHUB_TOKEN"
            ),
        };
        let repo = env("GITHUB_ISSUES_REPO").unwrap_or_else(|| DEFAULT_REPO.into());
        let valid = |part: &str| {
            !part.is_empty()
                && part
                    .chars()
                    .all(|c| c.is_ascii_alphanumeric() || "-_.".contains(c))
        };
        if !matches!(repo.split_once('/'), Some((owner, name)) if valid(owner) && valid(name)) {
            anyhow::bail!("GITHUB_ISSUES_REPO must be owner/name");
        }
        let mut headers = header::HeaderMap::new();
        headers.insert(
            header::ACCEPT,
            header::HeaderValue::from_static("application/vnd.github+json"),
        );
        headers.insert(
            "X-GitHub-Api-Version",
            header::HeaderValue::from_static("2022-11-28"),
        );
        // GitHub refuses requests without one.
        headers.insert(
            header::USER_AGENT,
            header::HeaderValue::from_static("kiki-ingester"),
        );
        let http = reqwest::Client::builder()
            .default_headers(headers)
            .timeout(TIMEOUT)
            .redirect(reqwest::redirect::Policy::none())
            .build()?;
        Ok(Some(Self {
            http,
            api: env("GITHUB_API_URL")
                .unwrap_or_else(|| DEFAULT_API.into())
                .trim_end_matches('/')
                .to_owned(),
            repo,
            credential,
        }))
    }

    pub fn repo(&self) -> &str {
        &self.repo
    }

    /// Creates `name` unless the repo has it already.
    pub async fn ensure_label(
        &mut self,
        name: &str,
        color: &str,
        description: &str,
    ) -> Result<(), Error> {
        let path = format!("/repos/{}/labels", self.repo);
        let body = json!({ "name": name, "color": color, "description": description });
        match self.send(Method::POST, &path, Some(&body), &[]).await {
            Ok(_) => Ok(()),
            // `already_exists`
            Err(Error::Invalid(_)) => Ok(()),
            Err(e) => Err(e),
        }
    }

    /// The issue whose body shows `fingerprint`: an open one first, else the
    /// newest. Search matches words, so the body is checked too.
    pub async fn find(&mut self, fingerprint: &str) -> Result<Option<Issue>, Error> {
        let query = format!(
            "repo:{} is:issue label:kiki-fault in:body {fingerprint}",
            self.repo
        );
        let found = self
            .send(
                Method::GET,
                "/search/issues",
                None,
                &[("q", &query), ("sort", "created"), ("order", "desc")],
            )
            .await?;
        #[derive(Deserialize)]
        struct Item {
            #[serde(flatten)]
            issue: Issue,
            body: Option<String>,
        }
        #[derive(Deserialize)]
        struct Found {
            items: Vec<Item>,
        }
        let found: Found =
            serde_json::from_value(found).context("unexpected GitHub search response")?;
        let mut matching = found
            .items
            .into_iter()
            .filter(|i| i.body.as_deref().is_some_and(|b| b.contains(fingerprint)))
            .map(|i| i.issue);
        let first = matching.next();
        let open = first
            .iter()
            .cloned()
            .chain(matching)
            .find(|i| i.state == "open");
        Ok(open.or(first))
    }

    pub async fn create(
        &mut self,
        title: &str,
        body: &str,
        labels: &[&str],
    ) -> Result<Issue, Error> {
        let path = format!("/repos/{}/issues", self.repo);
        let issue = json!({ "title": title, "body": body, "labels": labels });
        let created = self.send(Method::POST, &path, Some(&issue), &[]).await?;
        Ok(serde_json::from_value(created).context("unexpected GitHub issue response")?)
    }

    pub async fn get(&mut self, number: i32) -> Result<Issue, Error> {
        let path = format!("/repos/{}/issues/{number}", self.repo);
        let issue = self.send(Method::GET, &path, None, &[]).await?;
        Ok(serde_json::from_value(issue).context("unexpected GitHub issue response")?)
    }

    pub async fn comment(&mut self, number: i32, body: &str) -> Result<(), Error> {
        let path = format!("/repos/{}/issues/{number}/comments", self.repo);
        self.send(Method::POST, &path, Some(&json!({ "body": body })), &[])
            .await
            .map(drop)
    }

    async fn send(
        &mut self,
        method: Method,
        path: &str,
        body: Option<&Value>,
        query: &[(&str, &str)],
    ) -> Result<Value, Error> {
        let mut retried = false;
        loop {
            let authorization = self.authorization().await?;
            let url = reqwest::Url::parse_with_params(&format!("{}{path}", self.api), query)
                .context("invalid GitHub URL")?;
            let mut request = self
                .http
                .request(method.clone(), url)
                .header(header::AUTHORIZATION, authorization);
            if let Some(body) = body {
                request = request.json(body);
            }
            let response = request.send().await.context("GitHub request failed")?;
            let status = response.status();
            // An installation token can be revoked before it expires: one retry
            // with a new one.
            if status == StatusCode::UNAUTHORIZED
                && !retried
                && let Credential::App { token, .. } = &mut self.credential
            {
                *token = None;
                retried = true;
                continue;
            }
            return read(response).await;
        }
    }

    async fn authorization(&mut self) -> Result<header::HeaderValue, Error> {
        let (app_id, installation_id, key, token) = match &mut self.credential {
            Credential::Token(value) => return Ok(value.clone()),
            Credential::App {
                app_id,
                installation_id,
                key,
                token,
            } => (app_id, installation_id, key, token),
        };
        if let Some((value, until)) = token
            && Instant::now() < *until
        {
            return Ok(value.clone());
        }
        let now = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_err(|e| anyhow!(e))?
            .as_secs();
        let jwt = bearer(&app_jwt(app_id, key, now)?)?;
        let response = self
            .http
            .post(format!(
                "{}/app/installations/{installation_id}/access_tokens",
                self.api
            ))
            .header(header::AUTHORIZATION, jwt)
            .send()
            .await
            .context("GitHub installation token request failed")?;
        #[derive(Deserialize)]
        struct Token {
            token: String,
        }
        let made: Token = serde_json::from_value(read(response).await?)
            .context("unexpected GitHub installation token response")?;
        let value = bearer(&made.token)?;
        *token = Some((value.clone(), Instant::now() + INSTALLATION_TOKEN_TTL));
        Ok(value)
    }
}

/// The response's JSON, or the error its status means. GitHub's error
/// messages are kept (they're about the request, like "Validation Failed"),
/// never the request.
async fn read(response: reqwest::Response) -> Result<Value, Error> {
    let status = response.status();
    let header = |name: &str| {
        response
            .headers()
            .get(name)
            .and_then(|v| v.to_str().ok())
            .map(str::to_owned)
    };
    let retry_after = header("retry-after")
        .and_then(|s| s.parse().ok())
        .map(Duration::from_secs);
    let exhausted = header("x-ratelimit-remaining").as_deref() == Some("0");
    let body = response
        .text()
        .await
        .context("reading the GitHub response")?;
    if status.is_success() {
        return Ok(if body.is_empty() {
            Value::Null
        } else {
            serde_json::from_str(&body).context("unexpected GitHub response")?
        });
    }
    let message = serde_json::from_str::<Value>(&body)
        .ok()
        .and_then(|v| {
            v.get("message")?
                .as_str()
                .map(|m| m.chars().take(200).collect())
        })
        .unwrap_or_default();
    Err(match status {
        StatusCode::TOO_MANY_REQUESTS => Error::RateLimited(retry_after),
        // GitHub's rate limits answer 403 too, with these headers.
        StatusCode::FORBIDDEN if exhausted || retry_after.is_some() => {
            Error::RateLimited(retry_after)
        }
        StatusCode::UNAUTHORIZED | StatusCode::FORBIDDEN => {
            Error::Unauthorized(format!("HTTP {status}: {message}"))
        }
        StatusCode::NOT_FOUND | StatusCode::GONE => Error::NotFound,
        StatusCode::UNPROCESSABLE_ENTITY => Error::Invalid(message),
        _ => Error::Other(anyhow!("GitHub returned HTTP {status}: {message}")),
    })
}

fn bearer(token: &str) -> anyhow::Result<header::HeaderValue> {
    let mut value = header::HeaderValue::from_str(&format!("Bearer {token}"))?;
    value.set_sensitive(true);
    Ok(value)
}

/// The App's key: a PEM (PKCS#1, as GitHub downloads it, or PKCS#8), as is,
/// with `\n` escapes, or in base64.
fn private_key(value: &str) -> anyhow::Result<RsaKeyPair> {
    let pem = if value.contains("-----BEGIN") {
        value.to_owned()
    } else {
        let compact: String = value.split_whitespace().collect();
        String::from_utf8(
            STANDARD
                .decode(compact)
                .context("GITHUB_APP_PRIVATE_KEY is neither a PEM nor a base64 PEM")?,
        )
        .context("GITHUB_APP_PRIVATE_KEY is neither a PEM nor a base64 PEM")?
    };
    // -----BEGIN <label>-----<base64>-----END <label>-----
    let parts: Vec<&str> = pem.split("-----").collect();
    let (Some(label), Some(encoded)) = (parts.get(1), parts.get(2)) else {
        anyhow::bail!("GITHUB_APP_PRIVATE_KEY is not a PEM");
    };
    let encoded: String = encoded.replace("\\n", "").split_whitespace().collect();
    let der = STANDARD
        .decode(encoded)
        .context("GITHUB_APP_PRIVATE_KEY is not a valid PEM")?;
    let key = if label.contains("RSA PRIVATE KEY") {
        RsaKeyPair::from_der(&der)
    } else {
        RsaKeyPair::from_pkcs8(&der)
    };
    key.map_err(|e| anyhow!("GITHUB_APP_PRIVATE_KEY is not an RSA private key: {e}"))
}

/// The JWT that asks for an installation token: RS256, valid for nine minutes
/// from a minute ago (for clock drift), as GitHub's docs say.
fn app_jwt(app_id: &str, key: &RsaKeyPair, now: u64) -> anyhow::Result<String> {
    // The App ID is a number; a client ID (`Iv1.…`) works too, as a string.
    let issuer = app_id
        .parse::<u64>()
        .map(Value::from)
        .unwrap_or_else(|_| Value::from(app_id));
    let header = URL_SAFE_NO_PAD.encode(r#"{"alg":"RS256","typ":"JWT"}"#);
    let claims = URL_SAFE_NO_PAD.encode(
        json!({ "iat": now.saturating_sub(60), "exp": now + 540, "iss": issuer }).to_string(),
    );
    let message = format!("{header}.{claims}");
    let mut signature = vec![0; key.public().modulus_len()];
    key.sign(
        &signature::RSA_PKCS1_SHA256,
        &SystemRandom::new(),
        message.as_bytes(),
        &mut signature,
    )
    .map_err(|_| anyhow!("signing the GitHub App JWT failed"))?;
    Ok(format!("{message}.{}", URL_SAFE_NO_PAD.encode(signature)))
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;
    use ring::signature::KeyPair;

    /// A throwaway 2048-bit key for these tests only (`openssl genrsa -traditional`).
    pub const TEST_KEY: &str = include_str!("test_app_key.pem");

    pub fn env(vars: &'static [(&'static str, &'static str)]) -> impl Fn(&str) -> Option<String> {
        move |name: &str| {
            vars.iter()
                .find(|(k, _)| *k == name)
                .map(|(_, v)| v.to_string())
        }
    }

    #[test]
    fn needs_a_whole_credential_and_a_repo() {
        let _ = rustls::crypto::ring::default_provider().install_default();
        assert!(Github::from_env(env(&[])).unwrap().is_none());
        assert!(
            Github::from_env(env(&[("GITHUB_TOKEN", " ")]))
                .unwrap()
                .is_none()
        );
        let github = Github::from_env(env(&[("GITHUB_TOKEN", "t")]))
            .unwrap()
            .unwrap();
        assert_eq!(
            (github.repo(), github.api.as_str()),
            (DEFAULT_REPO, DEFAULT_API)
        );
        assert!(Github::from_env(env(&[("GITHUB_APP_ID", "1")])).is_err());
        assert!(
            Github::from_env(env(&[
                ("GITHUB_TOKEN", "t"),
                ("GITHUB_ISSUES_REPO", "kiki")
            ]))
            .is_err()
        );
        assert!(
            Github::from_env(env(&[
                ("GITHUB_APP_ID", "1"),
                ("GITHUB_APP_INSTALLATION_ID", "2"),
                ("GITHUB_APP_PRIVATE_KEY", "not a key"),
            ]))
            .is_err()
        );
    }

    #[test]
    fn reads_the_key_as_downloaded_escaped_or_in_base64() {
        assert!(private_key(TEST_KEY).is_ok());
        assert!(private_key(&TEST_KEY.replace('\n', "\\n")).is_ok());
        assert!(private_key(&STANDARD.encode(TEST_KEY)).is_ok());
    }

    #[test]
    fn app_jwts_are_signed_with_the_key() {
        let key = private_key(TEST_KEY).unwrap();
        let jwt = app_jwt("12345", &key, 1_000_000).unwrap();
        let (message, signature) = jwt.rsplit_once('.').unwrap();
        let claims: Value = serde_json::from_slice(
            &URL_SAFE_NO_PAD
                .decode(message.split('.').nth(1).unwrap())
                .unwrap(),
        )
        .unwrap();
        assert_eq!(
            claims,
            json!({ "iat": 999_940, "exp": 1_000_540, "iss": 12345 })
        );
        signature::UnparsedPublicKey::new(
            &signature::RSA_PKCS1_2048_8192_SHA256,
            key.public_key().as_ref(),
        )
        .verify(
            message.as_bytes(),
            &URL_SAFE_NO_PAD.decode(signature).unwrap(),
        )
        .unwrap();
    }
}
