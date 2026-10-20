"""A reasoning chat model thinks inside `max_tokens`. On 10-20 (reply:404) DeepSeek
V4.1 Flash thought through all 2,000 tokens after two read_images and wrote
nothing; pydantic-ai raised UnexpectedModelBehavior and the reply was lost. Runs
now give thinking room beyond the answer's, and a response that is all thinking
goes again with thinking off, keeping the run's tool results."""

from types import SimpleNamespace

import pytest
from pydantic_ai.exceptions import FallbackExceptionGroup
from pydantic_ai.messages import (
    ModelResponse,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from kiki_agent_host.agent.executor import PydanticExecutor
from kiki_agent_host.contracts import JobRequest
from kiki_agent_host.media.vision import REASONING_TOKENS


class FakeEmbeddings:
    async def create(self, model, input):
        data = [SimpleNamespace(index=i, embedding=[0.1] * 1024) for i in range(len(input))]
        return SimpleNamespace(data=data, usage=SimpleNamespace(cost=0))


def thought_only() -> ModelResponse:
    return ModelResponse(
        parts=[ThinkingPart("hmm " * 50)],
        finish_reason="length",
        provider_details={"cost": 0.003},
    )


def executor(database_url, thinking, unthinking) -> PydanticExecutor:
    class TestExecutor(PydanticExecutor):
        web_tools = ()  # FunctionModel can't run OpenRouter's server tools (see test_web.py)

        def model(self, name):
            return FunctionModel(thinking)

        def unthinking(self, name):
            return FunctionModel(unthinking)

    e = TestExecutor(database_url, "unused-key")
    e.embeddings = SimpleNamespace(embeddings=FakeEmbeddings())
    return e


def job(kind: str) -> JobRequest:
    return JobRequest.model_validate(
        {
            "id": f"{kind}:thinking",
            "kind": kind,
            "scope": {"guild_id": "1", "channel_id": "2", "conversation_id": "2"},
            "model": "test/model",
            "messages": [{"role": "user", "content": "alex: help kiki where my meme at"}],
            "max_output_tokens": 100,
            "people": [{"ref": "u1", "user_id": "100", "label": "alex", "names": ["alex"]}],
        }
    )


async def test_a_reply_that_only_thinks_goes_again_without_thinking(database_url):
    thinking_settings, unthinking_messages = [], []

    def thinking(messages, info: AgentInfo) -> ModelResponse:
        thinking_settings.append(info.model_settings)
        if not any(isinstance(m, ModelResponse) for m in messages):
            return ModelResponse(
                parts=[ToolCallPart("find_emoji", {"name": "huh"}, tool_call_id="1")],
                provider_details={"cost": 0.001},
            )
        return thought_only()

    def unthinking(messages, info: AgentInfo) -> ModelResponse:
        unthinking_messages.append(messages)
        return ModelResponse(parts=[TextPart("on its way")], provider_details={"cost": 0.0005})

    result = await executor(database_url, thinking, unthinking).execute(job("reply"))
    assert result.text == "on its way"
    # Thinking has room beyond the answer's.
    assert [s["max_tokens"] for s in thinking_settings] == [100 + REASONING_TOKENS] * 2
    # The same request went again: the tool's result is still there, not rerun.
    returns = [p for m in unthinking_messages[0] for p in m.parts if isinstance(p, ToolReturnPart)]
    assert [r.tool_name for r in returns] == ["find_emoji"]
    # The response thrown away was still paid for.
    assert result.usage.cost == pytest.approx(0.0045)


async def test_a_topic_that_only_thinks_goes_again_without_thinking():
    def unthinking(messages, info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart("memes")])

    e = executor("unused", lambda messages, info: thought_only(), unthinking)
    assert (await e.execute(job("topic"))).text == "memes"


async def test_no_answer_even_without_thinking_fails_the_job():
    e = executor("unused", lambda messages, info: thought_only(), lambda m, i: thought_only())
    # The job fails, and the ingester posts its failure note (agent/mod.rs).
    with pytest.raises(FallbackExceptionGroup):
        await e.execute(job("topic"))
