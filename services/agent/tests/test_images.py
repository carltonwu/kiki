import asyncio
import base64
import json
from types import SimpleNamespace

import httpx
import pytest
from openai import AsyncOpenAI
from psycopg import AsyncConnection
from psycopg.types.json import Jsonb
from pydantic_ai.exceptions import ContentFilterError, UnexpectedModelBehavior, UserError
from pydantic_ai.images import GeneratedImage, ImageGenerationResult, TestImageGenerationModel
from pydantic_ai.messages import (
    BinaryContent,
    BinaryImage,
    ModelResponse,
    TextPart,
    ToolCallPart,
    UserPromptPart,
)
from pydantic_ai.models.function import FunctionModel

from kiki_agent_host.agent import MAX_IMAGES, PydanticExecutor
from kiki_agent_host.contracts import Drawing, ImageJobRequest, JobRequest, Subject
from kiki_agent_host.media import drawing, fetch, generation
from kiki_agent_host.media.vision import ImageRefused
from kiki_agent_host.openrouter.images import MAX_REFERENCES, OPENAI_QUALITY, OpenRouterImageModel

PNG = b"\x89PNG\r\n\x1a\nfake"
JPEG = b"\xff\xd8\xff\xe0nougie"


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

    model = OpenRouterImageModel("openai/gpt-image-2.5-flare", openrouter(handler))
    data, mime, cost = await generation.generate(model, "  a frog in a hat ", "3:4")
    assert (data, mime, cost) == (PNG, "image/png", 0.03)
    [(path, authorization, body)] = sent
    assert path == "/api/v1/images"
    assert authorization == "Bearer test-key"
    assert body == {
        "model": "openai/gpt-image-2.5-flare",
        "prompt": "a frog in a hat",
        "n": 1,
        "aspect_ratio": "3:4",
        "quality": OPENAI_QUALITY,
    }


async def test_openrouter_model_sends_references_in_order_as_data_urls():
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        image = {"b64_json": base64.b64encode(PNG).decode()}
        return httpx.Response(200, json={"data": [image], "usage": {"cost": 0.05}})

    model = OpenRouterImageModel("openai/gpt-image-2.5-flare", openrouter(handler))
    references = [
        BinaryImage(JPEG, media_type="image/jpeg"),
        BinaryImage(PNG, media_type="image/png"),
    ]
    await generation.generate(model, "Nougie on a beach", references=references)
    sent = bodies[0]["input_references"]
    assert [r["type"] for r in sent] == ["image_url", "image_url"]
    # Bytes, not Discord links (they expire), in the order the prompt numbers them.
    assert (
        sent[0]["image_url"]["url"] == "data:image/jpeg;base64," + base64.b64encode(JPEG).decode()
    )
    assert sent[1]["image_url"]["url"].startswith("data:image/png;base64,")
    with pytest.raises(UserError, match="at most 16"):
        await model.generate("x", images=[references[0]] * (MAX_REFERENCES + 1))


async def test_openrouter_model_sends_quality_only_to_openai_and_raises_without_images():
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"data": [], "usage": {"cost": 0}})

    model = OpenRouterImageModel("google/gemini-2.5-flash-image", openrouter(handler))
    with pytest.raises(UnexpectedModelBehavior):
        await model.generate("x")
    assert bodies == [{"model": "google/gemini-2.5-flash-image", "prompt": "x", "n": 1}]


class FakeImageModel(TestImageGenerationModel):
    """Records prompts and references; returns `image`, or raises `error`."""

    def __init__(self, image=PNG, media_type="image/png", error=None, cost=0.04):
        super().__init__("test/image")
        self.image, self.media_type, self.error, self.cost = image, media_type, error, cost
        self.prompts = []
        self.references = []

    async def generate(self, prompt, *, images=None, settings=None):
        self.prompts.append((prompt, settings))
        self.references.append(list(images or []))
        await asyncio.sleep(0.01)
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
    assert await generation.generate(model, " a frog ", "2:3") == (PNG, "image/png", 0.04)
    assert model.prompts == [("a frog", {"aspect_ratio": "2:3"})]
    # The bytes decide the type (fetch.sniff), not the provider's label.
    mislabeled = FakeImageModel(media_type="image/jpeg")
    assert (await generation.generate(mislabeled, "x"))[1] == "image/png"
    data, mime, _ = await generation.generate(TestImageGenerationModel(), "x")
    assert mime == "image/png" and data.startswith(b"\x89PNG")
    cases = [
        (FakeImageModel(error=ContentFilterError("blocked", body=None)), "refused"),
        (FakeImageModel(error=UnexpectedModelBehavior("empty", body=None)), "No image"),
        (FakeImageModel(image=b"<svg/>", media_type="image/svg+xml"), "unsupported image/svg"),
        (FakeImageModel(image=b"0" * (generation.MAX_IMAGE_BYTES + 1)), "too large"),
    ]
    for model, message in cases:
        with pytest.raises(ImageRefused, match=message):
            await generation.generate(model, "x")
    for prompt in ["   ", "x" * (generation.MAX_PROMPT_CHARS + 1)]:
        with pytest.raises(ValueError, match="prompt"):
            await generation.generate(FakeImageModel(), prompt)


def test_compose_numbers_references_and_defaults_to_a_phone_photo():
    nougie = Subject(name="Nougie", description="traumagotchi's dog")
    ethan = Subject(name="Ethan", user_id="2")
    kiki = Subject(name="Kiki", kiki=True, description="a smoky mushroom sprite")
    picture = BinaryImage(PNG, media_type="image/png")
    found = [
        drawing.Found(ethan, [(drawing.Reference(subject="Ethan", avatar_of="2"), picture)]),
        drawing.Found(
            nougie,
            [(drawing.Reference(subject="Nougie", message_id=str(i)), picture) for i in (7, 8)],
        ),
        drawing.Found(kiki, []),
    ]
    prompt, references, pictures = drawing.compose(
        Drawing(prompt="Ethan walks Nougie on a beach"), found
    )
    assert prompt.startswith("Ethan walks Nougie on a beach\n\nStyle: A real photograph, candid")
    assert "No glamorization, no heavy retouching" in prompt
    # Each reference by number and role, with what must stay the same.
    assert (
        "- Ethan, shown in reference image 1: Keep Ethan's face, facial features, hair, "
        "skin tone and build the same; don't change Ethan's identity." in prompt
    )
    assert (
        "- Nougie, shown in reference images 2-3: traumagotchi's dog. Keep Nougie's look "
        "(for an animal: breed, size, coat colors and markings) the same" in prompt
    )
    assert "- Kiki: a smoky mushroom sprite.\n" in prompt
    assert prompt.endswith(drawing.REFERENCES_ONLY)
    assert [r.message_id or r.avatar_of for r in references] == ["2", "7", "8"]
    assert len(pictures) == 3
    styled, _, _ = drawing.compose(Drawing(prompt="x", style="90s anime"), [])
    assert styled == "x\n\nStyle: 90s anime"
    # An edit changes only what's asked and keeps the photo's own look.
    edited, _, _ = drawing.compose(Drawing(prompt="Give him a cowboy hat", edit=True), found[:1])
    assert "Style:" not in edited
    assert edited.endswith(drawing.EDIT) and "Reference image 1 is the photo to edit" in edited


def test_verdicts_read_the_vision_check():
    answer = (
        "Image 1: a white dog on a couch => yes\nImage 2: a meme => no\n"
        "**Image 3:** the dog, a selfie (yes) => **Yes**\nimage 4: blurry => maybe\n"
        "Image 5: says yes on a sign => no"
    )
    assert drawing.verdicts(answer) == {1, 3}


def test_links_expire_and_refresh_only_for_discord():
    assert fetch.expired("https://cdn.discordapp.com/attachments/1/2/a.png?ex=1&is=2&hm=3")
    assert not fetch.expired(
        "https://cdn.discordapp.com/attachments/1/2/a.png?ex=ffffffff&is=2&hm=3"
    )
    assert not fetch.expired("https://cdn.discordapp.com/avatars/1/abc.png")


async def test_refresh_asks_discord_for_expired_links_only(monkeypatch):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(
            (request.url.path, request.headers["authorization"], json.loads(request.content))
        )
        urls = json.loads(request.content)["attachment_urls"]
        return httpx.Response(
            200,
            json={
                "refreshed_urls": [
                    {"original": u, "refreshed": u.replace("ex=1", "ex=ffffffff")} for u in urls
                ]
            },
        )

    old = "https://cdn.discordapp.com/attachments/1/2/a.png?ex=1&hm=3"
    fresh = "https://cdn.discordapp.com/attachments/1/2/b.png?ex=ffffffff&hm=3"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        assert await fetch.refresh(http, "", [old]) == {}  # no token: nothing to ask
        found = await fetch.refresh(
            http, "bot-token", [old, fresh, "https://example.com/x.png?ex=1"]
        )
    assert found == {old: old.replace("ex=1", "ex=ffffffff")}
    [(path, authorization, body)] = calls
    assert path == "/api/v10/attachments/refresh-urls"
    assert authorization == "Bot bot-token"
    assert body == {"attachment_urls": [old]}


# --- with Postgres -------------------------------------------------------------

GUILD = 1


def request(image_model, messages=None):
    return JobRequest.model_validate(
        {
            "id": "reply:1",
            "kind": "reply",
            "scope": {"guild_id": str(GUILD), "channel_id": "2", "conversation_id": "1"},
            "model": "test/model",
            "messages": messages or [{"role": "user", "content": "[u1 Carlton] draw nougie"}],
            "max_output_tokens": 100,
            "people": [
                {"ref": "u1", "user_id": "100", "label": "Carlton"},
                {"ref": "u2", "user_id": "200", "label": "Ethan"},
            ],
            "image_model": image_model,
        }
    )


class FakeEmbeddings:
    async def create(self, model, input):
        data = [SimpleNamespace(index=i, embedding=[0.1] * 1024) for i in range(len(input))]
        return SimpleNamespace(data=data, usage=SimpleNamespace(cost=0))


def executor(database_url, image_model, chat):
    class TestExecutor(PydanticExecutor):
        web_tools = ()  # FunctionModel can't run OpenRouter's server tools (see test_web.py)

        def model(self, name):
            return FunctionModel(chat)

        def image_model(self, name):
            self.names.append(name)
            return image_model

    run = TestExecutor(database_url, "unused-key", discord_token="bot-token")
    run.names = []
    run.embeddings = SimpleNamespace(embeddings=FakeEmbeddings())
    return run


async def seed(database_url, *messages):
    async with await AsyncConnection.connect(database_url, autocommit=True) as db:
        for id, author, content, attachments in messages:
            raw = {
                "author": {"id": str(author), "username": f"user{author}", "avatar": "abc123"},
                "attachments": attachments,
            }
            await db.execute(
                "INSERT INTO messages (id, guild_id, channel_id, author_id, author_name, content,"
                " created_at, raw) VALUES (%s, %s, 2, %s, %s, %s, now(), %s)",
                (id, GUILD, author, f"user{author}", content, Jsonb(raw)),
            )


def attachment(id, name, content_type="image/jpeg", ex="ffffffff"):
    url = f"https://cdn.discordapp.com/attachments/2/{id}/{name}?ex={ex}&hm=1"
    return {"id": str(id), "filename": name, "content_type": content_type, "url": url}


def calls_generate_image(args, ack):
    """A chat model that queues one picture, then says `ack`."""

    def respond(messages, info):
        if len(messages) == 1:
            return ModelResponse(parts=[ToolCallPart("generate_image", args, tool_call_id="1")])
        return ModelResponse(parts=[TextPart(ack)])

    return respond


async def test_reply_queues_the_drawing_and_its_ack_is_the_models_own_words(database_url):
    await seed(database_url, (70, 300, "Nougie got adopted today!", [attachment(71, "a.jpg")]))
    model = FakeImageModel()
    args = {
        "prompt": "Nougie in a sun hat on a beach",
        "subjects": [
            {"name": "Nougie", "photos": ["70"], "description": "a small white dog"},
            {"name": "Ethan", "person": "u2"},
        ],
        "aspect_ratio": "4:3",
        "caption": "beach day",
    }
    for ack in ["omw to the beach with him 🏖️", "give me a sec, sketching"]:
        run = executor(database_url, model, calls_generate_image(args, ack))
        result = await run.execute(request("openai/gpt-image-2.5-flare"))
        # The reply is the model's text, whatever it is: no stock ack is added.
        assert result.text == ack
        [queued] = result.drawings
        assert queued.prompt == "Nougie in a sun hat on a beach"
        assert queued.caption == "beach day" and queued.aspect_ratio == "4:3"
        assert [(s.name, s.user_id, s.photos) for s in queued.subjects] == [
            ("Nougie", None, ["70"]),
            ("Ethan", "200", []),
        ]
        assert result.images == []
    # Nothing is drawn during the reply.
    assert run.names == [] and model.prompts == []


async def test_bad_subjects_are_tool_errors_the_model_can_fix(database_url):
    await seed(database_url, (80, 300, "no pictures here", []))
    results = []

    for subject in [{"name": "X", "person": "u9"}, {"name": "X", "photos": ["80"]}]:

        def respond(messages, info, subject=subject):
            if len(messages) == 1:
                call = {"prompt": "x", "subjects": [subject]}
                return ModelResponse(parts=[ToolCallPart("generate_image", call, tool_call_id="1")])
            results.append(messages[-1].parts[0].content)
            return ModelResponse(parts=[TextPart("hm")])

        result = await executor(database_url, FakeImageModel(), respond).execute(
            request("test/image")
        )
        assert result.drawings == []
    assert "u9 isn't in this conversation" in results[0]["error"]
    assert "No message with images" in results[1]["error"]


async def test_common_argument_slips_queue_rather_than_fail_the_reply(database_url):
    # Seen in prod: a bare name for a subject, twice, used up the tool's retries.
    # And a person named without their ref is still found as that person.
    await seed(database_url, (90, 200, "hi", []))
    subjects = ["Nougie", "user200", "u2 Ethan (@user200)"]
    args = {"prompt": "x", "subjects": subjects, "aspect_ratio": "5:7"}
    run = executor(database_url, FakeImageModel(), calls_generate_image(args, "on it"))
    [queued] = (await run.execute(request("test/image"))).drawings
    assert [(s.name, s.user_id) for s in queued.subjects] == [
        ("Nougie", None),
        ("user200", "200"),
        ("Ethan", "200"),  # a roster label: by its @username
    ]
    assert queued.aspect_ratio is None


async def test_at_most_max_images_are_queued_per_reply(database_url):
    def respond(messages, info):
        if len(messages) == 1:
            return ModelResponse(
                parts=[
                    ToolCallPart("generate_image", {"prompt": f"frog {i}"}, tool_call_id=str(i))
                    for i in range(3)
                ]
            )
        return ModelResponse(parts=[TextPart("frogs incoming")])

    result = await executor(database_url, FakeImageModel(), respond).execute(request("test/image"))
    assert len(result.drawings) == MAX_IMAGES


async def test_generate_image_is_not_offered_without_an_image_model(database_url):
    offered = []

    def respond(messages, info):
        offered.extend(t.name for t in info.function_tools)
        return ModelResponse(parts=[TextPart("no drawing today")])

    result = await executor(database_url, FakeImageModel(), respond).execute(request(None))
    assert "generate_image" not in offered and "read_image" in offered
    assert result.drawings == []


def discord(files: dict[str, bytes], refreshed: list):
    """Discord's CDN and its link refresh endpoint."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/attachments/refresh-urls"):
            urls = json.loads(request.content)["attachment_urls"]
            refreshed.extend(urls)
            pairs = [{"original": u, "refreshed": u.replace("ex=1&", "ex=ffffffff&")} for u in urls]
            return httpx.Response(200, json={"refreshed_urls": pairs})
        if "ex=1&" in str(request.url):
            return httpx.Response(404)  # expired
        body = files.get(request.url.path)
        if body is None:
            return httpx.Response(404)
        return httpx.Response(200, content=body)

    return httpx.MockTransport(handler)


def image_job(subjects, *, ack="one sec", prompt="Nougie on a beach"):
    return ImageJobRequest(
        id="image:1:1",
        scope={"guild_id": str(GUILD), "channel_id": "2", "conversation_id": "1"},
        image_model="openai/gpt-image-2.5-flare",
        reply_job="reply:1",
        chat_model="test/model",
        ack=ack,
        drawing=Drawing(prompt=prompt, subjects=subjects, caption="da beach"),
    )


async def reply_job(database_url):
    async with await AsyncConnection.connect(database_url, autocommit=True) as db:
        payload = request("test/image").model_dump(mode="json", exclude_none=True)
        await db.execute(
            "INSERT INTO agent_host_jobs (id, request, status) VALUES ('reply:1', %s, 'completed'),"
            " ('image:1:1', '{}', 'running')",
            (Jsonb(payload),),
        )


async def test_drawing_finds_checks_and_numbers_references(database_url):
    await reply_job(database_url)
    await seed(
        database_url,
        # Nougie's photos (one link expired), a meme naming him, and Ethan's selfie.
        (70, 300, "Nougie got adopted today!", [attachment(71, "adopted.jpg", ex="1")]),
        (72, 300, "nougie meme lmao", [attachment(73, "meme.png", "image/png")]),
        (74, 300, "Nougie seeing snow", [attachment(75, "snow.jpg")]),
        (76, 200, "me tonite", [attachment(77, "selfie.jpg")]),
        (78, 300, "unrelated", [attachment(79, "x.jpg")]),
    )
    files = {
        "/attachments/2/71/adopted.jpg": JPEG + b"1",
        "/attachments/2/73/meme.png": PNG + b"meme",
        "/attachments/2/75/snow.jpg": JPEG + b"2",
        "/attachments/2/77/selfie.jpg": JPEG + b"ethan",
        "/avatars/200/abc123.png": PNG + b"avatar",
    }
    checks = []

    def chat(messages, info):
        prompt = messages[-1].parts[0].content
        texts = [p for p in prompt if isinstance(p, str)]
        checks.append(texts)
        if "draw Nougie" in texts[0]:
            # Newest first: snow, meme, adopted. The meme is turned down.
            return ModelResponse(
                parts=[TextPart("Image 1: dog => yes\nImage 2: meme => no\nImage 3: dog => yes")]
            )
        return ModelResponse(
            parts=[TextPart("Image 1: selfie => yes\nImage 2: cartoon avatar => no")]
        )

    model = FakeImageModel()
    run = executor(database_url, model, chat)
    refreshed = []
    run.transport = discord(files, refreshed)
    result = await run.draw(
        image_job(
            [
                Subject(name="Nougie", description="a small white dog"),
                Subject(name="Ethan", user_id="200"),
            ]
        )
    )
    assert result.error is None and result.image is not None
    assert [(r.subject, r.message_id, r.avatar_of) for r in result.references] == [
        ("Nougie", "74", None),
        ("Nougie", "70", None),
        ("Ethan", "76", None),
    ]
    # The image model got those bytes, in that order, and a prompt naming them.
    [sent] = model.references
    assert [r.data for r in sent] == [JPEG + b"2", JPEG + b"1", JPEG + b"ethan"]
    prompt = model.prompts[0][0]
    assert "Nougie, shown in reference images 1-2" in prompt
    assert "Ethan, shown in reference image 3" in prompt
    assert "Style: A real photograph, candid, taken on a phone camera" in prompt
    assert result.prompt == prompt and result.caption == "da beach"
    # The expired link was refreshed through Discord before downloading.
    assert any("adopted.jpg?ex=1&" in u for u in refreshed)
    # The vision check saw captions, and the avatar as a candidate for Ethan.
    assert any('caption: "Nougie got adopted today!"' in t for t in checks[0] + checks[1])
    assert any("(their avatar)" in t for t in checks[0] + checks[1])
    async with await AsyncConnection.connect(database_url, autocommit=True) as db:
        cursor = await db.execute(
            "SELECT job_id, prompt, model, reference_images, subjects, cost_usd"
            " FROM agent_host_images WHERE id = %s",
            (result.image.id,),
        )
        job_id, saved_prompt, saved_model, refs, subjects, cost = await cursor.fetchone()
    assert (job_id, saved_prompt, saved_model) == (
        "image:1:1",
        prompt,
        "openai/gpt-image-2.5-flare",
    )
    assert [r["message_id"] for r in refs] == ["74", "70", "76"]
    assert subjects[0]["name"] == "Nougie" and cost == pytest.approx(0.04)


async def test_kiki_reuses_her_last_self_portrait(database_url):
    await reply_job(database_url)
    async with await AsyncConnection.connect(database_url, autocommit=True) as db:
        await db.execute(
            "INSERT INTO agent_host_images (job_id, mime_type, data, subjects)"
            " VALUES ('reply:1', 'image/png', %s, %s)",
            (PNG + b"me", Jsonb([{"name": "Kiki", "kiki": True}])),
        )
    model = FakeImageModel()
    result = await executor(database_url, model, lambda m, i: None).draw(
        image_job([Subject(name="Kiki", kiki=True)], prompt="Kiki at the beach")
    )
    assert [r.drawing_id is not None for r in result.references] == [True]
    assert [r.data for r in model.references[0]] == [PNG + b"me"]


async def test_a_failed_drawing_gets_a_note_in_kikis_words(database_url):
    await reply_job(database_url)
    seen = []

    def chat(messages, info):
        seen.append(messages)
        return ModelResponse(parts=[TextPart("ok the image model said absolutely not 😭")])

    model = FakeImageModel(error=ContentFilterError("blocked", body=None))
    result = await executor(database_url, model, chat).draw(image_job([], ack="drawing it now!!"))
    assert result.image is None and result.error == "refused"
    assert result.failure_note == "ok the image model said absolutely not 😭"
    # Written from the reply's own conversation, after her ack.
    [messages] = seen
    assert "draw nougie" in json.dumps([str(m) for m in messages])
    assert messages[-2].parts[0].content == "drawing it now!!"
    last = messages[-1].parts[0]
    assert isinstance(last, UserPromptPart) and "refused" in last.content


async def test_a_note_that_fails_too_leaves_rust_its_fallback(database_url):
    await reply_job(database_url)

    def chat(messages, info):
        raise RuntimeError("chat model down")

    model = FakeImageModel(error=UnexpectedModelBehavior("empty", body=None))
    result = await executor(database_url, model, chat).draw(image_job([]))
    assert (result.error, result.failure_note) == ("refused", "")


def test_binary_keeps_only_types_the_image_model_takes():
    assert drawing.binary(PNG).media_type == "image/png"
    assert drawing.binary(b"GIF89a....") is None
    assert isinstance(BinaryContent(PNG, media_type="image/png"), BinaryContent)
