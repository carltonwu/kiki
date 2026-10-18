import asyncio
import base64
import json
from types import SimpleNamespace

import httpx
import pytest
from openai import AsyncOpenAI
from psycopg import AsyncConnection
from pydantic_ai.exceptions import ContentFilterError, UnexpectedModelBehavior
from pydantic_ai.images import GeneratedImage, ImageGenerationResult, TestImageGenerationModel
from pydantic_ai.messages import BinaryImage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel

from kiki_agent_host import images
from kiki_agent_host.agent import MAX_IMAGES, PydanticExecutor
from kiki_agent_host.contracts import JobRequest

PNG = b"\x89PNG\r\n\x1a\nfake"


def openrouter(handler) -> AsyncOpenAI:
    """An OpenRouter client whose HTTP requests go to `handler`."""
    transport = httpx.MockTransport(handler)
    return AsyncOpenAI(
        base_url="https://openrouter.test/api/v1",
        api_key="test-key",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=transport),
    )


async def test_openrouter_model_calls_the_images_api_and_reports_cost():
    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(
            (request.url.path, request.headers["authorization"], json.loads(request.content))
        )
        data = [
            {"b64_json": "not base64!"},
            {"b64_json": base64.b64encode(PNG).decode(), "media_type": "image/png"},
        ]
        return httpx.Response(200, json={"created": 1, "data": data, "usage": {"cost": 0.03}})

    model = images.OpenRouterImageModel("openai/gpt-image-2.5-flare", openrouter(handler))
    data, mime, cost = await images.generate(model, "  a frog in a hat ", "3:2")
    assert (data, mime, cost) == (PNG, "image/png", 0.03)
    [(path, authorization, body)] = sent
    assert path == "/api/v1/images"
    assert authorization == "Bearer test-key"
    assert body == {
        "model": "openai/gpt-image-2.5-flare",
        "prompt": "a frog in a hat",
        "n": 1,
        "aspect_ratio": "3:2",
        "quality": images.OPENAI_QUALITY,
    }


async def test_openrouter_model_sends_quality_only_to_openai_and_raises_without_images():
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"data": [], "usage": {"cost": 0}})

    model = images.OpenRouterImageModel("google/gemini-2.5-flash-image", openrouter(handler))
    with pytest.raises(UnexpectedModelBehavior):
        await model.generate("x")
    assert bodies == [{"model": "google/gemini-2.5-flash-image", "prompt": "x", "n": 1}]


class FakeImageModel(TestImageGenerationModel):
    """Records prompts; returns `image`, or raises `error`, after a short pause."""

    def __init__(self, image=PNG, media_type="image/png", error=None, cost=0.04):
        super().__init__("test/image")
        self.image, self.media_type, self.error, self.cost = image, media_type, error, cost
        self.prompts = []

    async def generate(self, prompt, *, images=None, settings=None):
        self.prompts.append((prompt, settings))
        await asyncio.sleep(0.01)  # like a real call, let parallel tool calls interleave
        if self.error:
            raise self.error
        return ImageGenerationResult(
            [GeneratedImage(BinaryImage(self.image, media_type=self.media_type))],
            prompt=prompt,
            model_name=self.model_name,
            provider_name="test",
            provider_details={"cost": self.cost},
        )


async def test_generate_turns_failures_into_messages_for_the_model():
    model = FakeImageModel()
    assert await images.generate(model, " a frog ", "2:3") == (PNG, "image/png", 0.04)
    assert model.prompts == [("a frog", {"aspect_ratio": "2:3"})]
    # Pydantic AI's own test model works too.
    data, mime, _ = await images.generate(TestImageGenerationModel(), "x")
    assert mime == "image/png" and data.startswith(b"\x89PNG")
    cases = [
        (FakeImageModel(error=ContentFilterError("blocked", body=None)), "refused"),
        (FakeImageModel(error=UnexpectedModelBehavior("empty", body=None)), "No image"),
        (FakeImageModel(media_type="image/svg+xml"), "unsupported image/svg"),
        (FakeImageModel(image=b"0" * (images.MAX_IMAGE_BYTES + 1)), "too large"),
    ]
    for model, message in cases:
        with pytest.raises(images.ImageRefused, match=message):
            await images.generate(model, "x")
    for prompt in ["   ", "x" * (images.MAX_PROMPT_CHARS + 1)]:
        with pytest.raises(ValueError, match="prompt"):
            await images.generate(FakeImageModel(), prompt)


def request(image_model):
    return JobRequest.model_validate(
        {
            "id": "reply:1",
            "kind": "reply",
            "scope": {"guild_id": "1", "channel_id": "2", "conversation_id": "1"},
            "model": "test/model",
            "messages": [{"role": "user", "content": "alex: draw me a frog"}],
            "max_output_tokens": 100,
            "image_model": image_model,
        }
    )


class FakeEmbeddings:
    async def create(self, model, input):
        data = [SimpleNamespace(index=i, embedding=[0.1] * 1024) for i in range(len(input))]
        return SimpleNamespace(data=data, usage=SimpleNamespace(cost=0))


def executor(database_url, image_model):
    class TestExecutor(PydanticExecutor):
        web_tools = ()  # TestModel can't run OpenRouter's server tools (see test_web.py)

        def model(self, name):
            return TestModel()

        def image_model(self, name):
            self.names.append(name)
            return image_model

    executor = TestExecutor(database_url, "unused-key")
    executor.names = []
    executor.embeddings = SimpleNamespace(embeddings=FakeEmbeddings())
    return executor


async def job_row(database_url, job_id="reply:1"):
    async with await AsyncConnection.connect(database_url, autocommit=True) as db:
        await db.execute(
            "INSERT INTO agent_host_jobs (id, request, status) VALUES (%s, '{}', 'running')",
            (job_id,),
        )


async def test_executor_saves_generated_images_for_rust_to_attach(database_url):
    await job_row(database_url)
    run = executor(database_url, FakeImageModel())
    result = await run.execute(request("openai/gpt-image-2.5-flare"))
    assert run.names == ["openai/gpt-image-2.5-flare"]
    # TestModel calls every offered tool once, then answers with their results.
    outputs = json.loads(result.text)
    assert outputs["generate_image"] == {"attached": True, "images_attached": 1}
    [image] = result.images
    assert image.mime_type == "image/png"
    assert result.usage.cost == pytest.approx(0.04)
    async with await AsyncConnection.connect(database_url, autocommit=True) as db:
        cursor = await db.execute(
            "SELECT job_id, mime_type, data FROM agent_host_images WHERE id = %s", (image.id,)
        )
        assert await cursor.fetchone() == ("reply:1", "image/png", PNG)


async def test_generate_image_is_not_offered_without_an_image_model(database_url):
    model = FakeImageModel()
    run = executor(database_url, model)
    result = await run.execute(request(None))
    assert "generate_image" not in json.loads(result.text)
    assert result.images == []
    assert run.names == [] and model.prompts == []


async def test_refusals_reach_the_model_instead_of_failing_the_reply(database_url):
    model = FakeImageModel(error=ContentFilterError("blocked", body=None))
    result = await executor(database_url, model).execute(request("test/image"))
    assert json.loads(result.text)["generate_image"] == {
        "error": "The image model refused that prompt."
    }
    assert result.images == []


async def test_parallel_calls_still_draw_at_most_max_images(database_url):
    def respond(messages, info):
        if len(messages) == 1:
            # One response asking for three images at once: the tools run concurrently.
            return ModelResponse(
                parts=[
                    ToolCallPart("generate_image", {"prompt": f"frog {i}"}, tool_call_id=str(i))
                    for i in range(3)
                ]
            )
        return ModelResponse(parts=[TextPart("here")])

    model = FakeImageModel()
    run = executor(database_url, model)
    run.model = lambda name: FunctionModel(respond)
    await job_row(database_url)
    result = await run.execute(request("test/image"))
    assert len(result.images) == MAX_IMAGES == len(model.prompts)
