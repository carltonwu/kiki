"""The reply agent's tools, one toolset per capability. A new capability is a
module with a `toolset`, added to `TOOLSETS`; the model decides when to use it."""

from pydantic_ai import Agent

from ..deps import Deps
from . import drawing, emojis, memory, messages, reading

TOOLSETS = [messages.toolset, reading.toolset, emojis.toolset, drawing.toolset, memory.toolset]
reply_agent = Agent(deps_type=Deps, toolsets=TOOLSETS)
