"""The reply agent's tools, one toolset per capability. A new capability is a
module with a `toolset`, added to `TOOLSETS`; the model decides when to use it."""

import logging
from dataclasses import dataclass, replace
from pathlib import Path

from pydantic_ai import Agent, ModelRetry, RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.models import ModelRequestContext
from pydantic_ai.workspaces import LocalWorkspaceBackend
from pydantic_ai_harness import Skills

from ...chat import emojis as emoji_catalog
from ..deps import Deps
from . import drawing, emojis, links, memory, messages, reading

logger = logging.getLogger(__name__)

# Kiki's answer when she sits out; the emoji check's answer when nothing valid is left.
NO_REPLY = "NO_REPLY"

TOOLSETS = [
    messages.toolset,
    reading.toolset,
    links.toolset,
    emojis.toolset,
    drawing.toolset,
    memory.toolset,
]


@dataclass
class LastRequestAnswers(AbstractCapability[Deps]):
    """No function tools on the last request the limit allows, including ones
    capabilities add (Skills' `load_capability`). A tool call there would need one
    more request, and the limit would then fail the whole reply. Without tools,
    the model has to answer."""

    async def before_model_request(
        self, ctx: RunContext[Deps], request_context: ModelRequestContext
    ) -> ModelRequestContext:
        if ctx.usage.requests + 1 < ctx.deps.request_limit:
            return request_context
        parameters = replace(request_context.model_request_parameters, function_tools=[])
        return replace(request_context, model_request_parameters=parameters)


# Skill packages in `kiki_agent_host/skills/`: the model sees each one's name and
# description and loads the body when it wants it. `unslop` is adapted from
# cursor/plugins (pstack/skills/unslop).
SKILLS = Skills("skills", workspace=LocalWorkspaceBackend(Path(__file__).parents[2]))
reply_agent = Agent(
    deps_type=Deps,
    toolsets=TOOLSETS,
    capabilities=[SKILLS, LastRequestAnswers()],
)


@reply_agent.output_validator
async def check_emojis(ctx: RunContext[Deps], text: str) -> str:
    """Kiki's emoji must be the server's own, written the way Discord renders them.

    A known name is fixed in place: a missing or wrong ID, a missing `a` for an
    animated emoji, or a `:name:` shortcode. Before an emoji is called invalid,
    the catalog is reloaded once, since the Gateway may have just synced one.
    An unusable emoji gets a retry while the run has an output retry and a model
    request left. Otherwise it's cut, because a reply with one bad emoji beats no
    reply. A retry costs at least one more request: more if the model searches
    first, and `LastRequestAnswers` keeps that within the limit.
    """
    if ctx.partial_output:
        return text
    deps = ctx.deps
    checked = emoji_catalog.check(text, deps.emojis)
    if checked.problems and not deps.emojis_reloaded:
        deps.emojis_reloaded = True
        deps.emojis = await emoji_catalog.catalog(deps.connection, deps.scope.guild_id)
        checked = emoji_catalog.check(text, deps.emojis)
    if not checked.problems:
        if checked.fixed:
            logger.info("reply emoji fixed job_id=%s fixed=%d", deps.job_id, checked.fixed)
        return checked.text
    requests_left = deps.request_limit - ctx.usage.requests
    if not ctx.last_attempt and requests_left > 0:
        logger.info(
            "reply emoji rejected job_id=%s problems=%d fixed=%d attempt=%d requests_left=%d",
            deps.job_id,
            len(checked.problems),
            checked.fixed,
            ctx.retry,
            requests_left,
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
    # Nothing valid left (a reply of only bad emoji): sitting out beats posting them.
    # A server that disallows silence fails the run rather than posting them.
    answer = checked.stripped if checked.stripped.strip() else NO_REPLY
    logger.warning(
        "reply emoji cut job_id=%s problems=%d fixed=%d silent=%s",
        deps.job_id,
        len(checked.problems),
        checked.fixed,
        answer == NO_REPLY,
    )
    return answer
