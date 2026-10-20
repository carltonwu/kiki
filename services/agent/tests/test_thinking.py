"""A reasoning chat model thinks inside `max_tokens`. On 10-20 (reply:404) DeepSeek
V4.1 Flash thought through all 2,000 tokens after two read_images and wrote
nothing; pydantic-ai raised UnexpectedModelBehavior and the reply was lost. Runs
now give thinking room beyond the answer's, and a response that is all thinking
goes again with thinking off, keeping the run's tool results."""

import contextlib
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
        unthinking_settings.append(info.model_settings)
        return ModelResponse(parts=[TextPart("on its way")], provider_details={"cost": 0.0005})

    unthinking_settings = []
    result = await executor(database_url, thinking, unthinking).execute(job("reply"))
    assert result.text == "on its way"
    # Thinking has room beyond the answer's; without thinking it's the plain cap.
    assert [s["max_tokens"] for s in thinking_settings] == [100 + REASONING_TOKENS] * 2
    assert [s["max_tokens"] for s in unthinking_settings] == [100]
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


async def test_a_tool_call_that_ends_at_the_limit_is_acted_on(database_url):
    """Complete tool arguments that finish exactly at the limit aren't thrown
    away for an answer without the tool (review of #68: no drawing was queued)."""
    calls = []

    def thinking(messages, info: AgentInfo) -> ModelResponse:
        returns = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
        if returns:
            return ModelResponse(parts=[TextPart("done")])
        call = ToolCallPart("find_emoji", {"name": "huh"}, tool_call_id="1")
        return ModelResponse(parts=[ThinkingPart("hmm"), call], finish_reason="length")

    def unthinking(messages, info: AgentInfo) -> ModelResponse:
        calls.append(messages)
        return ModelResponse(parts=[TextPart("without the tool")])

    result = await executor(database_url, thinking, unthinking).execute(job("reply"))
    assert result.text == "done"
    assert calls == []


async def test_a_model_that_doesnt_think_keeps_the_plain_cap(model_catalog):
    from kiki_agent_host.openrouter import catalog

    model_catalog["test/model"] = frozenset({"tools", "max_tokens"})
    await catalog.refresh()  # at startup
    seen = []

    def plain(messages, info: AgentInfo) -> ModelResponse:
        seen.append(info.model_settings["max_tokens"])
        return ModelResponse(parts=[TextPart("memes")])

    e = executor("unused", plain, lambda m, i: pytest.fail("no fallback without thinking"))
    assert (await e.execute(job("topic"))).text == "memes"
    assert seen == [100]


async def test_the_catalog_says_which_models_think(model_catalog):
    from kiki_agent_host.openrouter import catalog

    model_catalog |= {
        "deepseek/deepseek-v4.1-flash-1017": frozenset({"tools", "reasoning"}),
        "openai/gpt-4o-mini": frozenset({"tools"}),
    }
    assert catalog.reasons("openai/gpt-4o-mini")  # before the first fetch: room
    await catalog._refreshing  # the background fetch it started
    assert catalog.reasons("deepseek/deepseek-v4.1-flash-1017")
    assert not catalog.reasons("openai/gpt-4o-mini")
    assert catalog.reasons("new/unlisted-model")  # unknown: give it room


async def test_a_reply_never_waits_on_the_catalog(monkeypatch):
    import asyncio

    from kiki_agent_host.openrouter import catalog

    stuck = asyncio.Event()

    async def hangs():
        await stuck.wait()
        return {}

    monkeypatch.setattr(catalog, "fetch", hangs)
    assert catalog.reasons("test/model")  # answered from the copy, fetch still running
    assert not catalog._refreshing.done()
    assert catalog.reasons("test/model")  # one fetch at a time
    stuck.set()
    await catalog._refreshing


async def test_a_failed_refresh_of_a_stale_copy_is_retried_soon(model_catalog, monkeypatch):
    """The review of #68: a failed hourly refresh waited another hour, and a model
    missing from a full copy never triggered one."""
    from kiki_agent_host.openrouter import catalog

    model_catalog["openai/gpt-4o-mini"] = frozenset({"tools"})
    clock = [1000.0]
    monkeypatch.setattr(catalog.time, "monotonic", lambda: clock[0])
    await catalog.refresh()
    assert not catalog.due("openai/gpt-4o-mini", clock[0])

    async def down():
        raise catalog.httpx.ConnectError("down")

    monkeypatch.setattr(catalog, "fetch", down)
    clock[0] += catalog.CACHE_SECONDS + 1
    assert catalog.due("openai/gpt-4o-mini", clock[0])
    await catalog.refresh()  # fails: the stale copy stays
    assert not catalog.reasons("openai/gpt-4o-mini")
    assert not catalog.due("openai/gpt-4o-mini", clock[0] + catalog.RETRY_SECONDS - 1)
    assert catalog.due("openai/gpt-4o-mini", clock[0] + catalog.RETRY_SECONDS + 1)
    # A model the copy doesn't list: fetch again, at most once a minute.
    clock[0] += catalog.RETRY_SECONDS + 1
    model_catalog["new/model"] = frozenset({"tools"})
    monkeypatch.setattr(catalog, "fetch", lambda: _listed(model_catalog))
    await catalog.refresh()
    assert not catalog.due("new/model", clock[0])
    assert catalog.due("other/new-model", clock[0] + catalog.RETRY_SECONDS + 1)
    assert not catalog.reasons("new/model")


async def _listed(models):
    return dict(models)


async def test_whitespace_at_the_limit_goes_again_without_thinking():
    """Thinking then " \n\t" at the limit: pydantic-ai takes it as text, and the
    reply was rejected as empty (review of #68)."""

    def blank(messages, info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[ThinkingPart("hmm"), TextPart(" \n\t")], finish_reason="length")

    def unthinking(messages, info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart("memes")])

    assert (await executor("unused", blank, unthinking).execute(job("topic"))).text == "memes"


async def test_a_vision_answer_of_whitespace_at_the_limit_goes_again():
    from kiki_agent_host.media.vision import Vision, look

    seen = []

    def respond(messages, info: AgentInfo) -> ModelResponse:
        seen.append(info.model_settings.get("openrouter_reasoning"))
        if seen[-1] == {"enabled": False}:
            return ModelResponse(parts=[TextPart("Image 1: a dog.")])
        return ModelResponse(parts=[ThinkingPart("hmm"), TextPart(" \n\t")], finish_reason="length")

    vision = Vision(FunctionModel(respond), "test/vision")
    assert await look(vision, ["describe"], max_tokens=100) == "Image 1: a dog."
    assert seen == [None, {"enabled": False}]


async def test_no_answer_even_without_thinking_fails_the_job():
    e = executor("unused", lambda messages, info: thought_only(), lambda m, i: thought_only())
    # The job fails, and the ingester posts its failure note (agent/mod.rs).
    with pytest.raises(FallbackExceptionGroup):
        await e.execute(job("topic"))


async def test_the_host_fetches_the_catalog_at_startup(model_catalog, monkeypatch):
    import asyncio

    from kiki_agent_host.api import app as host
    from kiki_agent_host.config import Settings
    from kiki_agent_host.openrouter import catalog

    monkeypatch.setattr(host.telemetry, "shutdown", lambda: None)
    model_catalog["openai/gpt-4o-mini"] = frozenset({"tools"})
    app = host.create_app(
        Settings("postgresql://unused.invalid/kiki", "t"), executor("unused", None, None)
    )
    async with host.lifespan(app):
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not catalog.reasons("openai/gpt-4o-mini")  # known before any reply


@pytest.mark.parametrize("failing", [False, True])
async def test_shutdown_stops_a_refresh_still_running(monkeypatch, failing):
    """A later refresh (the hourly one, or for a new model) doesn't outlive the
    app, even when teardown comes from an error (review of #68)."""
    import asyncio

    from kiki_agent_host.api import app as host
    from kiki_agent_host.config import Settings
    from kiki_agent_host.openrouter import catalog

    monkeypatch.setattr(host.telemetry, "shutdown", lambda: None)
    started, cancelled = asyncio.Event(), []

    async def hangs():
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    app = host.create_app(
        Settings("postgresql://unused.invalid/kiki", "t"), executor("unused", None, None)
    )
    with pytest.raises(RuntimeError) if failing else contextlib.nullcontext():
        async with host.lifespan(app):
            await catalog._refreshing  # the startup fetch (the fixture's: instant)
            monkeypatch.setattr(catalog, "_fetched_at", -float("inf"))  # an hour on
            monkeypatch.setattr(catalog, "_attempted_at", -float("inf"))
            monkeypatch.setattr(catalog, "fetch", hangs)
            catalog.reasons("test/model")
            await started.wait()
            if failing:
                raise RuntimeError("teardown from an error")
    assert cancelled == [True]
    assert catalog._refreshing.done()
