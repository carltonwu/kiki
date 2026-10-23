"""A server whose chat model is `refuseless/<id>` replies through Refuseless's
OpenAI-compatible API: without OpenRouter's web tools or settings, and with its
own thinking left on."""

import json

import httpx
import pytest
from openai import AsyncOpenAI
from pydantic_ai.providers.openai import OpenAIProvider

from kiki_agent_host.agent.executor import PydanticExecutor
from kiki_agent_host.contracts import JobRequest

MODEL = "refuseless/deepseek-v4.1-flash-abliterated"


def refuseless(requests: list[dict]) -> OpenAIProvider:
    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append({"url": str(request.url), **body})
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-1",
                "object": "chat.completion",
                "created": 0,
                "model": body["model"],
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "hey"},
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            },
        )

    client = AsyncOpenAI(
        base_url="https://refuseless.test/v1",
        api_key="test",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    )
    return OpenAIProvider(openai_client=client)


def job(kind: str) -> JobRequest:
    return JobRequest.model_validate(
        {
            "id": f"{kind}:1" if kind == "reply" else f"{kind}:1:2",
            "kind": kind,
            "scope": {"guild_id": "1", "channel_id": "2", "conversation_id": "1"},
            "model": MODEL,
            "messages": [{"role": "user", "content": "[u1 alex] kiki hi"}],
            "max_output_tokens": 100,
        }
    )


async def test_a_refuseless_model_replies_through_refuseless(database_url):
    requests: list[dict] = []
    executor = PydanticExecutor(database_url, "unused-key", refuseless_api_token="token")
    executor.refuseless = refuseless(requests)

    result = await executor.execute(job("reply"))

    assert result.text == "hey"
    assert result.model == MODEL
    assert result.usage.cost is None  # Refuseless reports tokens, not cost
    [body] = requests
    assert body["url"] == "https://refuseless.test/v1/chat/completions"
    assert body["model"] == "deepseek-v4.1-flash-abliterated"
    # Kiki's own tools, but not OpenRouter's server tools or settings.
    assert {t["type"] for t in body["tools"]} == {"function"}
    assert not {"max_tool_calls", "usage", "reasoning", "reasoning_effort"} & set(body)
    # No thinking room or twin without thinking: one request at the plain cap.
    assert body["max_completion_tokens"] == 100


async def test_a_refuseless_topic_runs_through_refuseless(database_url):
    requests: list[dict] = []
    executor = PydanticExecutor(database_url, "unused-key", refuseless_api_token="token")
    executor.refuseless = refuseless(requests)

    await executor.execute(job("topic"))

    [body] = requests
    assert body["model"] == "deepseek-v4.1-flash-abliterated"
    assert "tools" not in body


async def test_a_refuseless_model_without_a_token_fails_clearly(database_url):
    executor = PydanticExecutor(database_url, "unused-key")

    with pytest.raises(ValueError, match="REFUSELESS_API_TOKEN"):
        await executor.execute(job("reply"))


def test_other_models_stay_on_openrouter():
    from kiki_agent_host.openrouter.chat import WebOpenRouterModel

    executor = PydanticExecutor("postgresql://unused.invalid/kiki", "unused-key")
    assert isinstance(executor.model("deepseek/deepseek-v4.1-flash-1017"), WebOpenRouterModel)
