"""The reply agent's tools, one toolset per capability. A new capability is a
module with a `toolset`, added to `TOOLSETS`; the model decides when to use it."""

import logging
from pathlib import Path

from pydantic_ai import Agent, ModelRetry, RunContext
from pydantic_ai.workspaces import LocalWorkspaceBackend
from pydantic_ai_harness import Skills

from ...chat.emojis import check
from ..deps import Deps
from . import drawing, emojis, links, memory, messages, reading

logger = logging.getLogger(__name__)

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


@reply_agent.output_validator
def check_emojis(ctx: RunContext[Deps], text: str) -> str:
    """Kiki's emoji must be the server's own, written the way Discord renders them.
    A known name is fixed in place: a missing or wrong ID, a missing `a` for an
    animated one, or a `:name:` shortcode. An unusable emoji gets a retry so the
    model can choose again. On the last attempt it's cut instead, because a
    reply with one bad emoji beats no reply."""
    if ctx.partial_output:
        return text
    checked = check(text, ctx.deps.emojis)
    if not checked.problems:
        if checked.fixed:
            logger.info("reply emoji fixed job_id=%s fixed=%d", ctx.deps.job_id, checked.fixed)
        return checked.text
    if not ctx.last_attempt:
        logger.info(
            "reply emoji rejected job_id=%s problems=%d fixed=%d attempt=%d",
            ctx.deps.job_id,
            len(checked.problems),
            checked.fixed,
            ctx.retry,
        )
        raise ModelRetry(
            "Your reply uses emoji Discord can't show, so they would be posted as text "
            "(or, as reactions, silently left out):\n- "
            + "\n- ".join(checked.problems)
            + "\nOnly use custom emoji your emoji tools returned or the chat shows, "
            "written exactly like <:name:id>; never write one from memory. Call "
            "search_emojis to find one by meaning, use a Unicode emoji character, or "
            "leave it out. Then write your whole reply again."
        )
    logger.warning(
        "reply emoji cut job_id=%s problems=%d fixed=%d",
        ctx.deps.job_id,
        len(checked.problems),
        checked.fixed,
    )
    return checked.stripped or checked.text
