"""Custom emoji tools: find the server's emoji by meaning or by name."""

from pydantic_ai import RunContext
from pydantic_ai.toolsets import FunctionToolset

from ...chat import emojis
from ..deps import Deps
from ..guard import guarded


async def search_emojis(ctx: RunContext[Deps], query: str, limit: int = 5) -> dict:
    """Find this server's custom emoji by mood, reaction or what they show.

    To use one, write its `emoji` exactly as returned, like <:name:id>.

    Args:
        query: The feeling, reaction or subject, like "confused" or "laughing so hard".
        limit: Maximum results, up to 10.
    """

    async def work():
        return await emojis.search(
            ctx.deps.connection, ctx.deps.embed, ctx.deps.scope.guild_id, query, limit
        )

    return await guarded(ctx, "search_emojis", work)


async def find_emoji(ctx: RunContext[Deps], name: str) -> dict:
    """Look up this server's custom emoji by name, or explain one someone used.

    Args:
        name: An emoji name like huh or :huh:, or an emoji as it appears in a
            message, like <:huh:1085017182301339648>.
    """

    async def work():
        return await emojis.find(ctx.deps.connection, ctx.deps.scope.guild_id, name)

    return await guarded(ctx, "find_emoji", work)


toolset = FunctionToolset([search_emojis, find_emoji])
