//! Client for TypeSafe's Jev ("System One") decision model, called through
//! OpenRouter's Decisions API, which takes the same `state` + `questions` request
//! as TypeSafe's own API.
//! https://openrouter.ai/docs/cookbook/evaluate-and-optimize/jev-classification

use std::time::Duration;

use anyhow::Context;
use serde::Deserialize;
use serde_json::{Map, Value, json};

const URL: &str = "https://openrouter.ai/api/alpha/decisions";

pub struct Jev {
    http: reqwest::Client,
    api_key: String,
    model: String,
}

#[derive(Debug)]
pub struct Choice {
    pub choice: String,
    /// 0 to 1, from how peaked the probabilities are.
    pub confidence: f64,
    pub model: Option<String>,
    pub cost: Option<f64>,
}

#[derive(Deserialize)]
struct Response {
    model: Option<String>,
    answers: Map<String, Value>,
    usage: Option<Usage>,
}

#[derive(Deserialize)]
struct Usage {
    cost: Option<f64>,
}

#[derive(Deserialize)]
struct ChoiceAnswer {
    choice: String,
    confidence: Option<f64>,
}

impl Jev {
    pub fn new(api_key: String, model: String, timeout: Duration) -> anyhow::Result<Self> {
        let http = reqwest::Client::builder()
            .timeout(timeout)
            .build()
            .context("building Jev HTTP client")?;
        Ok(Self {
            http,
            api_key,
            model,
        })
    }

    pub async fn choose(
        &self,
        state: Value,
        instructions: &str,
        criteria: Map<String, Value>,
    ) -> anyhow::Result<Choice> {
        let body = json!({
            "model": self.model,
            "state": state,
            "questions": {
                "intent": { "type": "choice", "instructions": instructions, "criteria": criteria },
            },
        });
        let response = self
            .http
            .post(URL)
            .bearer_auth(&self.api_key)
            .header("HTTP-Referer", "https://github.com/carltonwu/kiki")
            .header("X-Title", "Kiki")
            .json(&body)
            .send()
            .await
            .context("calling Jev")?;
        let status = response.status();
        if !status.is_success() {
            // Provider error bodies can echo private prompts or credentials.
            anyhow::bail!("Jev returned HTTP {status}");
        }
        let mut response: Response = response.json().await.context("parsing Jev response")?;
        let answer = response
            .answers
            .remove("intent")
            .context("Jev response has no answer")?;
        let answer: ChoiceAnswer = serde_json::from_value(answer).context("parsing Jev choice")?;
        Ok(Choice {
            choice: answer.choice,
            // Optional in OpenRouter's schema; missing means we can't trust it.
            confidence: answer.confidence.unwrap_or(0.0),
            model: response.model,
            cost: response.usage.and_then(|u| u.cost),
        })
    }
}
