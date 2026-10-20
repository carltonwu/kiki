import asyncio
import base64
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from openai import AsyncOpenAI, BadRequestError
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

from kiki_agent_host.agent.executor import PydanticExecutor
from kiki_agent_host.agent.tools.drawing import MAX_IMAGES
from kiki_agent_host.contracts import Drawing, ImageJobRequest, JobRequest, Subject
from kiki_agent_host.media import drawing, fetch, generation
from kiki_agent_host.media.links import x
from kiki_agent_host.media.vision import ImageRefused
from kiki_agent_host.openrouter.images import (
    MAX_REFERENCES,
    OPENAI_QUALITY,
    OpenRouterImageModel,
    aspect_ratio_for,
    reference_limit,
    safety_violations,
)

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


async def test_a_safety_block_is_a_content_filter_error_and_other_400s_stay_errors():
    # OpenRouter passes OpenAI's 400 through; seen in prod for most failed drawings.
    blocked = (
        "Your request was rejected by the safety system. If you believe this is an error,"
        " contact us at help.openai.com and include the request ID req_1."
        " safety_violations=[sexu…"
    )
    for message, expected in [(blocked, "sexu"), ("Invalid aspect_ratio", None)]:

        def handler(request, message=message):
            return httpx.Response(400, json={"error": {"message": message, "code": 400}})

        model = OpenRouterImageModel("openai/gpt-image-2.5-flare", openrouter(handler))
        if expected is None:
            with pytest.raises(BadRequestError):
                await generation.generate(model, "x")
        else:
            with pytest.raises(ImageRefused, match=r"safety filter blocked this picture \(sexu\)"):
                await generation.generate(model, "x")
    assert safety_violations("rejected by the safety system") == ""
    assert safety_violations("safety_violations=[sexual, violence]") == "sexual, violence"
    assert safety_violations("Alibaba blocked this request through content moderation.") == ""
    assert safety_violations("bad request") is None


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
        (FakeImageModel(error=ContentFilterError("sexual", body=None)), r"filter.*\(sexual\)"),
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


def test_attachments_that_look_like_photos_rank_first():
    # Seen in prod: a 300x225 meme and phone screenshots ranked with selfies.
    now = ((1561578462123266113 >> 22) + drawing.DISCORD_EPOCH_MS) + 1000
    recent = 1561578462123266113

    def rank(name, content_type, width, height, message_id=recent):
        attachment = {"filename": name, "content_type": content_type}
        attachment |= {"width": width, "height": height}
        return drawing.likely(attachment, message_id, now)

    camera = rank("IMG_8637.jpg", "image/jpeg", 2160, 2880)
    screenshot = rank("Screenshot 2026-10-18.png", "image/png", 1179, 2556)
    thumbnail = rank("image0.jpg", "image/jpeg", 300, 225)
    pasted = rank("image.png", "image/png", 1200, 900)
    years_old = rank("IMG_1.jpg", "image/jpeg", 2160, 2880, message_id=1 << 22)
    drawn = rank("kiki-16.png", "image/png", 1024, 1024)
    assert camera > years_old > thumbnail
    assert camera > pasted > screenshot
    assert drawn < -5


def picks(judge):
    """A vision model answering the reference picker: JSON verdicts for each
    candidate image, `judge(label)` giving (subject, clarity) from its label."""

    def respond(messages, info):
        texts = [p for p in messages[-1].parts[0].content if isinstance(p, str)]
        labels = [t for t in texts if t.startswith("Candidate image ")]
        verdicts = []
        for n, label in enumerate(labels, 1):
            subject, clarity = judge(label)
            verdicts.append(
                {
                    "image": n,
                    "shows": label,
                    "kind": "photo" if subject else "meme",
                    "subject": subject,
                    "clarity": clarity,
                }
            )
        return ModelResponse(parts=[TextPart(json.dumps({"verdicts": verdicts}))])

    return respond


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


@pytest.fixture(autouse=True)
def offline_sites(monkeypatch):
    """Link readers find nothing unless a test serves them: nothing reaches the network."""
    monkeypatch.setattr(x, "TRANSPORT", httpx.MockTransport(lambda r: httpx.Response(404)))


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


async def test_nulls_numbers_and_made_up_keys_queue_rather_than_fail_the_reply(database_url):
    # Each of these was a validation error, so a tool retry; a model that repeats
    # its slip runs out of them and the reply fails (UnexpectedModelBehavior).
    await seed(database_url, (90, 200, "hi", [attachment(91, "a.jpg")]))
    args = {
        "prompt": "a meme",
        "subjects": json.dumps(
            [
                {"name": "Ethan", "person": 2, "photos": [90], "description": None},
                {"name": None, "me": True, "photos": None},
            ]
        ),
        "caption": None,
        "edit": None,
        "shared": 90,
        "negative_prompt": "blurry",
    }
    run = executor(database_url, FakeImageModel(), calls_generate_image(args, "on it"))
    [queued] = (await run.execute(request("test/image"))).drawings
    assert [(s.name, s.user_id, s.kiki, s.photos) for s in queued.subjects] == [
        ("Ethan", "200", False, ["90"]),
        ("Kiki", None, True, []),
    ]
    assert queued.caption == "" and not queued.edit and queued.shared == ["90"]


async def test_a_reply_that_runs_out_of_tool_retries_logs_why(database_url, caplog):
    def respond(messages, info):
        return ModelResponse(parts=[ToolCallPart("generate_image", {"caption": "x"})])

    run = executor(database_url, FakeImageModel(), respond)
    with pytest.raises(UnexpectedModelBehavior):
        await run.execute(request("test/image"))
    [line] = [r.getMessage() for r in caplog.records if "model misbehaved" in r.getMessage()]
    assert "retries=generate_image:prompt=missing;" in line
    assert "last_parts=tool-call" in line


async def test_pasted_message_and_attachment_links_are_photos(database_url):
    # Seen in prod: a message link and a pasted CDN link for "use this photo" were
    # dropped, since photos took only message IDs.
    await seed(
        database_url,
        (1561505604110000237, 200, "this is him", [attachment(1561505603338109108, "him.png")]),
    )
    link = "https://discord.com/channels/1/2/1561505604110000237"
    cdn = "https://cdn.discordapp.com/attachments/2/1561505603338109108/him.png?ex=1&hm=2"
    args = {
        "prompt": "x",
        "subjects": [{"name": "Reece", "photos": [link]}, {"name": "Reece 2", "photos": [cdn]}],
    }
    run = executor(database_url, FakeImageModel(), calls_generate_image(args, "on it"))
    [queued] = (await run.execute(request("test/image"))).drawings
    # The attachment link names one image of its message.
    assert [s.photos for s in queued.subjects] == [
        ["1561505604110000237"],
        ["1561505604110000237:1"],
    ]

    results = []

    def respond(messages, info):
        if len(messages) == 1:
            call = {
                "prompt": "x",
                "subjects": [
                    {
                        "name": "X",
                        "photos": [cdn.replace("1561505603338109108", "1561505603338109999")],
                    }
                ],
            }
            return ModelResponse(parts=[ToolCallPart("generate_image", call, tool_call_id="1")])
        results.append(messages[-1].parts[0].content)
        return ModelResponse(parts=[TextPart("hm")])

    await executor(database_url, FakeImageModel(), respond).execute(request("test/image"))
    assert "No message in this server has the attachment" in results[0]["error"]


# Seen in prod: "replace the person in the twitter picture with reece" was refused,
# and the follow-ups that asked again ("no, do exactly as instructed") didn't reply
# to the tweet, so its photos never reached the image model. The tweet was a link
# fixer's repost (its original deleted), with its photos only as link previews.
ORIGINAL, REPOST = 1561562612125147176, 1561562613379235890
FIXER = 1358234666267578368
TWEET = "https://x.com/Sneaky/status/440322224407314432"


def preview(n):
    proxy = f"https://images-ext-1.discordapp.net/external/{n}/https/pbs.twimg.com/media/{n}.jpg"
    return {
        "type": "rich",
        "url": TWEET.replace("x.com", "fxtwitter.com"),
        "image": {"url": f"https://pbs.twimg.com/media/{n}.jpg", "proxy_url": proxy},
    }


async def seed_repost(database_url, embeds=2):
    footer = (
        "-# The original message sent by <@300> was deleted so the bot could fix the "
        "broken embed links for easier viewing."
    )
    rows = [
        (ORIGINAL, 300, False, TWEET, {"author": {"id": "300"}}, "now()"),
        (
            REPOST,
            FIXER,
            True,
            f"<@300>:\n{TWEET.replace('x.com', 'fxtwitter.com')}\n\n{footer}",
            {"author": {"id": str(FIXER)}, "embeds": [preview(n) for n in range(1, embeds + 1)]},
            None,
        ),
    ]
    async with await AsyncConnection.connect(database_url, autocommit=True) as db:
        for id, author, bot, content, raw, deleted in rows:
            await db.execute(
                "INSERT INTO messages (id, guild_id, channel_id, author_id, author_name,"
                " author_bot, content, created_at, raw, deleted_at)"
                f" VALUES (%s, %s, 2, %s, 'x', %s, %s, now(), %s, {deleted or 'NULL'})",
                (id, GUILD, author, bot, content, Jsonb(raw)),
            )


async def test_a_picture_to_redraw_can_be_named_from_earlier_in_the_chat(database_url):
    await seed_repost(database_url)
    await seed(
        database_url,
        (95, 300, "no pictures here", []),
        (96, 300, "", [attachment(97, "meme.png", "image/png")]),
    )
    picks = [
        # The deleted original stands for its repost; :1 is only the first photo.
        ([f"{ORIGINAL}:1"], [f"{REPOST}:1"]),
        # The post's own link, whichever host it was pasted with.
        ([TWEET], [str(REPOST)]),
        ([f"https://discord.com/channels/{GUILD}/2/{REPOST}"], [str(REPOST)]),
    ]
    for shared, expected in picks:
        args = {"prompt": "the tweet's first photo with Reece", "shared": shared}
        run = executor(database_url, FakeImageModel(), calls_generate_image(args, "on it"))
        [queued] = (await run.execute(request("test/image"))).drawings
        assert queued.shared == expected
    # A link preview is a photo too, as an edit's photo or a subject's.
    args = {"prompt": "x", "edit": True, "subjects": [{"name": "Sneaky", "photos": [str(REPOST)]}]}
    run = executor(database_url, FakeImageModel(), calls_generate_image(args, "on it"))
    [queued] = (await run.execute(request("test/image"))).drawings
    assert queued.subjects[0].photos == [str(REPOST)]

    errors = []
    for shared in [["95"], ["96:2"], ["https://x.com/someone/status/77"]]:

        def respond(messages, info, shared=shared):
            if len(messages) == 1:
                call = {"prompt": "x", "shared": shared}
                return ModelResponse(parts=[ToolCallPart("generate_image", call, tool_call_id="1")])
            errors.append(str(messages[-1].parts[0].content))
            return ModelResponse(parts=[TextPart("hm")])

        result = await executor(database_url, FakeImageModel(), respond).execute(
            request("test/image")
        )
        assert result.drawings == []
    assert "No images in ['95']" in errors[0]
    # It has one. (A linked post isn't counted here: the site may have more.)
    assert "No images in ['96:2']" in errors[1]
    assert "No message in this channel shares" in errors[2]


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

    judged = picks(lambda label: ("meme" not in label and "avatar" not in label, 2))

    def chat(messages, info):
        checks.append([p for p in messages[-1].parts[0].content if isinstance(p, str)])
        return judged(messages, info)

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


async def test_shared_images_come_last_and_given_photos_are_not_sent_twice(database_url):
    # Seen in prod: the meme someone replied to never reached the image model, and a
    # given photo found again in the history was sent twice.
    await reply_job(database_url)
    await seed(
        database_url,
        (80, 300, "Nico", [attachment(81, "nico.jpg")]),
        (82, 300, "", [attachment(83, "meme.png", "image/png")]),
    )
    files = {
        "/attachments/2/81/nico.jpg": JPEG + b"nico",
        "/attachments/2/83/meme.png": PNG + b"meme",
    }

    chat = picks(lambda label: (True, 3))

    model = FakeImageModel()
    run = executor(database_url, model, chat)
    run.transport = discord(files, [])
    job = image_job([Subject(name="Nico", photos=["80"])], prompt="Nico in the meme")
    job.drawing.shared = ["82", "80"]
    result = await run.draw(job)
    assert [(r.subject, r.message_id) for r in result.references] == [
        ("Nico", "80"),
        ("shared", "82"),
    ]
    assert [r.data for r in model.references[0]] == [JPEG + b"nico", PNG + b"meme"]
    prompt = model.prompts[0][0]
    assert "Nico, shown in reference image 1" in prompt
    assert "Reference image 2 is what they shared with the request" in prompt


async def test_a_shared_link_previews_image_is_a_reference(database_url):
    # Seen in prod: "replace Sneaky with Reece" about a pasted tweet; the tweet's
    # photo is a link preview (an embed, no attachment ID) and was dropped.
    await reply_job(database_url)
    proxy = "https://images-ext-1.discordapp.net/external/abc/https/pbs.twimg.com/media/x.jpg"
    embed = {
        "type": "rich",
        "url": "https://fxtwitter.com/Sneaky/status/1",
        "image": {"url": "https://pbs.twimg.com/media/x.jpg", "proxy_url": proxy},
    }
    async with await AsyncConnection.connect(database_url, autocommit=True) as db:
        await db.execute(
            "INSERT INTO messages (id, guild_id, channel_id, author_id, author_name, content,"
            " created_at, raw) VALUES (84, %s, 2, 300, 'user300', 'https://fxtwitter.com/x',"
            " now(), %s)",
            (GUILD, Jsonb({"author": {"id": "300"}, "embeds": [embed]})),
        )
    model = FakeImageModel()
    run = executor(database_url, model, lambda messages, info: None)
    run.transport = discord({"/external/abc/https/pbs.twimg.com/media/x.jpg": JPEG + b"tweet"}, [])
    job = image_job([], prompt="The tweet's photo with Reece instead of Sneaky")
    job.drawing.shared = ["84"]
    result = await run.draw(job)
    assert [(r.subject, r.message_id, r.attachment_id, r.url) for r in result.references] == [
        ("shared", "84", None, proxy)
    ]
    assert [r.data for r in model.references[0]] == [JPEG + b"tweet"]


async def test_picked_images_keep_their_order_and_one_can_be_chosen(database_url):
    await reply_job(database_url)
    await seed_repost(database_url)
    await seed(database_url, (96, 300, "", [attachment(97, "meme.png", "image/png")]))
    files = {
        "/attachments/2/97/meme.png": PNG + b"meme",
        "/external/2/https/pbs.twimg.com/media/2.jpg": JPEG + b"second",
    }
    model = FakeImageModel()
    run = executor(database_url, model, lambda messages, info: None)
    run.transport = discord(files, [])
    job = image_job([], prompt="x")
    # Given in this order (lower IDs aren't first), and only the tweet's 2nd photo.
    job.drawing.shared = [f"{REPOST}:2", "96"]
    result = await run.draw(job)
    assert [(r.message_id, r.attachment_id) for r in result.references] == [
        (str(REPOST), None),
        ("96", "97"),
    ]
    assert [r.data for r in model.references[0]] == [JPEG + b"second", PNG + b"meme"]


async def test_a_linked_posts_photos_come_from_the_site(database_url, monkeypatch):
    # A link in <...> has no preview at all, and Discord's preview of an X post
    # shows at most one photo: the post's photos are fetched from the site.
    await reply_job(database_url)
    await seed(database_url, (98, 300, f"put me in this <{TWEET}>", []))
    photo = json.loads(
        next(
            (Path(__file__).parent / "fixtures/fxtwitter").glob("*-440322224407314432.json")
        ).read_text()
    )

    def fxtwitter(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=photo)

    def site(request: httpx.Request) -> httpx.Response:
        if request.url.host == "pbs.twimg.com" and request.url.params.get("name") == "medium":
            return httpx.Response(200, content=JPEG + b"tweet")
        return httpx.Response(404)

    monkeypatch.setattr(x, "TRANSPORT", httpx.MockTransport(fxtwitter))
    model = FakeImageModel()
    run = executor(database_url, model, lambda messages, info: None)
    run.transport = httpx.MockTransport(site)
    job = image_job([], prompt="x")
    job.drawing.shared = ["98"]
    result = await run.draw(job)
    assert [(r.message_id, r.url) for r in result.references] == [
        ("98", "https://pbs.twimg.com/media/BhxWutnCEAAtEQ6.jpg?name=medium")
    ]
    assert [r.data for r in model.references[0]] == [JPEG + b"tweet"]


def test_models_get_the_references_and_ratios_they_take():
    assert reference_limit("openai/gpt-image-2.5-flare") == 16
    assert reference_limit("qwen/qwen-image-2.1-turbo") == 4
    assert reference_limit("google/gemini-2.5-flash-image") == MAX_REFERENCES
    assert aspect_ratio_for("qwen/qwen-image-2.1-turbo", "21:9") == "16:9"
    assert aspect_ratio_for("qwen/qwen-image-2.1-turbo", "4:3") == "4:3"
    assert aspect_ratio_for("openai/gpt-image-2.5-flare", "21:9") == "21:9"
    assert aspect_ratio_for("google/gemini-2.5-flash-image", "21:9") == "21:9"
    assert aspect_ratio_for("qwen/qwen-image-2.1-turbo", None) is None


def test_trimming_keeps_each_subjects_best_references_first():
    groups = [["e1", "e2", "e3"], ["n1", "n2", "n3"], ["s1", "s2"]]
    assert drawing.trim(groups, 4) == [["e1", "e2"], ["n1"], ["s1"]]
    assert drawing.trim(groups, 16) == groups
    assert drawing.trim([[], ["a"]], 0) == [[], []]


async def test_a_safety_block_is_drawn_by_the_fallback_with_the_references_it_takes(database_url):
    # Seen in prod: most failed drawings were OpenAI safety blocks of campy requests.
    await reply_job(database_url)
    await seed(
        database_url,
        *[(90 + i, 300, f"Nico pic {i}", [attachment(190 + i, f"n{i}.jpg")]) for i in range(5)],
    )
    files = {f"/attachments/2/{190 + i}/n{i}.jpg": JPEG + bytes([i]) for i in range(5)}
    primary = FakeImageModel(error=ContentFilterError("sexual", body=None))
    fallback = FakeImageModel(image=JPEG + b"qwen", media_type="image/jpeg")
    models = {"openai/gpt-image-2.5-flare": primary, "qwen/qwen-image-2.1-turbo": fallback}

    class Fallback(PydanticExecutor):
        def image_model(self, name):
            return models[name]

    run = Fallback(database_url, "unused-key", image_fallback_model="qwen/qwen-image-2.1-turbo")
    run.transport = discord(files, [])
    job = image_job(
        [Subject(name="Nico", photos=["90", "91", "92"]), Subject(name="Ethan", photos=["93"])],
        prompt="Nico at the bar",
    )
    job.drawing.shared = ["94"]
    job.drawing.aspect_ratio = "21:9"
    result = await run.draw(job)
    assert result.error is None and result.model == "qwen/qwen-image-2.1-turbo"
    # The primary got every reference; the fallback its four, each subject's best first.
    assert len(primary.references[0]) == 5
    assert [(r.subject, r.message_id) for r in result.references] == [
        ("Nico", "90"),
        ("Nico", "91"),
        ("Ethan", "93"),
        ("shared", "94"),
    ]
    prompt, settings = fallback.prompts[0]
    assert "Nico, shown in reference images 1-2" in prompt
    assert "Ethan, shown in reference image 3" in prompt
    assert "Reference image 4 is what they shared" in prompt
    assert settings == {"aspect_ratio": "16:9"}
    async with await AsyncConnection.connect(database_url, autocommit=True) as db:
        cursor = await db.execute(
            "SELECT model FROM agent_host_images WHERE id = %s", (result.image.id,)
        )
        assert (await cursor.fetchone())[0] == "qwen/qwen-image-2.1-turbo"

    # Blocked by both: the note says so, with the server's model's prompt recorded.
    fallback.error = ContentFilterError("", body=None)

    def chat(messages, info):
        return ModelResponse(parts=[TextPart("both said no")])

    run.model = lambda name: FunctionModel(chat)
    result = await run.draw(job)
    assert result.error == "refused" and result.failure_note == "both said no"
    assert len(result.references) == 5 and "Nico at the bar" in result.prompt


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
    assert isinstance(last, UserPromptPart) and "safety filter" in last.content
    assert "Asking again won't get it through" in last.content


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


async def test_the_picker_takes_the_clearest_photos_of_several_candidates(database_url):
    # Seen in prod: the first approved candidates were taken in search order, and
    # a meme of a monkey and a game character were taken for Reece.
    await reply_job(database_url)
    await seed(
        database_url,
        (100, 200, "me at the lake", [attachment(101, "IMG_1.jpg")]),
        (102, 200, "me lol", [attachment(103, "IMG_2.jpg")]),
        (104, 200, "my soup", [attachment(105, "IMG_3.jpg")]),
        (106, 300, "user200 as a monkey", [attachment(107, "monkey.png", "image/png")]),
    )
    files = {
        "/attachments/2/101/IMG_1.jpg": JPEG + b"lake",
        "/attachments/2/103/IMG_2.jpg": JPEG + b"close",
        "/attachments/2/105/IMG_3.jpg": JPEG + b"soup",
        "/attachments/2/107/monkey.png": PNG + b"monkey",
    }
    clarity = {"lake": 1, "lol": 3}
    prompts = []
    judged = picks(
        lambda label: next(((True, c) for k, c in clarity.items() if k in label), (False, 0))
    )

    def chat(messages, info):
        prompts.append([p for p in messages[-1].parts[0].content if isinstance(p, str)])
        return judged(messages, info)

    model = FakeImageModel()
    run = executor(database_url, model, chat)
    run.transport = discord(files, [])
    job = image_job([Subject(name="user200", user_id="200")], prompt="a portrait")
    result = await run.draw(job)
    # The clear close-up first, then the blurry lake photo; soup and monkey are out.
    assert [r.message_id for r in result.references] == ["102", "100"]
    [texts] = prompts
    assert sum(t.startswith("Candidate image ") for t in texts) == 4  # (no avatar served)
    assert 'The picture to draw: "a portrait"' in texts[0]


async def test_given_photos_anchor_the_picker_and_bad_answers_get_one_retry(database_url):
    await reply_job(database_url)
    await seed(
        database_url,
        (110, 300, "this is what reece looks like", [attachment(111, "reece.jpg")]),
        (112, 300, "Reece at the beach", [attachment(113, "beach.jpg")]),
    )
    files = {
        "/attachments/2/111/reece.jpg": JPEG + b"reece",
        "/attachments/2/113/beach.jpg": JPEG + b"beach",
    }
    requests = []
    judged = picks(lambda label: (True, 2))

    def chat(messages, info):
        requests.append(messages)
        if len(messages) == 1:  # skips the only candidate: retried through ModelRetry
            return ModelResponse(parts=[TextPart('{"verdicts": []}')])
        return judged(messages[:1], info)

    model = FakeImageModel()
    run = executor(database_url, model, chat)
    run.transport = discord(files, [])
    result = await run.draw(image_job([Subject(name="Reece", photos=["110"])]))
    assert [r.message_id for r in result.references] == ["110", "112"]
    assert len(requests) == 2  # the first answer had no verdict for image 1
    texts = [p for p in requests[0][0].parts[0].content if isinstance(p, str)]
    assert "Known photo A of Reece (not a candidate)" in texts
    assert "the same one as in the known photos" in texts[0]
    assert [t for t in texts if t.startswith("Candidate image ")] == [
        'Candidate image 1; caption: "Reece at the beach"'
    ]


async def test_photos_cited_in_memory_are_candidates(database_url):
    await reply_job(database_url)
    await seed(database_url, (120, 300, "look at her go", [attachment(121, "IMG_9.jpg")]))
    async with await AsyncConnection.connect(database_url, autocommit=True) as db:
        cursor = await db.execute(
            "INSERT INTO memory_subjects (guild_id, kind, name, name_norm)"
            " VALUES (%s, 'thing', 'Piper', name_norm('Piper')) RETURNING id",
            (GUILD,),
        )
        [subject] = await cursor.fetchone()
        await db.execute(
            "INSERT INTO memory_entries (doc_id, kind, text, sources, first_observed_at,"
            " last_observed_at) SELECT id, 'fact', 'Piper is a merle dog', '{120}', now(), now()"
            " FROM memory_docs WHERE subject_id = %s",
            (subject,),
        )
    model = FakeImageModel()
    run = executor(database_url, model, picks(lambda label: (True, 3)))
    run.transport = discord({"/attachments/2/121/IMG_9.jpg": JPEG + b"piper"}, [])
    result = await run.draw(image_job([Subject(name="Piper")], prompt="Piper in snow"))
    # The caption doesn't name her: only her memory points at the photo.
    assert [r.message_id for r in result.references] == ["120"]


async def test_a_name_two_people_had_means_the_one_going_by_it(database_url):
    # Seen in prod: Ethan had once been nicknamed "Reece", so "Reece" meant nobody
    # and Reece was searched for as a thing.
    await seed(database_url, (130, 200, "hi", []), (131, 400, "hey", []))
    async with await AsyncConnection.connect(database_url, autocommit=True) as db:
        await db.execute(
            "INSERT INTO user_names (guild_id, user_id, kind, name, name_norm, first_seen,"
            " last_seen) VALUES (%s, 200, 'nick', 'Reece', 'reece', now(), now()),"
            " (%s, 400, 'nick', 'Reece', 'reece', now(), now()) ON CONFLICT DO NOTHING",
            (GUILD, GUILD),
        )
        await db.execute(
            "INSERT INTO people (guild_id, user_id, username, nick, seen_at) VALUES"
            " (%s, 200, 'user200', 'Ethan', now()), (%s, 400, 'rqc2', 'Reece', now())"
            " ON CONFLICT (guild_id, user_id) DO UPDATE SET nick = excluded.nick",
            (GUILD, GUILD),
        )
    args = {"prompt": "x", "subjects": [{"name": "Reece"}]}
    run = executor(database_url, FakeImageModel(), calls_generate_image(args, "on it"))
    [queued] = (await run.execute(request("test/image"))).drawings
    assert [(s.name, s.user_id) for s in queued.subjects] == [("Reece", "400")]


async def test_a_message_id_in_a_name_is_a_photo_and_kikis_drawings_are_not(database_url):
    # Seen in prod: "Reece (from the selfie he shared, message …)" with no photos,
    # and Kiki's own picture of Reece given as his photo.
    await reply_job(database_url)
    await seed(database_url, (1561574753435652146, 300, "me", [attachment(141, "me.jpg")]))
    async with await AsyncConnection.connect(database_url, autocommit=True) as db:
        # Her saved drawings, posted in every format Rust names by type, and another
        # bot's file that only looks like hers (no saved drawing has its number).
        await db.execute(
            "INSERT INTO agent_host_images (id, job_id, mime_type, data) VALUES"
            " (16, 'reply:1', 'image/png', 'x'), (17, 'reply:1', 'image/jpeg', 'x'),"
            " (18, 'reply:1', 'image/webp', 'x'), (19, 'reply:1', 'image/gif', 'x')"
        )
        for id, name, mime in (
            (142, "kiki-16.png", "image/png"),
            (144, "kiki-17.jpg", "image/jpeg"),
            (146, "kiki-18.webp", "image/webp"),
            (148, "kiki-19.gif", "image/gif"),
            (150, "picture.png", "image/png"),  # her image job's follow-up, by provenance
            (152, "kiki-99.png", "image/png"),  # another bot's
        ):
            await db.execute(
                "INSERT INTO messages (id, guild_id, channel_id, author_id, author_name,"
                " author_bot, content, created_at, raw) VALUES (%s, %s, 2, 999, 'Kiki', true,"
                " 'starring reece', now(), %s)",
                (id, GUILD, Jsonb({"attachments": [attachment(id + 1, name, mime)]})),
            )
        cursor = await db.execute(
            "INSERT INTO conversations (guild_id, channel_id, started_by) VALUES (%s, 2, 200)"
            " RETURNING id",
            (GUILD,),
        )
        [conversation] = await cursor.fetchone()
        cursor = await db.execute(
            "INSERT INTO agent_runs (conversation_id, trigger_message_ids) VALUES (%s, '{1}')"
            " RETURNING id",
            (conversation,),
        )
        [run_id] = await cursor.fetchone()
        await db.execute(
            "INSERT INTO agent_image_jobs (run_id, position, reply_to_id, request, status,"
            " message_id) VALUES (%s, 1, 1, '{}', 'done', 150)",
            (run_id,),
        )
    name = "Reece (from the selfie he shared, message 1561574753435652146)"
    args = {"prompt": "x", "subjects": [{"name": name}]}
    run = executor(database_url, FakeImageModel(), calls_generate_image(args, "on it"))
    [queued] = (await run.execute(request("test/image"))).drawings
    assert [(s.name, s.photos) for s in queued.subjects] == [("Reece", ["1561574753435652146"])]

    for drawn in ("142", "144", "146", "148", "150"):
        results = []

        def respond(messages, info, drawn=drawn, results=results):
            if len(messages) == 1:
                call = {"prompt": "x", "subjects": [{"name": "Reece", "photos": [drawn]}]}
                return ModelResponse(parts=[ToolCallPart("generate_image", call, tool_call_id="1")])
            results.append(messages[-1].parts[0].content)
            return ModelResponse(parts=[TextPart("hm")])

        result = await executor(database_url, FakeImageModel(), respond).execute(
            request("test/image")
        )
        assert result.drawings == []
        assert f"{drawn} is a picture you drew, not a photo of Reece" in results[0]["error"]

    # Another bot's look-alike file name isn't hers: it's queued as a photo.
    args = {"prompt": "x", "subjects": [{"name": "Reece", "photos": ["152"]}]}
    run = executor(database_url, FakeImageModel(), calls_generate_image(args, "on it"))
    [queued] = (await run.execute(request("test/image"))).drawings
    assert queued.subjects[0].photos == ["152"]


def test_only_message_ids_in_a_name_become_photos():
    # Review of #66: a mention lost its ID to photos, and a message link gave its
    # guild and channel IDs as photos.
    from kiki_agent_host.agent.tools.drawing import Pictured

    mention = Pictured.model_validate({"name": "<@123456789012345678>"})
    assert (mention.name, mention.photos) == ("<@123456789012345678>", [])
    nick = Pictured.model_validate({"name": "<@!123456789012345678>"})
    assert (nick.name, nick.photos) == ("<@!123456789012345678>", [])
    link = Pictured.model_validate(
        {
            "name": "Reece (https://discord.com/channels/348273727298863104/"
            "925198822802612305/1561574753435652146)"
        }
    )
    assert (link.name, link.photos) == ("Reece", ["1561574753435652146"])
    both = Pictured.model_validate({"name": "<@123456789012345678> in 1561574753435652146"})
    assert (both.name, both.photos) == ("<@123456789012345678> in", ["1561574753435652146"])


async def test_a_mention_as_a_name_is_that_person_by_their_name(database_url):
    await seed(database_url, (150, 200, "hi", []))
    args = {"prompt": "x", "subjects": [{"name": "<@200>"}]}
    run = executor(database_url, FakeImageModel(), calls_generate_image(args, "on it"))
    [queued] = (await run.execute(request("test/image"))).drawings
    assert [(s.name, s.user_id, s.photos) for s in queued.subjects] == [("Ethan", "200", [])]


async def names(database_url, *people):
    """People (user ID, channel they spoke in, their names now) who were all once
    nicknamed Reece."""
    async with await AsyncConnection.connect(database_url, autocommit=True) as db:
        for user, channel, nick in people:
            await db.execute(
                "INSERT INTO messages (id, guild_id, channel_id, author_id, author_name, content,"
                " created_at, raw) VALUES (%s, %s, %s, %s, 'x', 'hi', now(), '{}')",
                (user + 1, GUILD, channel, user),
            )
            await db.execute(
                "INSERT INTO user_names (guild_id, user_id, kind, name, name_norm, first_seen,"
                " last_seen) VALUES (%s, %s, 'nick', 'Reece', 'reece', now(), now())"
                " ON CONFLICT DO NOTHING",
                (GUILD, user),
            )
            await db.execute(
                "INSERT INTO people (guild_id, user_id, username, nick, seen_at) VALUES"
                " (%s, %s, %s, %s, now()) ON CONFLICT (guild_id, user_id) DO UPDATE"
                " SET nick = excluded.nick, username = excluded.username",
                (GUILD, user, f"user{user}", nick),
            )


async def test_people_going_by_one_name_narrow_to_this_conversation_or_are_asked_about(
    database_url,
):
    # Review of #66: two people going by "Reece" now still fell through to a
    # thing-style search by name.
    await names(database_url, (200, 3, "Reece"), (400, 3, "Reece"), (500, 2, "Reece"))
    args = {"prompt": "x", "subjects": [{"name": "Reece"}]}

    def queue():
        return executor(database_url, FakeImageModel(), calls_generate_image(args, "on it"))

    # u2 (200) is in the conversation.
    [queued] = (await queue().execute(request("test/image"))).drawings
    assert [s.user_id for s in queued.subjects] == ["200"]

    # Not in the roster: the one who spoke in this channel (500, in channel 2).
    people = [{"ref": "u1", "user_id": "100", "label": "Carlton"}]
    job = request("test/image")
    job.people = [type(job.people[0]).model_validate(p) for p in people]
    [queued] = (await queue().execute(job)).drawings
    assert [s.user_id for s in queued.subjects] == ["500"]


async def test_a_name_still_ambiguous_is_a_tool_error_not_a_thing(database_url):
    await names(database_url, (400, 3, "Reece"), (500, 3, "Reece"))
    results = []

    def respond(messages, info):
        if len(messages) == 1:
            call = {"prompt": "x", "subjects": [{"name": "Reece"}]}
            return ModelResponse(parts=[ToolCallPart("generate_image", call, tool_call_id="1")])
        results.append(messages[-1].parts[0].content)
        return ModelResponse(parts=[TextPart("which Reece?")])

    result = await executor(database_url, FakeImageModel(), respond).execute(request("test/image"))
    assert result.drawings == []
    assert "'Reece' could be" in results[0]["error"]
    assert "user400" in results[0]["error"] and "user500" in results[0]["error"]


async def test_contradictory_verdicts_for_one_image_are_retried(database_url):
    # Review of #66: a "no" and a "yes" for image 1 counted as a yes.
    await reply_job(database_url)
    await seed(database_url, (160, 300, "Bear at the vet", [attachment(161, "IMG_1.jpg")]))
    calls = []

    def chat(messages, info):
        calls.append(messages)
        verdict = {"image": 1, "shows": "a dog", "kind": "photo", "subject": True, "clarity": 3}
        if len(messages) == 1:
            no = verdict | {"subject": False, "clarity": 0}
            return ModelResponse(parts=[TextPart(json.dumps({"verdicts": [no, verdict]}))])
        return ModelResponse(parts=[TextPart(json.dumps({"verdicts": [verdict | {"clarity": 2}]}))])

    model = FakeImageModel()
    run = executor(database_url, model, chat)
    run.transport = discord({"/attachments/2/161/IMG_1.jpg": JPEG + b"bear"}, [])
    result = await run.draw(image_job([Subject(name="Bear")], prompt="Bear in space"))
    assert len(calls) == 2  # the contradiction was sent back
    assert [r.message_id for r in result.references] == ["160"]

    # Still contradictory after the retry: no references rather than a guess.
    def stubborn(messages, info):
        verdict = {"image": 1, "shows": "a dog", "kind": "photo", "subject": True, "clarity": 3}
        return ModelResponse(parts=[TextPart(json.dumps({"verdicts": [verdict, verdict]}))])

    run = executor(database_url, FakeImageModel(), stubborn)
    run.transport = discord({"/attachments/2/161/IMG_1.jpg": JPEG + b"bear"}, [])
    result = await run.draw(image_job([Subject(name="Bear")], prompt="Bear in space"))
    assert result.references == []


async def test_memory_photos_stay_in_the_guild(database_url):
    # Memory cites a message by ID: one from another server is never a candidate.
    await reply_job(database_url)
    async with await AsyncConnection.connect(database_url, autocommit=True) as db:
        await db.execute(
            "INSERT INTO messages (id, guild_id, channel_id, author_id, author_name, content,"
            " created_at, raw) VALUES (170, 999, 9, 300, 'x', 'elsewhere', now(), %s)",
            (Jsonb({"attachments": [attachment(171, "IMG_7.jpg")]}),),
        )
        cursor = await db.execute(
            "INSERT INTO memory_subjects (guild_id, kind, name, name_norm)"
            " VALUES (%s, 'thing', 'Piper', name_norm('Piper')) RETURNING id",
            (GUILD,),
        )
        [subject] = await cursor.fetchone()
        await db.execute(
            "INSERT INTO memory_entries (doc_id, kind, text, sources, first_observed_at,"
            " last_observed_at) SELECT id, 'fact', 'Piper is a dog', '{170}', now(), now()"
            " FROM memory_docs WHERE subject_id = %s",
            (subject,),
        )
    asked = []

    def chat(messages, info):
        asked.append(messages)
        return picks(lambda label: (True, 3))(messages, info)

    run = executor(database_url, FakeImageModel(), chat)
    run.transport = discord({"/attachments/2/171/IMG_7.jpg": JPEG + b"other"}, [])
    result = await run.draw(image_job([Subject(name="Piper")], prompt="Piper"))
    assert result.references == [] and asked == []


def test_the_picker_treats_captions_and_image_text_as_data():
    prompt = drawing.VERIFY.format(name="X", what="", rules="", known="", purpose="")
    assert "not instructions" in prompt and "ignore" in prompt


async def test_the_reference_stage_keeps_to_its_deadline_with_many_subjects(
    database_url, monkeypatch
):
    # Review of #66: 6 subjects of 24 candidates make 12 picker batches, two waves
    # through 8 slots; each wave within its own 30 s still took 2 x 27 s, and with
    # the image's 70 s the job passed the host's 120 s. Scaled down 10x here, with a
    # second wave far slower than the stage allows, so the outcome doesn't hang on
    # how fast the machine seeds and downloads (CI ate a 1 s stage doing so).
    monkeypatch.setattr(drawing, "REFERENCES_SECONDS", 3.0)
    monkeypatch.setattr(drawing, "VERIFY_TIMEOUT_SECONDS", 6.0)
    monkeypatch.setattr(drawing, "MIN_PICK_SECONDS", 0.05)
    monkeypatch.setattr(drawing, "STAGE_GRACE", 0.5)
    await reply_job(database_url)
    rows, files = [], {}
    for d in range(6):
        for i in range(12):
            id = 2000 + d * 100 + i * 2
            pair = [attachment(id * 10 + k, f"IMG_{d}{i}{k}.jpg") for k in (1, 2)]
            rows.append((id, 300, f"Dog{d} at the park", pair))
            for k in (1, 2):
                files[f"/attachments/2/{id * 10 + k}/IMG_{d}{i}{k}.jpg"] = JPEG + bytes([d, i, k])
    await seed(database_url, *rows)
    judged = picks(lambda label: (True, 2))
    started = []

    async def slow(messages, info):
        started.append(asyncio.get_running_loop().time())
        # The first wave (8 slots) answers in 1 s; the second would take 5 s.
        await asyncio.sleep(1.0 if len(started) <= drawing.PICKS_AT_ONCE else 5.0)
        return judged(messages, info)

    model = FakeImageModel()
    run = executor(database_url, model, slow)
    run.transport = discord(files, [])
    job = image_job([Subject(name=f"Dog{d}") for d in range(6)], prompt="six dogs")
    began = asyncio.get_running_loop().time()
    result = await run.draw(job)
    elapsed = asyncio.get_running_loop().time() - began
    # One deadline for the stage, queue waits included: the second wave gets what's
    # left of it, not its own 6 s (6 s or more without the fix; cut at 3 + 0.5 s).
    assert elapsed < 4.5
    assert len(started) >= drawing.PICKS_AT_ONCE  # the first wave ran
    assert result.error is None and len(model.prompts) == 1
    # What the first wave judged is kept: at least 4 subjects have their 3.
    counts = [sum(r.subject == f"Dog{d}" for r in result.references) for d in range(6)]
    assert sum(c == 3 for c in counts) >= 4


async def test_a_batch_that_cant_start_in_time_is_skipped(database_url, monkeypatch):
    # Past the stage's deadline nothing new starts: no call, no references.
    monkeypatch.setattr(drawing, "MIN_PICK_SECONDS", 0.5)
    await reply_job(database_url)
    await seed(database_url, (3000, 300, "Bear at the vet", [attachment(3001, "IMG_1.jpg")]))
    calls = []

    def chat(messages, info):
        calls.append(messages)
        return picks(lambda label: (True, 3))(messages, info)

    run = executor(database_url, FakeImageModel(), chat)
    run.transport = discord({"/attachments/2/3001/IMG_1.jpg": JPEG + b"bear"}, [])
    async with await AsyncConnection.connect(database_url, autocommit=True) as db:
        from kiki_agent_host.chat.scope import Scope
        from kiki_agent_host.media.vision import Vision

        vision = Vision(FunctionModel(chat), "test/vision", discord_token="bot-token")
        gather = drawing.Gatherer(
            db,
            Scope(GUILD, 2, 1),
            vision,
            "https://cdn.discordapp.com",
            run.transport,
            seconds=0.3,
        )
        found, shared = await gather.everything(
            Drawing(prompt="x", subjects=[Subject(name="Bear")])
        )
    assert found[0].images == [] and shared == [] and calls == []


def gatherer(db, chat, transport, seconds=None):
    from kiki_agent_host.chat.scope import Scope
    from kiki_agent_host.media.vision import Vision

    vision = Vision(FunctionModel(chat), "test/vision", discord_token="bot-token")
    return drawing.Gatherer(
        db, Scope(GUILD, 2, 1), vision, "https://cdn.discordapp.com", transport, seconds=seconds
    )


async def test_cancelling_the_reference_stage_stops_its_calls_and_downloads(database_url):
    # Review of #66: cancelled while awaiting asyncio.wait, the stage left a picker
    # call and a download running (and billing) after the job had given up.
    await reply_job(database_url)
    await seed(
        database_url,
        (4000, 300, "Bear at the vet", [attachment(4001, "IMG_1.jpg")]),
        (4002, 300, "Nougie at the lake", [attachment(4003, "IMG_2.jpg")]),
    )
    picking, picker_stopped, download_stopped = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def slow_picker(messages, info):
        picking.set()
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            picker_stopped.set()
            raise

    async def cdn(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("IMG_2.jpg"):  # Nougie's download hangs
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                download_stopped.set()
                raise
        return httpx.Response(200, content=JPEG + b"bear")

    async with await AsyncConnection.connect(database_url, autocommit=True) as db:
        gather = gatherer(db, slow_picker, httpx.MockTransport(cdn))
        plan = Drawing(prompt="x", subjects=[Subject(name="Bear"), Subject(name="Nougie")])
        stage = asyncio.create_task(gather.everything(plan))
        await asyncio.wait_for(picking.wait(), 5)
        stage.cancel()
        with pytest.raises(asyncio.CancelledError):
            await stage
        # Nothing of the stage is left running.
        assert picker_stopped.is_set() and download_stopped.is_set()


async def test_the_stage_never_outlasts_the_jobs_own_deadline(monkeypatch):
    # With #69's job deadline: a resumed job with 10 s left gets a 10 s stage.
    monkeypatch.setattr(drawing, "job_time_left", lambda: 10.0)
    gather = drawing.Gatherer(None, None, None, "", seconds=35)
    assert 9.5 < gather.time_left() <= 10.0
    monkeypatch.setattr(drawing, "job_time_left", lambda: None)
    assert 34.5 < drawing.Gatherer(None, None, None, "", seconds=35).time_left() <= 35


async def test_a_paid_picker_response_is_billed_when_its_retry_fails(
    database_url, monkeypatch, recorded
):
    # Review of #66: a response costing $0.0123 with bad verdicts, then a retry
    # that timed out, recorded $0.
    monkeypatch.setattr(drawing, "VERIFY_TIMEOUT_SECONDS", 0.3)
    monkeypatch.setattr(drawing, "MIN_PICK_SECONDS", 0.05)
    await reply_job(database_url)
    await seed(database_url, (4100, 300, "Bear at the vet", [attachment(4101, "IMG_1.jpg")]))
    transport = discord({"/attachments/2/4101/IMG_1.jpg": JPEG + b"bear"}, [])
    bad = ModelResponse(parts=[TextPart('{"verdicts": []}')], provider_details={"cost": 0.0123})

    async def then_slow(messages, info):
        if len(messages) == 1:
            return bad
        await asyncio.sleep(5)

    def always_bad(messages, info):
        return bad

    plan = Drawing(prompt="x", subjects=[Subject(name="Bear")])
    async with await AsyncConnection.connect(database_url, autocommit=True) as db:
        gather = gatherer(db, then_slow, transport)
        found, _ = await gather.everything(plan)
        assert found[0].images == []
        assert gather.vision.cost == pytest.approx(0.0123)
        # Out of validation retries: both responses were paid for.
        gather = gatherer(db, always_bad, transport)
        found, _ = await gather.everything(plan)
        assert found[0].images == []
        assert gather.vision.cost == pytest.approx(0.0246)
    # And in telemetry, for both failed calls (a timeout, then a validation failure).
    labels = {"purpose": "vision", "model": "test/vision"}
    assert recorded.value("kiki.llm.cost_usd", **labels) == pytest.approx(0.0369)
    assert recorded.value("kiki.llm.calls", **labels, result="ok") is None
