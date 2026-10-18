"""OpenRouter's Images API as a Pydantic AI image model."""

import base64
import binascii
from collections.abc import Sequence

import httpx
from openai import AsyncOpenAI
from pydantic_ai.exceptions import UnexpectedModelBehavior, UserError
from pydantic_ai.images import (
    GeneratedImage,
    ImageGenerationInput,
    ImageGenerationModel,
    ImageGenerationResult,
    ImageGenerationSettings,
)
from pydantic_ai.messages import BinaryImage, ImageUrl

# GPT Image's limit, and OpenRouter's `input_references` range for it.
MAX_REFERENCES = 16
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
        if len(references) > MAX_REFERENCES:
            raise UserError(f"OpenRouter takes at most {MAX_REFERENCES} reference images")
        body: dict = {"model": self.model_name, "prompt": prompt, "n": 1}
        if references:
            # In order: prompts refer to them by position ("reference image 2").
            body["input_references"] = [
                {"type": "image_url", "image_url": {"url": reference_url(r)}} for r in references
            ]
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


def reference_url(image: ImageGenerationInput) -> str:
    """A reference as OpenRouter takes it: bytes as a data URL (Discord's links expire)."""
    if isinstance(image, BinaryImage):
        return image.data_uri
    if isinstance(image, ImageUrl):
        return image.url
    raise UserError("OpenRouter reference images must be bytes or URLs")
