"""Pydantic AI executor: runs reply and topic jobs (the SDK owns the loop; the
tools are in `tools/`), draws queued pictures, and describes custom emoji."""

import asyncio
import logging
import os
import time

import httpx
from openai import APIError, APIStatusError
from psycopg import OperationalError
from pydantic_ai import Agent
from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError
from pydantic_ai.images import ImageGenerationModel
from pydantic_ai.messages import BinaryContent, ModelResponse
from pydantic_ai.models import Model
from pydantic_ai.providers.openrouter import OpenRouterProvider
from pydantic_ai.usage import UsageLimits

from .. import db
from ..chat import emojis
from ..chat.scope import Roster, Scope
from ..config import cdn_base
from ..contracts import (
    DescribeEmojisRequest,
    DescribeEmojisResult,
    Image,
    ImageJobRequest,
    ImageJobResult,
    JobRequest,
    JobResult,
    Usage,
)
from ..executor import TransientError
from ..media import drawing, generation
from ..media.vision import ImageRefused, Vision, billed_cost, look
from ..openrouter.chat import WebOpenRouterModel
from ..openrouter.client import openrouter_client
from ..openrouter.embeddings import Embedder
from ..openrouter.images import OpenRouterImageModel
from ..prompt import history
from .deps import Deps, MemorySession
from .tools import reply_agent
from .tools.messages import finish, index_guild
from .web import MAX_WEB_STEPS, WEB_TOOLS, web_use

logger = logging.getLogger(__name__)
os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")

MAX_MODEL_REQUESTS = 10  # room for memory tools: a failed write, a split and a retry
EMBEDDING_TIMEOUT_SECONDS = 4  # two calls (index, query) fit in the tool deadline
# Image models are slow, more so with references; with the reference check this
# stays inside the host's job timeout (AGENT_HOST_JOB_TIMEOUT_SECS, 120 s).
IMAGE_TIMEOUT_SECONDS = 75

topic_agent = Agent()


class PydanticExecutor:
    web_tools = WEB_TOOLS
    # Discord media and API requests (tests replace it).
    transport: httpx.AsyncBaseTransport | None = None

    def __init__(
        self, database_url: str, api_key: str, vision_model: str = "", discord_token: str = ""
    ):
        self.database_url = database_url
        # Without one, images go to the chat model, which may not read them.
        self.vision_model = vision_model
        # Without it, images in messages older than a day can't be downloaded.
        self.discord_token = discord_token
        if os.getenv("OPENROUTER_BASE_URL"):
            self.provider = OpenRouterProvider(openai_client=openrouter_client(api_key))
        else:
            self.provider = OpenRouterProvider(api_key=api_key)
        # Fail fast inside a tool's deadline: a slow embedding degrades to "partial"
        # indexing and lexical search instead of losing the whole call.
        self.embeddings = openrouter_client(
            api_key, timeout=EMBEDDING_TIMEOUT_SECONDS, max_retries=0
        )
        # No retries: each attempt may be billed, and the tool has its own deadline.
        self.images = openrouter_client(api_key, timeout=IMAGE_TIMEOUT_SECONDS, max_retries=0)

    def model(self, name: str) -> Model:
        return WebOpenRouterModel(name, provider=self.provider)

    def image_model(self, name: str) -> ImageGenerationModel:
        return OpenRouterImageModel(name, self.images)

    async def execute(self, request: JobRequest) -> JobResult:
        started = time.monotonic()
        if request.messages[-1].role != "user":
            raise ValueError("the last message must be from the user")
        prompt, earlier = request.messages[-1].content, history(request.messages[:-1])
        options = {
            "model": self.model(request.model),
            "message_history": earlier,
            "model_settings": {
                "max_tokens": request.max_output_tokens,
                "openrouter_usage": {"include": True},
            },
            "usage_limits": UsageLimits(request_limit=MAX_MODEL_REQUESTS),
        }
        if request.kind == "reply":
            options["model_settings"]["extra_body"] = {"max_tool_calls": MAX_WEB_STEPS}
        embed = Embedder(self.embeddings)
        vision = self.vision(request.model)
        deps = None
        try:
            if request.kind == "topic":
                result = await topic_agent.run(prompt, **options)
            else:
                async with await db.connect(self.database_url) as connection:
                    scope = Scope(
                        int(request.scope.guild_id),
                        int(request.scope.channel_id),
                        int(request.scope.conversation_id),
                    )
                    deps = Deps(
                        connection,
                        scope,
                        embed,
                        Roster.of(request.people),
                        vision,
                        job_id=request.id,
                        image_model=request.image_model,
                        memory=MemorySession(self.database_url),
                    )
                    # Embedding is cheap: start while the model reads the prompt,
                    # rather than when it first searches.
                    deps.indexing = asyncio.create_task(index_guild(deps))
                    try:
                        result = await reply_agent.run(
                            prompt, deps=deps, capabilities=self.web_tools, **options
                        )
                    finally:
                        await finish(deps.indexing)
                        await deps.memory.close()
        except ModelHTTPError as e:
            if e.status_code == 429 or e.status_code >= 500:
                raise TransientError(f"provider returned {e.status_code}") from e
            raise
        except (ModelAPIError, OperationalError) as e:
            raise TransientError(type(e).__name__) from e

        usage = result.usage
        responses = [m for m in result.new_messages() if isinstance(m, ModelResponse)]
        web = web_use(responses)
        cost = billed_cost(result)  # includes OpenRouter's web tool fees
        extra = embed.cost + vision.cost
        if extra:
            cost = (cost or 0) + extra
        logger.info(
            "execution completed job_id=%s kind=%s channel_id=%s conversation_id=%s model=%s "
            "requests=%d tool_calls=%d vision_calls=%d web_searches=%d web_fetches=%d "
            "web_sources=%d drawings=%d prompt_tokens=%d completion_tokens=%d cost_usd=%s "
            "latency_ms=%d",
            request.id,
            request.kind,
            request.scope.channel_id,
            request.scope.conversation_id,
            request.model,
            usage.requests,
            usage.tool_calls,
            vision.calls,
            web.searches,
            web.fetches,
            len(web.sources),
            len(deps.drawings) if deps else 0,
            usage.input_tokens,
            usage.output_tokens,
            cost,
            (time.monotonic() - started) * 1000,
        )
        return JobResult(
            text=result.output,
            model=request.model,
            steps=usage.requests,
            usage=Usage(
                prompt_tokens=usage.input_tokens, completion_tokens=usage.output_tokens, cost=cost
            ),
            web=web if web.searches or web.fetches or web.sources else None,
            drawings=deps.drawings if deps else [],
        )

    def vision(self, chat_model: str) -> Vision:
        name = self.vision_model or chat_model
        return Vision(
            self.model(name), name, not self.vision_model, discord_token=self.discord_token
        )

    async def draw(self, request: ImageJobRequest) -> ImageJobResult:
        """A drawing a reply queued: references, the final prompt, the image, and
        Kiki's note if it fails (docs/image-generation.md)."""
        started = time.monotonic()
        plan = request.drawing
        vision = self.vision(request.chat_model)
        scope = Scope(
            int(request.scope.guild_id),
            int(request.scope.channel_id),
            int(request.scope.conversation_id),
        )
        error = reason = None
        image = None
        image_cost = note_cost = 0.0
        note = prompt = ""
        references = []
        try:
            async with await db.connect(self.database_url) as connection:
                gather = drawing.Gatherer(connection, scope, vision, cdn_base(), self.transport)
                found = await asyncio.gather(*(gather.find(s) for s in plan.subjects))
                prompt, references, pictures = drawing.compose(plan, found)
                try:
                    async with asyncio.timeout(IMAGE_TIMEOUT_SECONDS):
                        data, mime_type, image_cost = await generation.generate(
                            self.image_model(request.image_model),
                            prompt,
                            plan.aspect_ratio,
                            pictures,
                        )
                except ImageRefused as e:
                    error, reason = "refused", str(e)
                except ValueError as e:
                    error, reason = "failed", str(e)
                except APIStatusError as e:
                    # Content-policy blocks are 4xx: don't retry them.
                    kind = "refused" if 400 <= e.status_code < 500 else "failed"
                    error, reason = kind, f"the image model returned HTTP {e.status_code}"
                except (APIError, TimeoutError) as e:
                    error, reason = "failed", f"the image model failed ({type(e).__name__})"
                latency_ms = int((time.monotonic() - started) * 1000)
                if error is None:
                    image = Image(
                        id=await generation.save(
                            connection,
                            request.id,
                            mime_type,
                            data,
                            prompt=prompt,
                            model=request.image_model,
                            subjects=[s.model_dump(mode="json") for s in plan.subjects],
                            references=[r.model_dump(mode="json") for r in references],
                            cost=image_cost,
                            latency_ms=latency_ms,
                        ),
                        mime_type=mime_type,
                    )
                else:
                    note, note_cost = await drawing.failure_note(
                        connection,
                        self.model(request.chat_model),
                        request.reply_job,
                        request.ack,
                        reason,
                    )
        except OperationalError as e:
            raise TransientError(type(e).__name__) from e
        cost = image_cost + vision.cost + note_cost
        logger.info(
            "drawing completed job_id=%s channel_id=%s model=%s subjects=%d references=%d "
            "vision_calls=%d outcome=%s cost_usd=%s latency_ms=%d",
            request.id,
            request.scope.channel_id,
            request.image_model,
            len(plan.subjects),
            len(references),
            vision.calls,
            error or "drawn",
            cost,
            (time.monotonic() - started) * 1000,
        )
        return ImageJobResult(
            image=image,
            caption=plan.caption,
            prompt=prompt,
            references=references,
            model=request.image_model,
            cost=cost,
            latency_ms=int((time.monotonic() - started) * 1000),
            error=error,
            failure_note=note,
        )

    async def describe_emojis(self, request: DescribeEmojisRequest) -> DescribeEmojisResult:
        """Describes one batch of the guild's undescribed emoji with a vision model."""
        # The same model call as read_image; the model is Rust's OPENROUTER_EMOJI_MODEL.
        vision = Vision(self.model(request.model), request.model)

        async def describe(
            name: str, animated: bool, image: BinaryContent
        ) -> tuple[str, float | None]:
            prompt = emojis.PROMPT.format(
                name=name, animated=" (animated; this is its first frame)" if animated else ""
            )
            spent = vision.cost
            text = await look(vision, [prompt, image], max_tokens=emojis.DESCRIBE_MAX_TOKENS)
            return text, vision.cost - spent

        embed = Embedder(self.embeddings)
        try:
            async with await db.connect(self.database_url) as connection:
                counts = await emojis.describe_pending(
                    connection,
                    describe,
                    embed,
                    guild_id=int(request.guild_id),
                    model=request.model,
                    attempted_before=emojis.epoch_ms(request.attempted_before_ms),
                    redescribe=request.redescribe,
                )
        except OperationalError as e:
            raise TransientError(type(e).__name__) from e
        return DescribeEmojisResult(**counts)
