"""The `read_image` tool: another model describes a message's images."""

from pydantic_ai import RunContext
from pydantic_ai.toolsets import FunctionToolset

from ...media import reading
from ..deps import Deps
from ..guard import guarded

VISION_TIMEOUT_SECONDS = 45  # downloads plus a vision model call


async def read_image(ctx: RunContext[Deps], message_id: str, question: str | None = None) -> dict:
    """Look at the images in one message: a single image or a whole gallery.

    Another model looks at them and describes each image in detail (with any
    text in it), and a gallery as a whole.

    Args:
        message_id: The message ID shown with its images, like "[image: cat.png;
            message 123]", or a message `id` from search results.
        question: Optional: what you want to know about them, so the
            description covers it.
    """

    async def work():
        if not message_id.strip().isdigit():
            raise ValueError("message_id must be the ID of a message with images")
        deps = ctx.deps
        return await reading.read_image(
            deps.connection, deps.scope, deps.roster, deps.vision, int(message_id), question
        )

    return await guarded(ctx, "read_image", work, timeout=VISION_TIMEOUT_SECONDS)


toolset = FunctionToolset([read_image])
