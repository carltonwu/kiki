"""The vision model call shared by reading (`media/reading.py`), drawing
(`media/drawing.py`) and custom emoji descriptions (`chat/emojis.py`). It maps
provider failures to `ImageRefused`, the one image error whose message is safe
to show the chat model, and adds billed cost."""

import logging
from dataclasses import dataclass

from pydantic_ai import Agent
from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError, UnexpectedModelBehavior
from pydantic_ai.messages import ModelResponse, UserContent
from pydantic_ai.models import Model
from pydantic_ai.usage import UsageLimits

from .. import telemetry

logger = logging.getLogger(__name__)


viewer = Agent()

# A reasoning model's thinking counts against `max_tokens`. On a two-image
# gallery DeepSeek V4.1 Flash thought for 400 to 1,100 tokens, so a cap sized
# for the description alone ran out mid-thought (no text, so pydantic-ai raised
# UnexpectedModelBehavior) or mid-description (cut off). `look` adds this much
# on top of the answer's room.
REASONING_TOKENS = 2000


@dataclass
class Vision:
    """The model that reads images; `fallback` means it's the chat model."""

    model: Model
    name: str
    fallback: bool = False
    cost: float = 0.0
    calls: int = 0
    # The bot token, to refresh expired attachment links (`refresh`); none skips it.
    discord_token: str = ""
    # What its calls are for in kiki.llm.*: vision, or emoji for descriptions.
    purpose: str = "vision"


class ImageRefused(ValueError):
    """No usable image (read, described or drawn); the message is safe to show the
    chat model. A ValueError, so tools report it rather than failing the reply."""


async def look(
    vision: Vision,
    prompt: list[UserContent],
    *,
    max_tokens: int,
    instructions: str | None = None,
    think: bool = True,
) -> str:
    """One vision model request; its billed cost is added to `vision.cost`.
    `think=False` turns off a reasoning model's thinking, for quick yes/no checks:
    over several photos it can take longer than the answer is worth.
    `max_tokens` is the answer's room; thinking gets `REASONING_TOKENS` more."""
    vision.calls += 1
    budget = max_tokens + REASONING_TOKENS if think else max_tokens
    settings: dict = {"max_tokens": budget, "openrouter_usage": {"include": True}}
    if not think:
        settings["openrouter_reasoning"] = {"enabled": False}
        settings["temperature"] = 0  # the same photos get the same verdicts
    try:
        with telemetry.llm_call(vision.purpose, vision.name) as call:
            run = await viewer.run(
                prompt,
                model=vision.model,
                instructions=instructions,
                model_settings=settings,
                usage_limits=UsageLimits(request_limit=1),
            )
            bill(call, run)
    except ModelHTTPError as error:
        logger.warning(
            "vision call failed model=%s fallback=%s status=%d",
            vision.name,
            vision.fallback,
            error.status_code,
        )
        if error.status_code == 429 or error.status_code >= 500:
            raise ImageRefused(
                "The image model is unavailable right now; try again later."
            ) from None
        if vision.fallback:
            raise ImageRefused(
                "No vision model is configured (OPENROUTER_VISION_MODEL), and the "
                f"chat model {vision.name} can't read images. Tell them you can't see "
                "images right now."
            ) from None
        raise ImageRefused(f"The image model {vision.name} rejected these images.") from None
    except (ModelAPIError, UnexpectedModelBehavior) as error:
        logger.warning(
            "vision call failed model=%s error_type=%s", vision.name, type(error).__name__
        )
        raise ImageRefused("The image model is unavailable right now; try again later.") from None
    vision.cost += billed_cost(run) or 0.0
    if not run.output.strip():
        raise ImageRefused("The image model returned no description.")
    if run.response.finish_reason == "length":
        # Still worth reading, but Kiki shouldn't take it for the whole picture.
        logger.info("vision answer cut off model=%s max_tokens=%d", vision.name, budget)
        return run.output.strip() + "\n(The description was cut off here.)"
    return run.output.strip()


def bill(call: telemetry.Call, run) -> None:
    """Puts a run's billed cost and tokens on its telemetry call."""
    call.bill(billed_cost(run), run.usage.input_tokens, run.usage.output_tokens)


def response_cost(response: ModelResponse) -> float | None:
    """OpenRouter's billed cost for one response; Pydantic's price table lags new
    models."""
    return (response.provider_details or {}).get("cost")


def billed_cost(result) -> float | None:
    """OpenRouter's billed cost per response, summed."""
    billed = [
        cost
        for m in result.new_messages()
        if isinstance(m, ModelResponse) and (cost := response_cost(m)) is not None
    ]
    if billed:
        return sum(billed)
    return float(result.usage.cost) if result.usage.cost is not None else None
