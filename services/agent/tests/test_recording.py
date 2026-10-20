"""The host's own metrics and spans (docs/observability.md, "Metric catalog"):
recorded where each call is made, and nothing at all with export off."""

import asyncio
import os
import subprocess
import sys
from collections import Counter
from types import SimpleNamespace

import pytest
from pydantic_ai.exceptions import ContentFilterError
from pydantic_ai.images import GeneratedImage, ImageGenerationResult, TestImageGenerationModel
from pydantic_ai.messages import BinaryImage, ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import RequestUsage

from kiki_agent_host import telemetry
from kiki_agent_host.agent.executor import PydanticExecutor
from kiki_agent_host.agent.guard import Budget, guarded
from kiki_agent_host.contracts import Drawing, ImageJobRequest, JobRequest
from kiki_agent_host.media import generation
from kiki_agent_host.media.vision import ImageRefused, Vision, look
from kiki_agent_host.openrouter.embeddings import EMBEDDING_MODEL, Embedder

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 16
PRIMARY, FALLBACK = "openai/gpt-image-2.5-flare", "qwen/qwen-image-2.1-turbo"


def billed(text="hi", cost=0.002, input_tokens=120, output_tokens=8):
    def respond(messages, info):
        return ModelResponse(
            parts=[TextPart(text)],
            usage=RequestUsage(input_tokens=input_tokens, output_tokens=output_tokens),
            provider_details={"cost": cost},
        )

    return FunctionModel(respond)


def executor(model=None, **options):
    class TestExecutor(PydanticExecutor):
        def model(self, name):
            return model

        def image_model(self, name):
            return self.image_models[name]

    return TestExecutor("postgresql://unused.invalid/kiki", "unused-key", **options)


def topic_job():
    return JobRequest.model_validate(
        {
            "id": "topic:1",
            "kind": "topic",
            "scope": {"guild_id": "1", "channel_id": "2", "conversation_id": "2"},
            "model": "test/chat",
            "messages": [{"role": "user", "content": "name this conversation"}],
            "max_output_tokens": 50,
        }
    )


async def test_a_run_is_one_call_with_its_cost_and_tokens(recorded):
    await executor(billed("a topic")).execute(topic_job())
    labels = {"purpose": "topic", "model": "test/chat"}
    assert recorded.value("kiki.llm.calls", **labels, result="ok") == 1
    assert recorded.value("kiki.llm.cost_usd", **labels) == 0.002
    assert recorded.value("kiki.llm.tokens", **labels, type="input") == 120
    assert recorded.value("kiki.llm.tokens", **labels, type="output") == 8
    assert recorded.value("kiki.llm.duration", **labels) == 1
    assert recorded.value("kiki.agent.job.model_requests") == 1
    [span] = recorded.finished("llm topic")
    assert span.attributes["kiki.cost_usd"] == 0.002
    assert span.attributes["gen_ai.usage.input_tokens"] == 120
    assert span.attributes["gen_ai.request.model"] == "test/chat"


async def test_a_failed_call_is_an_error_without_its_message(recorded):
    def fail(messages, info):
        raise RuntimeError("secret provider detail")

    with pytest.raises(RuntimeError):
        await executor(FunctionModel(fail)).execute(topic_job())
    assert recorded.value("kiki.llm.calls", purpose="topic", model="test/chat", result="error")
    assert recorded.value("kiki.llm.cost_usd", purpose="topic", model="test/chat") is None
    [span] = recorded.finished("llm topic")
    assert span.attributes["error.type"] == "RuntimeError"
    assert not span.events and "secret" not in str(span.status.description)


async def test_vision_and_embedding_calls_are_billed_where_they_are_made(recorded):
    vision = Vision(billed("a cat", cost=0.001, input_tokens=900), "vision/model")
    assert await look(vision, ["what is this"], max_tokens=50) == "a cat"
    emoji = Vision(billed("a frog", cost=0.0005), "emoji/model", purpose="emoji")
    await look(emoji, ["describe"], max_tokens=50)

    class Embeddings:
        async def create(self, model, input):
            data = [SimpleNamespace(index=i, embedding=[0.1] * 1024) for i in range(len(input))]
            return SimpleNamespace(data=data, usage=SimpleNamespace(cost=0.0001, prompt_tokens=30))

    await Embedder(SimpleNamespace(embeddings=Embeddings()))(["hello"])
    assert recorded.value("kiki.llm.cost_usd", purpose="vision", model="vision/model") == 0.001
    assert recorded.value("kiki.llm.cost_usd", purpose="emoji", model="emoji/model") == 0.0005
    embedding = {"purpose": "embedding", "model": EMBEDDING_MODEL}
    assert recorded.value("kiki.llm.cost_usd", **embedding) == 0.0001
    assert recorded.value("kiki.llm.tokens", **embedding, type="input") == 30
    assert recorded.value("kiki.llm.tokens", **embedding, type="output") is None
    [span] = recorded.finished("llm vision")
    assert span.attributes["gen_ai.usage.input_tokens"] == 900
    assert len(recorded.finished("llm embedding")) == 1


class ImageModel(TestImageGenerationModel):
    def __init__(self, error=None):
        super().__init__("test/image")
        self.error = error

    async def generate(self, prompt, *, images=None, settings=None):
        if self.error:
            raise self.error
        return ImageGenerationResult(
            [GeneratedImage(BinaryImage(PNG, media_type="image/png"))],
            prompt=prompt,
            model_name=self.model_name,
            provider_name="test",
            provider_details={"cost": 0.04},
        )


def image_job():
    return ImageJobRequest(
        id="image:1:1",
        scope={"guild_id": "1", "channel_id": "2", "conversation_id": "1"},
        image_model=PRIMARY,
        reply_job="reply:1",
        chat_model="test/chat",
        ack="one sec",
        drawing=Drawing(prompt="Nougie on a beach", subjects=[], caption="da beach"),
    )


async def test_a_safety_block_counts_the_fallback_and_how_it_went(recorded):
    blocked = ContentFilterError("sexual", body=None)
    run = executor(image_fallback_model=FALLBACK)
    run.image_models = {PRIMARY: ImageModel(blocked), FALLBACK: ImageModel()}
    await run.generate(image_job(), [], [])
    assert recorded.value("kiki.images.attempts", model=PRIMARY, result="safety_blocked") == 1
    assert recorded.value("kiki.images.attempts", model=FALLBACK, result="drawn") == 1
    assert recorded.value("kiki.images.fallback", result="drawn") == 1
    assert recorded.value("kiki.images.duration", model=FALLBACK) == 1
    image = {"purpose": "image"}
    assert recorded.value("kiki.llm.calls", **image, model=PRIMARY, result="error") == 1
    assert recorded.value("kiki.llm.cost_usd", **image, model=FALLBACK) == 0.04
    spans = recorded.finished("image attempt")
    assert [s.attributes["kiki.image.result"] for s in spans] == ["safety_blocked", "drawn"]

    # Blocked by both: the fallback failed. A refusal the fallback can't fix
    # (no image at all) doesn't try it.
    run.image_models[FALLBACK].error = blocked
    with pytest.raises(generation.SafetyRefused):
        await run.generate(image_job(), [], [])
    assert recorded.value("kiki.images.fallback", result="failed") == 1
    run.image_models[PRIMARY].error = ImageRefused("no image")
    with pytest.raises(ImageRefused):
        await run.generate(image_job(), [], [])
    assert recorded.value("kiki.images.attempts", model=PRIMARY, result="refused") == 1
    assert recorded.value("kiki.images.attempts", model=FALLBACK, result="safety_blocked") == 1


async def test_every_tool_outcome_is_counted(recorded):
    deps = SimpleNamespace(
        spent=Counter(), job_id="reply:1", scope=SimpleNamespace(channel_id=2, conversation_id=2)
    )
    ctx = SimpleNamespace(deps=deps)
    one = Budget("Tool", 1)

    async def ok():
        return {"messages": []}

    async def slow():
        await asyncio.sleep(1)

    async def refused():
        raise ImageRefused("no image")

    async def invalid():
        raise ValueError("bad id")

    async def told():
        return {"error": "no such channel"}

    async def broken():
        raise RuntimeError("bug")

    assert await guarded(ctx, "search_messages", ok, budget=one) == {"messages": []}
    assert "budget used up" in (await guarded(ctx, "search_messages", ok, budget=one))["error"]
    await guarded(ctx, "read_link", slow, budget=None, timeout=0.01)
    await guarded(ctx, "read_image", refused, budget=None)
    await guarded(ctx, "find_emoji", invalid, budget=None)
    await guarded(ctx, "channel_history", told, budget=None)
    with pytest.raises(RuntimeError):
        await guarded(ctx, "recall", broken, budget=None)

    calls = {
        ("search_messages", "ok"),
        ("search_messages", "budget_exhausted"),
        ("read_link", "timeout"),
        ("read_image", "refused"),
        ("find_emoji", "invalid_arguments"),
        ("channel_history", "error"),
        ("recall", "error"),
    }
    for tool, outcome in calls:
        assert recorded.value("kiki.tools.calls", tool=tool, outcome=outcome) == 1, (tool, outcome)
    # A call turned away never ran: no duration.
    assert recorded.value("kiki.tools.duration", tool="search_messages") == 1
    spans = recorded.finished("execute_tool search_messages")
    assert [s.attributes["kiki.tool.outcome"] for s in spans] == ["ok", "budget_exhausted"]
    [span] = recorded.finished("execute_tool recall")
    assert span.attributes["error.type"] == "RuntimeError"


async def test_model_labels_are_capped_per_process(recorded):
    models = [f"vendor/model-{i}" for i in range(telemetry.MAX_MODELS + 2)]
    for model in [*models, models[0]]:
        with telemetry.llm_call("reply", model) as call:
            call.bill(0.01)
    assert recorded.value("kiki.llm.calls", purpose="reply", model=models[0], result="ok") == 2
    assert recorded.value("kiki.llm.calls", purpose="reply", model=models[15], result="ok") == 1
    assert recorded.value("kiki.llm.calls", purpose="reply", model=models[16], result="ok") is None
    assert recorded.value("kiki.llm.calls", purpose="reply", model="other", result="ok") == 2
    # Spans keep the full ID.
    assert recorded.finished("llm reply")[-2].attributes["gen_ai.request.model"] == models[-1]
    with telemetry.image_attempt("vendor/late-image", str):
        pass
    assert recorded.value("kiki.images.attempts", model="other", result="drawn") == 1


def test_with_export_off_recording_is_a_no_op_without_the_sdk():
    script = """
import sys
from kiki_agent_host import telemetry
import kiki_agent_host.agent.executor, kiki_agent_host.api.app

assert not telemetry.configure() and telemetry._recorder is None
with telemetry.llm_call("reply", "test/chat") as call:
    call.bill(0.01, 10, 2)
with telemetry.tool_call("recall") as call:
    call.outcome = "timeout"
with telemetry.image_attempt("test/image", str, fallback=True) as attempt:
    attempt.bill(0.04)
try:
    with telemetry.llm_call("vision", "test/vision"):
        raise ValueError("passes through")
except ValueError:
    pass
telemetry.job_answered("reply", "completed")
telemetry.job_ran("reply", 1.5)
telemetry.job_rejected("busy")
telemetry.model_requests(3)
telemetry.web_calls(1, 2)
loaded = [m for m in sys.modules if m.startswith(("opentelemetry.sdk", "opentelemetry.instrumentation"))]
assert not loaded, loaded
"""
    env = {k: v for k, v in os.environ.items() if not k.startswith("OTEL_")}
    ran = subprocess.run(
        [sys.executable, "-c", script], env=env, capture_output=True, text=True, check=False
    )
    assert ran.returncode == 0, ran.stderr


async def test_a_call_given_up_on_is_a_span_but_not_counted(recorded):
    async def embed_forever():
        with telemetry.llm_call("embedding", EMBEDDING_MODEL):
            await asyncio.sleep(10)

    task = asyncio.create_task(embed_forever())
    await asyncio.sleep(0.01)
    task.cancel()  # what finish() does to indexing a reply didn't wait for
    with pytest.raises(asyncio.CancelledError):
        await task
    for result in ("ok", "error", "timeout"):
        labels = {"purpose": "embedding", "model": EMBEDDING_MODEL, "result": result}
        assert recorded.value("kiki.llm.calls", **labels) is None
    [span] = recorded.finished("llm embedding")
    assert span.attributes["error.type"] == "CancelledError"
