"""OpenRouter's OpenAI-compatible API, for embeddings, images and chat."""

from openai import AsyncOpenAI

from ..config import openrouter_base


def openrouter_client(api_key: str, **options) -> AsyncOpenAI:
    return AsyncOpenAI(base_url=f"{openrouter_base()}/v1", api_key=api_key, **options)
