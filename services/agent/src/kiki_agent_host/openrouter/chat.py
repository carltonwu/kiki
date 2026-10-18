"""OpenRouter chat models, with its web fetch server tool mapped."""

from typing import Any

from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.models.openrouter import OpenRouterModel
from pydantic_ai.native_tools import AbstractNativeTool, WebFetchTool


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
