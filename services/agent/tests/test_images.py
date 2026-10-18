import asyncio
import base64
import json
from types import SimpleNamespace

import pytest
from pydantic_ai.models.test import TestModel

from kiki_agent_host import images
from kiki_agent_host.agent import MAX_IMAGES, PydanticExecutor
from kiki_agent_host.contracts import JobRequest

PNG = b"\x89PNG\r\n\x1a\nfake"


def completion(images_=None, content=None, cost=0.04):
    message = SimpleNamespace(content=content, model_extra={"images": images_} if images_ else {})
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message)], usage=SimpleNamespace(cost=cost)
    )


def data_url(data=PNG, mime="image/png"):
    return {
        "type": "image_url",
        "image_url": {"url": f"data:{mime};base64,{base64.b64encode(data).decode()}"},
    }


class FakeImages:
    def __init__(self, response):
        self.response, self.requests = response, []

    async def create(self, **request):
        self.requests.append(request)
        await asyncio.sleep(0.01)  # like a real call, let parallel tool calls interleave
        return self.response


def client(response):
    return SimpleNamespace(chat=SimpleNamespace(completions=FakeImages(response)))


async def test_generate_decodes_the_first_usable_image_and_reports_cost():
    fake = client(completion([{"image_url": {"url": "https://not-inline"}}, data_url()]))
    data, mime, cost = await images.generate(fake, "test/image", "  a frog in a hat ")
    assert (data, mime, cost) == (PNG, "image/png", 0.04)
    [request] = fake.chat.completions.requests
    assert request["model"] == "test/image"
    assert request["messages"] == [{"role": "user", "content": "a frog in a hat"}]
    assert request["extra_body"]["modalities"] == ["image", "text"]


async def test_generate_explains_missing_images_and_rejects_bad_input():
    with pytest.raises(images.ImageRefused, match="I can't draw that"):
        await images.generate(client(completion(content="I can't draw that")), "m/i", "x")
    with pytest.raises(images.ImageRefused, match="No image"):
        await images.generate(client(completion([data_url(mime="image/svg+xml")])), "m/i", "x")
    with pytest.raises(images.ImageRefused, match="too large"):
        big = data_url(b"0" * (images.MAX_IMAGE_BYTES + 1))
        await images.generate(client(completion([big])), "m/i", "x")
    for prompt in ["   ", "x" * (images.MAX_PROMPT_CHARS + 1)]:
        with pytest.raises(ValueError, match="prompt"):
            await images.generate(client(completion([data_url()])), "m/i", prompt)


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


def executor(database_url, response):
    class TestExecutor(PydanticExecutor):
        def model(self, name):
            return TestModel()

    executor = TestExecutor(database_url, "unused-key")
    executor.embeddings = SimpleNamespace(embeddings=FakeEmbeddings())
    executor.images = client(response)
    return executor


async def job_row(db, job_id="reply:1"):
    await db.execute(
        "INSERT INTO agent_host_jobs (id, request, status) VALUES (%s, '{}', 'running')", (job_id,)
    )


async def test_executor_saves_generated_images_for_rust_to_attach(database_url):
    from psycopg import AsyncConnection

    async with await AsyncConnection.connect(database_url, autocommit=True) as db:
        await job_row(db)
        result = await executor(database_url, completion([data_url()])).execute(
            request("test/image")
        )
        # TestModel calls every offered tool once, then answers with their results.
        outputs = json.loads(result.text)
        assert outputs["generate_image"] == {"attached": True, "images_attached": 1}
        [image] = result.images
        assert image.mime_type == "image/png"
        assert result.usage.cost == pytest.approx(0.04)
        cursor = await db.execute(
            "SELECT job_id, mime_type, data FROM agent_host_images WHERE id = %s", (image.id,)
        )
        assert await cursor.fetchone() == ("reply:1", "image/png", PNG)


async def test_generate_image_is_not_offered_without_an_image_model(database_url):
    fake = completion([data_url()])
    run = executor(database_url, fake)
    result = await run.execute(request(None))
    assert "generate_image" not in json.loads(result.text)
    assert result.images == []
    assert run.images.chat.completions.requests == []


async def test_refusals_reach_the_model_instead_of_failing_the_reply(database_url):
    result = await executor(database_url, completion(content="nope")).execute(request("test/image"))
    assert json.loads(result.text)["generate_image"] == {
        "error": "No image was generated. The image model said: nope"
    }
    assert result.images == []


async def test_parallel_calls_still_draw_at_most_max_images(database_url):
    from psycopg import AsyncConnection
    from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
    from pydantic_ai.models.function import FunctionModel

    def model(messages, info):
        if len(messages) == 1:
            # One response asking for three images at once: the tools run concurrently.
            return ModelResponse(
                parts=[
                    ToolCallPart("generate_image", {"prompt": f"frog {i}"}, tool_call_id=str(i))
                    for i in range(3)
                ]
            )
        return ModelResponse(parts=[TextPart("here")])

    run = executor(database_url, completion([data_url()]))
    run.model = lambda name: FunctionModel(model)
    async with await AsyncConnection.connect(database_url, autocommit=True) as db:
        await job_row(db)
    result = await run.execute(request("test/image"))
    assert len(result.images) == MAX_IMAGES == len(run.images.chat.completions.requests)
