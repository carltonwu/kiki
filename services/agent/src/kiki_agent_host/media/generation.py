"""Image generation for a drawing job, and storage for Rust to post the result."""

from collections.abc import Sequence

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb
from pydantic_ai import ImageGenerator
from pydantic_ai.exceptions import ContentFilterError, UnexpectedModelBehavior
from pydantic_ai.images import ImageGenerationModel, ImageGenerationSettings
from pydantic_ai.messages import BinaryImage

from .. import telemetry
from ..openrouter.images import NoImage
from .fetch import sniff
from .vision import ImageRefused

# Discord's smallest upload limit is 10 MiB; leave room for the rest of the request.
MAX_IMAGE_BYTES = 8 * 1024 * 1024
# Kiki's prompt plus the subjects and reference legend the image job adds.
MAX_PROMPT_CHARS = 8000


class SafetyRefused(ImageRefused):
    """The image model's safety filter blocked the picture: another model may draw it."""


async def generate(
    model: ImageGenerationModel,
    prompt: str,
    aspect_ratio: str | None = None,
    references: Sequence[BinaryImage] = (),
    attempt: telemetry.Call | None = None,
) -> tuple[bytes, str, float]:
    """The image's bytes, MIME type and billed cost, or ImageRefused/ValueError.
    `references` go to the model in order, as the prompt numbers them. The cost is
    billed to `attempt` as soon as the provider reports it: an image refused after
    that (too large, an unsupported type) was still paid for."""
    prompt = prompt.strip()
    if not prompt:
        raise ValueError("prompt must describe the image")
    if len(prompt) > MAX_PROMPT_CHARS:
        raise ValueError(f"prompt must be at most {MAX_PROMPT_CHARS} characters")
    settings: ImageGenerationSettings = {"aspect_ratio": aspect_ratio} if aspect_ratio else {}
    try:
        result = await ImageGenerator(model).generate(
            prompt, images=list(references), settings=settings
        )
    except ContentFilterError as e:
        # Seen in prod: told "the image model choked, ask again", people did, and the
        # same picture was blocked again. Only a tamer picture gets through.
        about = f" ({e.message})" if e.message.strip() else ""
        raise SafetyRefused(
            f"The image model's safety filter blocked this picture{about}. Asking again "
            "won't get it through; only a tamer version of the idea could be drawn."
        ) from None
    except UnexpectedModelBehavior as e:
        if isinstance(e, NoImage) and attempt is not None:
            attempt.bill(e.cost)  # paid for all the same
        raise ImageRefused("No image was generated.") from None
    image = result.image
    cost = float((result.provider_details or {}).get("cost") or 0)
    if attempt is not None:
        attempt.bill(cost)
    # The bytes decide the type (`fetch.sniff`), as for images Kiki reads: a
    # provider's declared type can be wrong, and Discord shows these inline.
    if len(image.data) > MAX_IMAGE_BYTES:
        raise ImageRefused("The generated image was too large to send.")
    media_type = sniff(image.data[:12])
    if media_type is None:
        raise ImageRefused(f"The image model returned an unsupported {image.media_type} file.")
    return image.data, media_type, cost


async def save(
    connection: AsyncConnection,
    job_id: str,
    mime_type: str,
    data: bytes,
    *,
    prompt: str | None = None,
    model: str | None = None,
    subjects: list | None = None,
    references: list | None = None,
    cost: float | None = None,
    latency_ms: int | None = None,
) -> int:
    """Saves a drawing with what it was made from (`subjects` and `references` are
    JSON-ready lists), so a later look at it can say why it came out as it did."""
    cursor = await connection.execute(
        "INSERT INTO agent_host_images (job_id, mime_type, data, prompt, model, subjects,"
        " reference_images, cost_usd, latency_ms)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (
            job_id,
            mime_type,
            data,
            prompt,
            model,
            Jsonb(subjects or []),
            Jsonb(references or []),
            cost,
            latency_ms,
        ),
    )
    row = await cursor.fetchone()
    return row[0]
