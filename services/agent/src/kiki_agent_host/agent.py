"""Pydantic AI executor: the SDK owns the loop, these tools search the guild and
the web and draw images."""

import asyncio
import contextlib
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from openai import APIError, APIStatusError
from psycopg import AsyncConnection, OperationalError
from pydantic_ai import Agent, RunContext, Tool
from pydantic_ai.capabilities import NativeTool, PrepareTools
from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError
from pydantic_ai.images import ImageGenerationModel
from pydantic_ai.messages import (
    BinaryContent,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    TextPart,
    UserPromptPart,
)
from pydantic_ai.models import Model, ModelRequestParameters
from pydantic_ai.models.openrouter import OpenRouterModel
from pydantic_ai.native_tools import AbstractNativeTool, WebFetchTool, WebSearchTool
from pydantic_ai.providers.openrouter import OpenRouterProvider
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.usage import UsageLimits

from . import emojis, images, memory_tools, reading, retrieval
from .contracts import (
    DescribeEmojisRequest,
    DescribeEmojisResult,
    Image,
    JobRequest,
    JobResult,
    Message,
    Usage,
    WebUse,
)
from .executor import TransientError
from .vision import Vision, billed_cost, look

logger = logging.getLogger(__name__)
os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")

MAX_TOOL_CALLS = 4
MAX_MODEL_REQUESTS = 10  # room for memory tools: a failed write, a split and a retry
TOOL_TIMEOUT_SECONDS = 10
VISION_TIMEOUT_SECONDS = 45  # downloads plus a vision model call
EMBEDDING_TIMEOUT_SECONDS = 4  # two calls (index, query) fit in the tool deadline
# OpenRouter runs web search and fetch itself (server tools). This caps its steps per
# model request (its default is 30); Kiki's own tool budget doesn't see them.
MAX_WEB_STEPS = 3
WEB_FETCH_MAX_TOKENS = 6000  # truncates long pages, which every later request re-reads
WEB_SOURCES = 10  # citation URLs kept in the result
MAX_IMAGES = 2
IMAGE_TIMEOUT_SECONDS = 60  # image models are slow; still well inside the job timeout


@dataclass
class Deps:
    """Trusted per-run context: tool arguments can only narrow it."""

    connection: AsyncConnection
    scope: retrieval.Scope
    embed: retrieval.Embedder
    roster: retrieval.Roster
    vision: Vision
    tool_calls: int = 0
    indexing: asyncio.Task[retrieval.Semantic] | None = None
    job_id: str = ""
    image_model: ImageGenerationModel | None = None
    images: list[Image] = field(default_factory=list)
    image_cost: float = 0.0
    image_calls: int = 0
    memory: memory_tools.Session | None = None


async def guarded(
    ctx: RunContext[Deps],
    name: str,
    work,
    timeout: float = TOOL_TIMEOUT_SECONDS,
    budgeted: bool = True,
) -> dict:
    """Runs a tool with a deadline. Lookups share MAX_TOOL_CALLS; `budgeted=False`
    tools (generate_image) have their own cap, so searching can't block a drawing."""
    if budgeted:
        ctx.deps.tool_calls += 1
        if ctx.deps.tool_calls > MAX_TOOL_CALLS:
            logger.debug("tool budget exhausted job_id=%s tool=%s", ctx.deps.job_id, name)
            return {"error": "Tool budget used up. Answer with what you have."}
    started = time.monotonic()
    outcome = "ok"
    try:
        async with asyncio.timeout(timeout):
            result = await work()
    except TimeoutError:
        outcome = "timeout"
        result = {"error": "Timed out."}
    except images.ImageRefused as e:
        outcome = "refused"
        result = {"error": str(e)}
    except ValueError as e:
        outcome = "invalid_arguments"
        result = {"error": str(e)}
    logger.info(
        "tool completed job_id=%s tool=%s channel_id=%s conversation_id=%s "
        "latency_ms=%d hits=%d semantic=%s outcome=%s",
        ctx.deps.job_id,
        name,
        ctx.deps.scope.channel_id,
        ctx.deps.scope.conversation_id,
        (time.monotonic() - started) * 1000,
        len(result.get("messages", result.get("images", result.get("emojis", [])))),
        result.get("semantic", "-"),
        "error" if outcome == "ok" and "error" in result else outcome,
    )
    return result


async def search_messages(
    ctx: RunContext[Deps],
    query: str,
    person: str | None = None,
    channel: str | None = None,
    before: datetime | None = None,
    after: datetime | None = None,
    limit: int = 10,
) -> dict:
    """Search the message history of every channel in this server (by meaning and by keywords).

    Recent matches rank slightly higher unless `before`/`after` is given. Older
    messages already shown to you in this conversation are not returned.

    Args:
        query: What to look for, in natural language or keywords.
        person: Only messages written by this person: a ref like u2 (best), an
            @username, or a name they've gone by. Names match exactly, so "Chris"
            won't find "Chrissy"; if several people share the name, results say so.
        channel: Only messages in this channel, like #general. Leave it out to
            search the whole server.
        before: Only messages sent before this time (ISO 8601, UTC).
        after: Only messages sent at or after this time (ISO 8601, UTC).
        limit: Maximum results, up to 10.
    """

    async def work():
        deps = ctx.deps
        if deps.indexing is None:
            deps.indexing = asyncio.create_task(index_guild(deps))
        # Shielded: a search timing out must not cancel indexing for the next one.
        semantic = await asyncio.shield(deps.indexing)
        return await retrieval.search(
            deps.connection,
            deps.embed,
            deps.scope,
            query,
            person=person,
            channel=channel,
            roster=deps.roster,
            before=before,
            after=after,
            limit=limit,
            semantic=semantic,
        )

    return await guarded(ctx, "search_messages", work)


async def get_message_context(
    ctx: RunContext[Deps], message_id: str, before: int = 5, after: int = 5
) -> dict:
    """Fetch the messages around one message in its channel, plus what it replied to.

    Args:
        message_id: The `id` of a message from search results.
        before: Messages before it, up to 10.
        after: Messages after it, up to 10.
    """

    async def work():
        if not message_id.isdigit():
            raise ValueError("message_id must be a message ID from search results")
        return await retrieval.message_context(
            ctx.deps.connection, ctx.deps.scope, int(message_id), before, after, ctx.deps.roster
        )

    return await guarded(ctx, "get_message_context", work)


async def read_image(ctx: RunContext[Deps], message_id: str, question: str | None = None) -> dict:
    """Look at the images in one message: a single image or a whole gallery.

    Another model looks at them and describes each image in detail (with any
    text in it), and a gallery as a whole.

    Args:
        message_id: The message ID shown with its images, like "[image: cat.png;
            message 123]", or a message `id` from search results.
        question: Optional: what you want to know about them, so the
            description covers it.
    """

    async def work():
        if not message_id.strip().isdigit():
            raise ValueError("message_id must be the ID of a message with images")
        deps = ctx.deps
        return await reading.read_image(
            deps.connection, deps.scope, deps.roster, deps.vision, int(message_id), question
        )

    return await guarded(ctx, "read_image", work, VISION_TIMEOUT_SECONDS)


async def generate_image(
    ctx: RunContext[Deps],
    prompt: str,
    aspect_ratio: Literal["1:1", "3:2", "2:3"] | None = None,
) -> dict:
    """Draw an image and attach it to your reply.

    Use it when someone asks for a picture, drawing, meme or edit-free visual, or
    when one would clearly land better than words. The image is attached to your
    first message automatically: don't paste links or markdown for it; just write
    your message as if it's posted with the image.

    Args:
        prompt: A complete, self-contained description of the image: subject,
            style, composition and any text it should contain. The image model
            can't see this conversation or people's faces, so describe what
            matters instead of referring to it.
        aspect_ratio: 3:2 for wide scenes, 2:3 for tall ones; square by default.
    """

    async def work():
        deps = ctx.deps
        # Counted before the call: the model may request several images in parallel,
        # and a failed attempt may still be billed.
        if deps.image_calls >= MAX_IMAGES:
            return {"error": f"At most {MAX_IMAGES} images per reply. Answer with what you have."}
        deps.image_calls += 1
        try:
            data, mime_type, cost = await images.generate(deps.image_model, prompt, aspect_ratio)
        except APIStatusError as e:
            # Content-policy refusals are 4xx; tell the model rather than failing the reply.
            logger.warning(
                "image generation failed job_id=%s status=%d", deps.job_id, e.status_code
            )
            return {"error": f"The image model failed (HTTP {e.status_code}). Don't retry."}
        except APIError as e:
            logger.warning(
                "image generation failed job_id=%s error_type=%s", deps.job_id, type(e).__name__
            )
            return {"error": "The image model is unreachable. Don't retry."}
        deps.image_cost += cost
        image = Image(
            id=await images.save(deps.connection, deps.job_id, mime_type, data),
            mime_type=mime_type,
        )
        deps.images.append(image)
        logger.info(
            "image generated job_id=%s image_id=%d mime_type=%s bytes=%d cost_usd=%s",
            deps.job_id,
            image.id,
            mime_type,
            len(data),
            cost,
        )
        return {"attached": True, "images_attached": len(deps.images)}

    return await guarded(ctx, "generate_image", work, IMAGE_TIMEOUT_SECONDS, budgeted=False)


async def only_with_image_model(
    ctx: RunContext[Deps], tool_def: ToolDefinition
) -> ToolDefinition | None:
    """Offers `generate_image` only when the server has image generation on."""
    return tool_def if ctx.deps.image_model else None


async def answer_on_last_request(
    ctx: RunContext[Deps], tool_defs: list[ToolDefinition]
) -> list[ToolDefinition]:
    """No function tools on the last request the limit allows, so the model answers
    with what it has instead of the run failing with UsageLimitExceeded."""
    return [] if ctx.run_step >= MAX_MODEL_REQUESTS else tool_defs


async def search_emojis(ctx: RunContext[Deps], query: str, limit: int = 5) -> dict:
    """Find this server's custom emoji by mood, reaction or what they show.

    To use one, write its `emoji` exactly as returned, like <:name:id>.

    Args:
        query: The feeling, reaction or subject, like "confused" or "laughing so hard".
        limit: Maximum results, up to 10.
    """

    async def work():
        return await emojis.search(
            ctx.deps.connection, ctx.deps.embed, ctx.deps.scope.guild_id, query, limit
        )

    return await guarded(ctx, "search_emojis", work)


async def find_emoji(ctx: RunContext[Deps], name: str) -> dict:
    """Look up this server's custom emoji by name, or explain one someone used.

    Args:
        name: An emoji name like huh or :huh:, or an emoji as it appears in a
            message, like <:huh:1085017182301339648>.
    """

    async def work():
        return await emojis.find(ctx.deps.connection, ctx.deps.scope.guild_id, name)

    return await guarded(ctx, "find_emoji", work)


async def index_guild(deps: Deps) -> retrieval.Semantic:
    try:
        found, more = await retrieval.index_pending(
            deps.connection, deps.embed, guild_id=deps.scope.guild_id
        )
    except OperationalError:
        raise
    except Exception as error:  # noqa: BLE001 - preserve search without logging provider payloads
        logger.warning(
            "indexing failed job_id=%s guild_id=%s error_type=%s; using partial index",
            deps.job_id,
            deps.scope.guild_id,
            type(error).__name__,
        )
        return "partial"
    logger.debug(
        "indexed guild job_id=%s guild_id=%s messages=%d more_pending=%s",
        deps.job_id,
        deps.scope.guild_id,
        found,
        more is not None,
    )
    return "partial" if more else "full"


async def finish(task: asyncio.Task) -> None:
    """Cancels indexing the run didn't wait for: the reply shouldn't wait either.
    Saved batches stay saved; the rest is indexed by a later search."""
    if not task.done():
        task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task


class WebOpenRouterModel(OpenRouterModel):
    """OpenRouter with its web fetch server tool, which Pydantic AI 2.57 maps only for
    Anthropic and Google. Web search is mapped by the SDK (`openrouter:web_search`)."""

    @classmethod
    def supported_native_tools(cls) -> frozenset[type[AbstractNativeTool]]:
        return super().supported_native_tools() | {WebFetchTool}

    def _get_tool_choice(self, model_settings, model_request_parameters: ModelRequestParameters):
        tools, tool_choice = super()._get_tool_choice(model_settings, model_request_parameters)
        fetch = next(
            (t for t in model_request_parameters.native_tools if isinstance(t, WebFetchTool)), None
        )
        if fetch is not None:
            parameters: dict[str, Any] = {}
            if fetch.max_uses is not None:
                parameters["max_uses"] = fetch.max_uses
            if fetch.max_content_tokens is not None:
                parameters["max_content_tokens"] = fetch.max_content_tokens
            if fetch.allowed_domains is not None:
                parameters["allowed_domains"] = fetch.allowed_domains
            if fetch.blocked_domains is not None:
                parameters["blocked_domains"] = fetch.blocked_domains
            tools.append({"type": "openrouter:web_fetch", "parameters": parameters})
        return tools, tool_choice


# Offered to every reply job; the model decides when the web helps.
WEB_TOOLS = (
    NativeTool(WebSearchTool()),
    NativeTool(WebFetchTool(max_content_tokens=WEB_FETCH_MAX_TOKENS)),
)
chat = Agent(
    deps_type=Deps,
    tools=[
        search_messages,
        get_message_context,
        read_image,
        search_emojis,
        find_emoji,
        Tool(generate_image, prepare=only_with_image_model),
    ],
    capabilities=[PrepareTools(answer_on_last_request)],
)
memory_tools.register(chat)
plain = Agent()


def web_use(responses: list[ModelResponse]) -> WebUse:
    """Server tool use as OpenRouter reports it: searches, every server tool call (only
    search and fetch are offered, so the rest are fetches), and cited URLs."""
    searches = calls = 0
    sources: list[str] = []
    for response in responses:
        details = response.provider_details or {}
        used = details.get("server_tool_use") or {}
        searches += used.get("web_search_requests") or 0
        calls += used.get("tool_calls_executed") or 0
        for annotation in details.get("annotations") or []:
            url = (annotation.get("url_citation") or {}).get("url")
            if url and url not in sources and len(sources) < WEB_SOURCES:
                sources.append(url)
    return WebUse(searches=searches, fetches=max(calls - searches, 0), sources=sources)


def history(messages: list[Message]) -> list[ModelMessage]:
    def convert(m: Message) -> ModelMessage:
        if m.role == "assistant":
            return ModelResponse(parts=[TextPart(m.content)])
        part = SystemPromptPart(m.content) if m.role == "system" else UserPromptPart(m.content)
        return ModelRequest(parts=[part])

    return [convert(m) for m in messages]


class PydanticExecutor:
    web_tools = WEB_TOOLS

    def __init__(self, database_url: str, api_key: str, vision_model: str = ""):
        self.database_url = database_url
        # Without one, images go to the chat model, which may not read them.
        self.vision_model = vision_model
        if os.getenv("OPENROUTER_BASE_URL"):
            self.provider = OpenRouterProvider(openai_client=retrieval.openrouter_client(api_key))
        else:
            self.provider = OpenRouterProvider(api_key=api_key)
        # Fail fast inside a tool's deadline: a slow embedding degrades to "partial"
        # indexing and lexical search instead of losing the whole call.
        self.embeddings = retrieval.openrouter_client(
            api_key, timeout=EMBEDDING_TIMEOUT_SECONDS, max_retries=0
        )
        # No retries: each attempt may be billed, and the tool has its own deadline.
        self.images = retrieval.openrouter_client(
            api_key, timeout=IMAGE_TIMEOUT_SECONDS, max_retries=0
        )

    def model(self, name: str) -> Model:
        return WebOpenRouterModel(name, provider=self.provider)

    def image_model(self, name: str) -> ImageGenerationModel:
        return images.OpenRouterImageModel(name, self.images)

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
        embed = retrieval.Embedder(self.embeddings)
        vision_name = self.vision_model or request.model
        vision = Vision(self.model(vision_name), vision_name, not self.vision_model)
        deps = None
        try:
            if request.kind == "topic":
                result = await plain.run(prompt, **options)
            else:
                async with await AsyncConnection.connect(
                    self.database_url, autocommit=True, connect_timeout=10
                ) as connection:
                    scope = retrieval.Scope(
                        int(request.scope.guild_id),
                        int(request.scope.channel_id),
                        int(request.scope.conversation_id),
                    )
                    deps = Deps(
                        connection,
                        scope,
                        embed,
                        retrieval.Roster.of(request.people),
                        vision,
                        job_id=request.id,
                        image_model=self.image_model(request.image_model)
                        if request.image_model
                        else None,
                        memory=memory_tools.Session(self.database_url),
                    )
                    # Embedding is cheap: start while the model reads the prompt,
                    # rather than when it first searches.
                    deps.indexing = asyncio.create_task(index_guild(deps))
                    try:
                        result = await chat.run(
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
        extra = embed.cost + vision.cost + (deps.image_cost if deps else 0)
        if extra:
            cost = (cost or 0) + extra
        logger.info(
            "execution completed job_id=%s kind=%s channel_id=%s conversation_id=%s model=%s "
            "requests=%d tool_calls=%d vision_calls=%d web_searches=%d web_fetches=%d "
            "web_sources=%d images=%d prompt_tokens=%d completion_tokens=%d cost_usd=%s "
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
            len(deps.images) if deps else 0,
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
            images=deps.images if deps else [],
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

        embed = retrieval.Embedder(self.embeddings)
        try:
            async with await AsyncConnection.connect(
                self.database_url, autocommit=True, connect_timeout=10
            ) as connection:
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
