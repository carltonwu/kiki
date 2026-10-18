"""Web search and fetch are OpenRouter server tools: check what we send and what we read."""

import json
import time

import httpx
import pytest
from openai import AsyncOpenAI
from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.providers.openrouter import OpenRouterProvider

from kiki_agent_host.agent import web
from kiki_agent_host.agent.executor import PydanticExecutor
from kiki_agent_host.agent.web import web_use
from kiki_agent_host.contracts import JobRequest, JobResult
from kiki_agent_host.openrouter.chat import WebOpenRouterModel


def completion(body: dict, server_tool_use: dict | None = None, annotations=()) -> dict:
    usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "cost": 0.01}
    if server_tool_use:
        usage["server_tool_use_details"] = server_tool_use
    return {
        "id": "gen-1",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": body["model"],
        "provider": "Test",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "logprobs": None,
                "message": {
                    "role": "assistant",
                    "content": "it's 2.57.0",
                    "refusal": None,
                    "annotations": [
                        {
                            "type": "url_citation",
                            "url_citation": {
                                "url": url,
                                "title": "t",
                                "start_index": 0,
                                "end_index": 0,
                            },
                        }
                        for url in annotations
                    ],
                },
            }
        ],
        "usage": usage,
    }


def openrouter(requests: list[dict], **answer) -> OpenRouterProvider:
    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        return httpx.Response(200, json=completion(body, **answer))

    client = AsyncOpenAI(
        base_url="https://openrouter.test/api/v1",
        api_key="test",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    )
    return OpenRouterProvider(openai_client=client)


async def test_reply_requests_offer_web_search_and_fetch_server_tools():
    requests: list[dict] = []
    model = WebOpenRouterModel("test/model", provider=openrouter(requests))
    probe = Agent(model, capabilities=web.WEB_TOOLS, tools=[lambda: "unused"])
    await probe.run("hi", model_settings={"extra_body": {"max_tool_calls": web.MAX_WEB_STEPS}})

    [body] = requests
    server = {t["type"]: t.get("parameters") for t in body["tools"] if t["type"] != "function"}
    assert set(server) == {"openrouter:web_search", "openrouter:web_fetch"}
    assert server["openrouter:web_fetch"] == {"max_content_tokens": web.WEB_FETCH_MAX_TOKENS}
    assert body["max_tool_calls"] == web.MAX_WEB_STEPS
    assert any(t["type"] == "function" for t in body["tools"])


def executor_with(provider: OpenRouterProvider, database_url: str) -> PydanticExecutor:
    class WebExecutor(PydanticExecutor):
        def model(self, name):
            return WebOpenRouterModel(name, provider=provider)

    return WebExecutor(database_url, "unused-key")


def job(kind: str, text: str) -> JobRequest:
    return JobRequest.model_validate(
        {
            "id": f"{kind}:1" if kind == "reply" else f"{kind}:1:2",
            "kind": kind,
            "scope": {"guild_id": "1", "channel_id": "2", "conversation_id": "1"},
            "model": "test/model",
            "messages": [{"role": "user", "content": text}],
            "max_output_tokens": 100,
        }
    )


async def test_topic_jobs_have_no_tools_or_web_step_cap(database_url):
    requests: list[dict] = []
    executor = executor_with(openrouter(requests), database_url)
    result = await executor.execute(job("topic", "[u1 alex] summarize"))

    [body] = requests
    assert "tools" not in body
    assert "max_tool_calls" not in body
    assert result.web is None


async def test_reply_without_web_use_records_none(database_url):
    requests: list[dict] = []
    executor = executor_with(openrouter(requests), database_url)
    result = await executor.execute(job("reply", "[u1 alex] kiki rate my outfit"))

    assert result.web is None
    assert result.model_dump(mode="json")["web"] is None


def test_web_use_counts_searches_fetches_and_unique_sources():
    def response(used=None, urls=()):
        details = {
            "annotations": [{"type": "url_citation", "url_citation": {"url": u}} for u in urls]
        }
        if used:
            details["server_tool_use"] = used
        return ModelResponse(parts=[TextPart("x")], provider_details=details)

    use = web_use(
        [
            response(
                {"web_search_requests": 2, "tool_calls_executed": 3}, ["https://a", "https://b"]
            ),
            response({"tool_calls_executed": 1}, ["https://a"]),
            response(),
        ]
    )
    assert (use.searches, use.fetches, use.sources) == (2, 2, ["https://a", "https://b"])
    assert web_use([response()]).model_dump() == {"searches": 0, "fetches": 0, "sources": []}


def test_saved_results_without_web_use_still_validate():
    old = {"text": "hi", "model": "m", "steps": 1, "usage": {"prompt_tokens": 1}}
    assert JobResult.model_validate(old).web is None


async def test_executor_reports_web_use(database_url):
    requests: list[dict] = []
    provider = openrouter(
        requests,
        server_tool_use={"web_search_requests": 1, "tool_calls_executed": 2},
        annotations=["https://pypi.org/project/pydantic-ai/"],
    )

    executor = executor_with(provider, database_url)
    result = await executor.execute(job("reply", "[u1 alex] kiki latest pydantic ai?"))

    assert result.web is not None
    assert result.web.model_dump() == {
        "searches": 1,
        "fetches": 1,
        "sources": ["https://pypi.org/project/pydantic-ai/"],
    }
    assert result.usage.cost == pytest.approx(0.01)
    assert {t["type"] for t in requests[0]["tools"]} >= {
        "openrouter:web_search",
        "openrouter:web_fetch",
    }
    assert requests[0]["max_tool_calls"] == web.MAX_WEB_STEPS
