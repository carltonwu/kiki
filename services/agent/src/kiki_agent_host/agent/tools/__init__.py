"""The reply agent's tools, one toolset per capability. A new capability is a
module with a `toolset`, added to `TOOLSETS`; the model decides when to use it."""

import logging
import re
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

# How the chat shows a message's images (Rust `people.rs`). Kiki's own drawings
# show this way in her past turns, so she sometimes writes one as if that posted
# a picture (issue #78).
IMAGE_MARKER = re.compile(r" ?\[(?:image|gallery of [0-9]+ images): [^\[\]\n]*; message [0-9]+\]")

# A reply telling them a picture is on its way, as Kiki words it ("on the easel",
# "painting now", "give it 30 sec"). Without a generate_image call that's a promise
# nothing keeps: in prod over a dozen of her heads-ups came with no call, and
# people asked "wheres the mewing image?". Broad on purpose: a false hit costs one
# retry, in which she writes her reply again.
PROMISED_PICTURE = re.compile(
    r"\b(?:on|off) the easel\b"
    r"|\b(?:in|out of|fresh out(?: of)?) the (?:oven|kiln|fryer)\b"
    r"|\b(?:it'?s|now|currently|still|already|getting|pic'?s|picture'?s|one'?s)"
    r" (?:re)?(?:rendering|cooking|drawing|painting|loading|baking|generating|coming"
    r"|on (?:its|it's|the) way|queued)\b"
    r"|\b(?:(?:re)?(?:render|draw|paint|cook|build|load|generat|swapp|redo|work|putt|edit|mak"
    r"|doing)(?:ing)?|on) (?:it |that |this |one |him |her |them |you )?"
    r"(?:now|rn|right now|as we speak)\b"
    r"|\bqueued\b|\b(?:incoming|coming (?:up|right up|in hot|your way)|on (?:its|it's) way)\b"
    r"|\b(?:landing|dropping|lands?|drops?|up|here|there|ready|done) (?:in|within)"
    r" (?:a (?:sec|second|minute|moment|bit)|~?[0-9]+ ?(?:s|sec|secs|seconds|min)\b|like|about"
    r"|half|thirty|a few)\b"
    r"|\b(?:one|a|in a|just a) sec\b|\bgive it (?:a|like|about|~?[0-9]+|half|thirty)\b|\bgimme a\b"
    r"|\b(?:half a minute|thirty seconds|~[0-9]+ ?s\b|[0-9]+ ?(?:sec|secs|seconds)\b)",
    re.IGNORECASE,
)

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


@reply_agent.output_validator
async def check_image_markers(ctx: RunContext[Deps], text: str) -> str:
    """Kiki mustn't write the chat's image markers herself: one posts as text and
    draws nothing. Without a queued drawing, the model gets a retry, on the same
    terms as `check_emojis`, to call generate_image instead. Otherwise, or with
    drawing off, the markers are cut; a reply left empty gets `emojis.STAND_IN`."""
    if ctx.partial_output or not IMAGE_MARKER.search(text):
        return text
    deps = ctx.deps
    requests_left = deps.request_limit - ctx.usage.requests
    if (
        deps.image_model
        and not deps.drawings
        and not ctx.last_attempt
        and requests_left > 0
        and time_for_another_turn()
    ):
        logger.info(
            "reply image marker rejected job_id=%s attempt=%d requests_left=%d",
            deps.job_id,
            ctx.retry,
            requests_left,
        )
        raise ModelRetry(
            'Your reply contains an image marker like "[image: kiki-1.png; message 123]". '
            "The chat adds those to show images that were really posted; writing one posts "
            "only the text, and no picture. To send a picture, call generate_image. Then "
            "write your whole reply again without any marker."
        )
    logger.warning("reply image marker cut job_id=%s drawings=%d", deps.job_id, len(deps.drawings))
    # A line that held only markers goes; other lines are kept verbatim.
    lines = []
    for line in text.split("\n"):
        if IMAGE_MARKER.search(line):
            line = IMAGE_MARKER.sub("", line).rstrip()
            if not line.strip():
                continue
        lines.append(line)
    return "\n".join(lines).strip() or emoji_catalog.STAND_IN


@reply_agent.output_validator
async def check_promised_picture(ctx: RunContext[Deps], text: str) -> str:
    """A reply that says a picture is coming must have queued one. Without a
    drawing, the model gets one retry (with two requests left: the call and the
    reply) to call generate_image or take the promise back. Its second answer
    stands either way."""
    deps = ctx.deps
    if (
        ctx.partial_output
        or not deps.image_model
        or deps.drawings
        or deps.promise_checked
        or not PROMISED_PICTURE.search(text)
    ):
        return text
    deps.promise_checked = True
    requests_left = deps.request_limit - ctx.usage.requests
    if ctx.last_attempt or requests_left < 2 or not time_for_another_turn():
        logger.warning("reply promised picture kept job_id=%s", deps.job_id)
        return text
    logger.info(
        "reply promised picture rejected job_id=%s attempt=%d requests_left=%d",
        deps.job_id,
        ctx.retry,
        requests_left,
    )
    raise ModelRetry(
        "Your reply sounds like a picture is on its way, but nothing is queued: you haven't "
        "called generate_image in this turn, and words alone draw nothing, so they'd wait for "
        "a picture that never comes. If you mean to send one, call generate_image now, then "
        "write your reply. If your reply wasn't promising a picture, write it again as it was."
    )
