"""Draw a picture the way Kiki's image job would, outside the job queue, and
optionally post it to a Discord channel as Kiki.

    railway run --service agent -- uv run --project services/agent \\
        python tools/redraw.py drawing.json --guild G --channel C \\
        --database-url postgresql://...@127.0.0.1:PORT/railway [--post]

`drawing.json` is a `Drawing` (services/agent contracts.py): Kiki's prompt, its
subjects and the messages shared with the request. References are found, checked
and numbered by the same code as a real job (`media/drawing.py`), and the server's
image model draws it, with the fallback model if its safety filter blocks it.

It only reads the database (use `railway connect postgres --tunnel-only` for prod):
the picture isn't saved to `agent_host_images`. It's written next to the drawing
file, and with `--post` sent to the channel with the drawing's caption.
`--include-deleted` replays a request whose shared messages were deleted since.
"""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

import httpx
from kiki_agent_host import db
from kiki_agent_host.agent.executor import PydanticExecutor
from kiki_agent_host.chat.scope import Scope
from kiki_agent_host.config import DEFAULT_IMAGE_FALLBACK_MODEL, cdn_base, discord_api
from kiki_agent_host.contracts import Drawing, ImageJobRequest
from kiki_agent_host.media import drawing

EXTENSIONS = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif"}


async def redraw(args: argparse.Namespace) -> None:
    plan = Drawing.model_validate_json(Path(args.drawing).read_text())
    request = ImageJobRequest(
        id="redraw",
        scope={"guild_id": args.guild, "channel_id": args.channel, "conversation_id": "0"},
        image_model=args.image_model,
        reply_job="redraw",
        chat_model=args.chat_model,
        drawing=plan,
    )
    executor = PydanticExecutor(
        args.database_url,
        os.environ["OPENROUTER_CHAT_API_KEY"],
        os.getenv("OPENROUTER_VISION_MODEL", "").strip(),
        os.environ["DISCORD_BOT_TOKEN"].strip(),
        image_fallback_model=os.getenv(
            "OPENROUTER_IMAGE_FALLBACK_MODEL", DEFAULT_IMAGE_FALLBACK_MODEL
        ).strip(),
    )
    vision = executor.vision(args.chat_model)
    scope = Scope(int(args.guild), int(args.channel), 0)
    if args.include_deleted:
        # Replaying an old request: link fixers delete the message people pasted.
        drawing.PHOTOS = drawing.PHOTOS.replace("AND m.deleted_at IS NULL", "")
    async with await db.connect(args.database_url) as connection:
        await connection.execute("SET default_transaction_read_only = on")
        gather = drawing.Gatherer(connection, scope, vision, cdn_base())
        found, shared = await asyncio.gather(
            asyncio.gather(*(gather.find(s) for s in plan.subjects)),
            gather.shared(plan.shared),
        )
    data, mime_type, cost, model, prompt, references = await executor.generate(
        request, list(found), shared
    )
    print(f"model: {model}  cost: ${cost + vision.cost:.4f}")
    for n, r in enumerate(references, 1):
        source = r.attachment_id or r.url or r.avatar_of or r.drawing_id
        print(f"reference {n}: {r.subject} (message {r.message_id}: {source})")
    print(f"\n{prompt}\n")
    out = Path(args.drawing).with_suffix("." + EXTENSIONS.get(mime_type, "bin"))
    out.write_bytes(data)
    print(f"saved {out}")
    if args.post:
        async with httpx.AsyncClient(timeout=60) as http:
            response = await http.post(
                f"{discord_api()}/channels/{args.channel}/messages",
                headers={"Authorization": f"Bot {os.environ['DISCORD_BOT_TOKEN'].strip()}"},
                data={"payload_json": json.dumps({"content": plan.caption})},
                files={"files[0]": (out.name, data, mime_type)},
            )
            response.raise_for_status()
            print(f"posted message {response.json()['id']}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("drawing", help="a Drawing as JSON")
    parser.add_argument("--guild", required=True)
    parser.add_argument("--channel", required=True)
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL"))
    parser.add_argument("--image-model", default="openai/gpt-image-2.5-flare")
    # Checks reference photos when OPENROUTER_VISION_MODEL isn't set.
    parser.add_argument("--chat-model", default="deepseek/deepseek-v4.1-flash-1017")
    parser.add_argument(
        "--include-deleted", action="store_true", help="use photos from deleted messages"
    )
    parser.add_argument("--post", action="store_true", help="post it to the channel")
    args = parser.parse_args()
    if not args.database_url:
        sys.exit("--database-url or DATABASE_URL is required")
    asyncio.run(redraw(args))


if __name__ == "__main__":
    main()
