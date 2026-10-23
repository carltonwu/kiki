"""Refuseless chat models (docs.refuseless.com): a server whose chat model is
`refuseless/<id>` runs on Refuseless's OpenAI-compatible API instead of
OpenRouter. It has no server tools (web search and fetch) and reports tokens
but no cost."""

from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from ..config import refuseless_base

PREFIX = "refuseless/"


def is_refuseless(model: str) -> bool:
    return model.startswith(PREFIX)


def refuseless_provider(api_key: str) -> OpenAIProvider:
    return OpenAIProvider(base_url=refuseless_base(), api_key=api_key)


def refuseless_model(name: str, provider: OpenAIProvider) -> OpenAIChatModel:
    """`name` without its `refuseless/` prefix, which Refuseless's IDs don't have."""
    return OpenAIChatModel(name.removeprefix(PREFIX), provider=provider)
