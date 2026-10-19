"""The `read_link` tool: what a shared link contains, read from the site itself."""

from pydantic_ai import RunContext
from pydantic_ai.toolsets import FunctionToolset

from ...media import links
from ..deps import Deps
from ..guard import guarded

READ_TIMEOUT_SECONDS = 45  # the site's API, image downloads and a vision model call


async def read_link(
    ctx: RunContext[Deps], url_or_message_id: str, question: str | None = None
) -> dict:
    """Read what a shared link contains (a post, tweet or thread) on sites web fetch can't.

    Returns the post's author, text, stats and top comments, with its images
    described by another model. For a site without a reader it returns an error;
    fetch the page instead.

    Args:
        url_or_message_id: The link, or the ID of a message that contains it (its
            first readable link is read).
        question: Optional: what you want to know about it, so image descriptions
            cover it.
    """

    async def work():
        target = url_or_message_id.strip()
        deps = ctx.deps
        if target.isdigit():
            target = await links.link_in_message(deps.connection, deps.scope, int(target))
        return await links.read_link(deps, target, question)

    return await guarded(ctx, "read_link", work, timeout=READ_TIMEOUT_SECONDS)


toolset = FunctionToolset([read_link])
