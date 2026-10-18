"""OpenRouter's Images API as a Pydantic AI image model."""

import base64
import binascii
import re
from collections.abc import Sequence

import httpx
from openai import AsyncOpenAI, BadRequestError
from pydantic_ai.exceptions import ContentFilterError, UnexpectedModelBehavior, UserError
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
        try:
            response = await self.client.post("/images", body=body, cast_to=httpx.Response)
        except BadRequestError as e:
            body = e.body if isinstance(e.body, dict) else {}
            if (violations := safety_violations(str(body.get("message") or e.message))) is not None:
                raise ContentFilterError(violations, body=None) from None
            raise
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


# Per model family, from OpenRouter's image catalog (`/api/v1/images/models`):
# how many `input_references` it takes and which aspect ratios.
LIMITS: dict[str, tuple[int, frozenset[str]]] = {
    "openai/": (16, frozenset({"1:1", "3:2", "2:3", "4:3", "3:4", "16:9", "9:16", "21:9"})),
    "qwen/": (
        4,
        frozenset(
            {"1:1", "1:2", "1:4", "2:1", "2:3", "3:2", "3:4", "4:1", "4:3", "4:5", "5:4"}
            | {"9:16", "16:9"}
        ),
    ),
}
# A ratio a model doesn't take, as the nearest one most do.
NEAREST_RATIO = {"21:9": "16:9"}


def reference_limit(model_name: str) -> int:
    """How many reference images `model_name` takes."""
    return next((n for p, (n, _) in LIMITS.items() if model_name.startswith(p)), MAX_REFERENCES)


def aspect_ratio_for(model_name: str, ratio: str | None) -> str | None:
    """`ratio`, or the nearest one `model_name` takes (None: the model's default)."""
    ratios = next((r for p, (_, r) in LIMITS.items() if model_name.startswith(p)), None)
    if ratio is None or ratios is None or ratio in ratios:
        return ratio
    nearest = NEAREST_RATIO.get(ratio)
    return nearest if nearest in ratios else None


def safety_violations(message: str) -> str | None:
    """The categories a provider's moderation blocked a request for, "" when it names
    none, or None when the 400 is something else. OpenRouter passes the provider's
    400 through: OpenAI's "...rejected by the safety system...
    safety_violations=[sexual]", Alibaba's "Alibaba blocked this request through
    content moderation."."""
    if not any(s in message for s in ("safety system", "safety_violations", "content moderation")):
        return None
    found = re.search(r"safety_violations=\[([^\]…'\"]*)", message)
    return found[1].strip() if found else ""


def reference_url(image: ImageGenerationInput) -> str:
    """A reference as OpenRouter takes it: bytes as a data URL (Discord's links expire)."""
    if isinstance(image, BinaryImage):
        return image.data_uri
    if isinstance(image, ImageUrl):
        return image.url
    raise UserError("OpenRouter reference images must be bytes or URLs")
