"""Image generation: OpenRouter's Images API as a Pydantic AI image model, and storage
for Rust to attach the result to the reply."""

import base64
import binascii
from collections.abc import Sequence

import httpx
from openai import AsyncOpenAI
from psycopg import AsyncConnection
from pydantic_ai import ImageGenerator
from pydantic_ai.exceptions import ContentFilterError, UnexpectedModelBehavior, UserError
from pydantic_ai.images import (
    GeneratedImage,
    ImageGenerationInput,
    ImageGenerationModel,
    ImageGenerationResult,
    ImageGenerationSettings,
)
from pydantic_ai.messages import BinaryImage

from .vision import ImageRefused, sniff

# Discord's smallest upload limit is 10 MiB; leave room for the rest of the request.
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_PROMPT_CHARS = 4000
# GPT Image bills by output tokens: "medium" keeps a 1024² image at a few cents
# (OpenAI's "auto" may pick "high", about 4× the cost).
OPENAI_QUALITY = "medium"


class OpenRouterImageModel(ImageGenerationModel):
    """OpenRouter's `POST /api/v1/images` (GPT Image, Gemini, FLUX, …), with the chat key.

    Pydantic AI's own OpenAI image model needs OpenAI's API and key; this one keeps
    every model call on OpenRouter. Its billed cost is in `provider_details["cost"]`.
    """

    def __init__(
        self,
        model_name: str,
        client: AsyncOpenAI,
        *,
        settings: ImageGenerationSettings | None = None,
    ):
        super().__init__(settings=settings)
        self._model_name = model_name
        self.client = client

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def system(self) -> str:
        return "openrouter"

    @property
    def base_url(self) -> str:
        return str(self.client.base_url)

    async def generate(
        self,
        prompt: str,
        *,
        images: Sequence[ImageGenerationInput] | None = None,
        settings: ImageGenerationSettings | None = None,
    ) -> ImageGenerationResult:
        prompt, references, settings = self.prepare_generate(
            prompt, images=images, settings=settings
        )
        if references:
            raise UserError("OpenRouterImageModel doesn't edit reference images")
        body: dict = {"model": self.model_name, "prompt": prompt, "n": 1}
        if aspect_ratio := settings.get("aspect_ratio"):
            body["aspect_ratio"] = aspect_ratio
        if self.model_name.startswith("openai/"):
            body["quality"] = OPENAI_QUALITY
        response = await self.client.post("/images", body=body, cast_to=httpx.Response)
        payload = response.json()
        generated = []
        for item in payload.get("data") or []:
            try:
                data = base64.b64decode(item.get("b64_json") or "", validate=True)
            except binascii.Error:
                continue
            if data:
                media_type = item.get("media_type") or "image/png"
                generated.append(GeneratedImage(BinaryImage(data, media_type=media_type)))
        cost = float((payload.get("usage") or {}).get("cost") or 0)
        if not generated:
            raise UnexpectedModelBehavior("OpenRouter returned no image", body=None)
        return ImageGenerationResult(
            generated,
            prompt=prompt,
            model_name=self.model_name,
            provider_name=self.system,
            provider_details={"cost": cost},
        )


async def generate(
    model: ImageGenerationModel, prompt: str, aspect_ratio: str | None = None
) -> tuple[bytes, str, float]:
    """The image's bytes, MIME type and billed cost, or ImageRefused/ValueError."""
    prompt = prompt.strip()
    if not prompt:
        raise ValueError("prompt must describe the image")
    if len(prompt) > MAX_PROMPT_CHARS:
        raise ValueError(f"prompt must be at most {MAX_PROMPT_CHARS} characters")
    settings: ImageGenerationSettings = {"aspect_ratio": aspect_ratio} if aspect_ratio else {}
    try:
        result = await ImageGenerator(model).generate(prompt, settings=settings)
    except ContentFilterError:
        raise ImageRefused("The image model refused that prompt.") from None
    except UnexpectedModelBehavior:
        raise ImageRefused("No image was generated.") from None
    image = result.image
    cost = float((result.provider_details or {}).get("cost") or 0)
    # The bytes decide the type (`vision.sniff`), as for images Kiki reads: a
    # provider's declared type can be wrong, and Discord shows these inline.
    if len(image.data) > MAX_IMAGE_BYTES:
        raise ImageRefused("The generated image was too large to send.")
    media_type = sniff(image.data[:12])
    if media_type is None:
        raise ImageRefused(f"The image model returned an unsupported {image.media_type} file.")
    return image.data, media_type, cost


async def save(connection: AsyncConnection, job_id: str, mime_type: str, data: bytes) -> int:
    cursor = await connection.execute(
        "INSERT INTO agent_host_images (job_id, mime_type, data) VALUES (%s, %s, %s) RETURNING id",
        (job_id, mime_type, data),
    )
    row = await cursor.fetchone()
    return row[0]
