"""The reply agent's tools, one toolset per capability. A new capability is a
module with a `toolset`, added to `TOOLSETS`; the model decides when to use it."""

from pathlib import Path

from pydantic_ai import Agent
from pydantic_ai.workspaces import LocalWorkspaceBackend
from pydantic_ai_harness import Skills

from ..deps import Deps
from . import drawing, emojis, links, memory, messages, reading

TOOLSETS = [
    messages.toolset,
    reading.toolset,
    links.toolset,
    emojis.toolset,
    drawing.toolset,
    memory.toolset,
]
# Skill packages in `kiki_agent_host/skills/`: the model sees each one's name and
# description and loads the body when it wants it. `unslop` is adapted from
# cursor/plugins (pstack/skills/unslop).
SKILLS = Skills("skills", workspace=LocalWorkspaceBackend(Path(__file__).parents[2]))
reply_agent = Agent(deps_type=Deps, toolsets=TOOLSETS, capabilities=[SKILLS])
