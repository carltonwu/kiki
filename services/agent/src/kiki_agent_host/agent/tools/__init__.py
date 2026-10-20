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
from ..guard import out_of_lookup_time, time_for_another_turn
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


@dataclass
class LastRequestAnswers(AbstractCapability[Deps]):
    """Sends no function tools on the last request the limit allows, including
    ones capabilities add (Skills' `load_capability`). A function tool call there
    would need one more request, and the limit would then fail the whole reply.
    OpenRouter's web search and fetch stay on: they run inside the request, so
    they don't need another. The same once the run is out of time for lookups
    (`guard`): a tool would only be turned away, at the cost of a request."""

    async def before_model_request(
        self, ctx: RunContext[Deps], request_context: ModelRequestContext
    ) -> ModelRequestContext:
        if ctx.usage.requests + 1 < ctx.deps.request_limit and not out_of_lookup_time():
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


async def current_catalog(deps: Deps) -> emoji_catalog.Catalog:
    """The server's emoji as of now. The Gateway may have added or deleted one
    since the run started, and the emoji tools read the table too. Indexing has
    its own connection, so this doesn't wait behind it."""
    return await emoji_catalog.catalog(deps.connection, deps.scope.guild_id)


@reply_agent.output_validator
async def check_emojis(ctx: RunContext[Deps], text: str) -> str:
    """Kiki's emoji must be the server's own, written the way Discord renders them.

    A known name is fixed in place: a missing or wrong ID, a missing `a` for an
    animated emoji, or a `:name:` shortcode. Each answer that writes emoji is
    checked against the catalog as it is now (`current_catalog`). An unusable
    emoji gets a retry while the run has an output retry and a model request
    left, and the time for one more turn (`guard.time_for_another_turn`). Otherwise
    it's cut (`emojis.check`'s `stripped`), keeping the reply's
    kind: a message stays a message and a reaction stays a reaction, so a cut
    never turns into silence the server may not allow. A retry costs at least
    one more request, more if the model searches first, and `LastRequestAnswers`
    keeps that within the limit.
    """
    if ctx.partial_output or not emoji_catalog.has_emoji(text):
        return text
    deps = ctx.deps
    deps.emojis = await current_catalog(deps)
    checked = emoji_catalog.check(text, deps.emojis)
    if not checked.problems:
        if checked.fixed:
            logger.info("reply emoji fixed job_id=%s fixed=%d", deps.job_id, checked.fixed)
        return checked.text
    requests_left = deps.request_limit - ctx.usage.requests
    if not ctx.last_attempt and requests_left > 0 and time_for_another_turn():
        logger.info(
            "reply emoji rejected job_id=%s problems=%d fixed=%d attempt=%d requests_left=%d",
            deps.job_id,
            len(checked.problems),
            checked.fixed,
            ctx.retry,
            requests_left,
        )
        if deps.emojis.by_name:
            how = (
                "Only use custom emoji your emoji tools returned or the chat shows, written "
                "exactly like <:name:id>; never write one from memory. Call search_emojis to "
                "find one by meaning, use a Unicode emoji character, or leave it out."
            )
        else:
            how = "This server has no custom emoji: use a Unicode emoji character or leave it out."
        raise ModelRetry(
            "Your reply uses emoji Discord can't show, so they would be posted as text "
            "(or, as reactions, silently left out):\n- "
            + "\n- ".join(checked.problems[: emoji_catalog.MAX_SUGGESTED])
            + (
                f"\n- and {len(checked.problems) - emoji_catalog.MAX_SUGGESTED} more"
                if len(checked.problems) > emoji_catalog.MAX_SUGGESTED
                else ""
            )
            + f"\n{how} Then write your whole reply again."
        )
    logger.warning(
        "reply emoji cut job_id=%s problems=%d fixed=%d stand_in=%s",
        deps.job_id,
        len(checked.problems),
        checked.fixed,
        emoji_catalog.STAND_IN in checked.stripped and emoji_catalog.STAND_IN not in text,
    )
    return checked.stripped
