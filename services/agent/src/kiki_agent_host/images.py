"""Image generation through OpenRouter, saved for Rust to attach to the reply."""

import base64
import binascii
import re

from openai import AsyncOpenAI
from psycopg import AsyncConnection

# Discord's smallest upload limit is 10 MiB; leave room for the rest of the request.
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_PROMPT_CHARS = 4000
MIME_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}
DATA_URL = re.compile(r"data:(image/[a-z+.-]+);base64,(.+)", re.DOTALL)


class ImageRefused(ValueError):
    """The model answered without a usable image; the message is safe to show the model."""


async def generate(client: AsyncOpenAI, model: str, prompt: str) -> tuple[bytes, str, float]:
    """Returns the first image's bytes, MIME type and OpenRouter's billed cost."""
    prompt = prompt.strip()
    if not prompt:
        raise ValueError("prompt must describe the image")
    if len(prompt) > MAX_PROMPT_CHARS:
        raise ValueError(f"prompt must be at most {MAX_PROMPT_CHARS} characters")
    response = await client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        extra_body={"modalities": ["image", "text"], "usage": {"include": True}},
    )
    cost = float(getattr(response.usage, "cost", None) or 0)
    message = response.choices[0].message if response.choices else None
    images = (getattr(message, "model_extra", None) or {}).get("images") or []
    for image in images:
        url = ((image or {}).get("image_url") or {}).get("url", "")
        match = DATA_URL.fullmatch(url.strip())
        if not match or match[1] not in MIME_TYPES:
            continue
        try:
            data = base64.b64decode("".join(match[2].split()), validate=True)
        except binascii.Error:
            continue
        if len(data) > MAX_IMAGE_BYTES:
            raise ImageRefused("The generated image was too large to send.")
        return data, match[1], cost
    note = (getattr(message, "content", None) or "").strip()[:300]
    raise ImageRefused(
        "No image was generated." + (f" The image model said: {note}" if note else "")
    )


async def save(connection: AsyncConnection, job_id: str, mime_type: str, data: bytes) -> int:
    cursor = await connection.execute(
        "INSERT INTO agent_host_images (job_id, mime_type, data) VALUES (%s, %s, %s) RETURNING id",
        (job_id, mime_type, data),
    )
    row = await cursor.fetchone()
    return row[0]
