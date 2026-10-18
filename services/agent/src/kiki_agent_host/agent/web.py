"""OpenRouter's web search and fetch server tools: what replies are offered, and
what a run used."""

from pydantic_ai.capabilities import NativeTool
from pydantic_ai.messages import ModelResponse
from pydantic_ai.native_tools import WebFetchTool, WebSearchTool

from ..contracts import WebUse

# OpenRouter runs web search and fetch itself (server tools). This caps its steps per
# model request (its default is 30); Kiki's own tool budget doesn't see them.
MAX_WEB_STEPS = 3
WEB_FETCH_MAX_TOKENS = 6000  # truncates long pages, which every later request re-reads
WEB_SOURCES = 10  # citation URLs kept in the result


# Offered to every reply job; the model decides when the web helps.
WEB_TOOLS = (
    NativeTool(WebSearchTool()),
    NativeTool(WebFetchTool(max_content_tokens=WEB_FETCH_MAX_TOKENS)),
)


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
