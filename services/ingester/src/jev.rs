//! Client for TypeSafe's Jev ("System One") decision model, called through
//! OpenRouter's Decisions API, which takes the same `state` + `questions` request
//! as TypeSafe's own API.
//! https://openrouter.ai/docs/cookbook/evaluate-and-optimize/jev-classification

use std::time::{Duration, Instant};

use anyhow::Context;
use serde::Deserialize;
use serde_json::{Map, Value, json};
use tracing::{Instrument, Span, field::Empty, info_span};

use crate::metrics::{self, LlmResult};

pub struct Jev {
    http: reqwest::Client,
    url: String,
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
            url: format!("{}/alpha/decisions", crate::config::openrouter_base()),
            api_key,
            model,
        })
    }

    /// Asks Jev, recording the call (`kiki.llm.*`, purpose `jev`) and a span.
    pub async fn choose(
        &self,
        state: Value,
        instructions: &str,
        criteria: Map<String, Value>,
    ) -> anyhow::Result<Choice> {
        let span = info_span!(
            "jev",
            otel.kind = "client",
            model = self.model,
            choice = Empty,
            confidence = Empty,
            result = Empty,
        );
        async {
            let started = Instant::now();
            let choice = self.request(state, instructions, criteria).await;
            let result = match &choice {
                Ok(_) => LlmResult::Ok,
                Err(e) if timed_out(e) => LlmResult::Timeout,
                Err(_) => LlmResult::Error,
            };
            let (model, cost) = match &choice {
                Ok(c) => (c.model.as_deref().unwrap_or(&self.model), c.cost),
                Err(_) => (self.model.as_str(), None),
            };
            metrics::llm_call("jev", model, result, started.elapsed(), cost);
            let span = Span::current();
            span.record("result", format!("{result:?}").to_lowercase());
            if let Ok(c) = &choice {
                metrics::jev_confidence(c.confidence);
                span.record("choice", c.choice.as_str());
                span.record("confidence", c.confidence);
            }
            choice
        }
        .instrument(span)
        .await
    }

    async fn request(
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
            .post(&self.url)
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

fn timed_out(error: &anyhow::Error) -> bool {
    error.chain().any(|e| {
        e.downcast_ref::<reqwest::Error>()
            .is_some_and(reqwest::Error::is_timeout)
    })
}
