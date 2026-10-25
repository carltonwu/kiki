"""The `generate_image` tool: queues a picture to draw after the reply
(docs/image-generation.md)."""

import json
import logging
import re
from typing import Annotated, Any, get_args

from pydantic import BaseModel, BeforeValidator, Field, model_validator
from pydantic_ai import RunContext, Tool
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.toolsets import FunctionToolset

from ...chat.attachments import images_in
from ...chat.scope import HEARD, resolve_person, speakers
from ...contracts import AspectRatio, Drawing, Subject
from ...media import links
from ...media.drawing import DRAWN_BY_KIKI, PICK
from ...media.links import reddit, x
from ..deps import Deps
from ..guard import guarded

logger = logging.getLogger(__name__)


MAX_IMAGES = 2  # pictures one reply may queue
# Seen in prod: four asked for, two queued, and Kiki said "all four queued".
TOO_MANY = (
    f"Not queued: at most {MAX_IMAGES} pictures per reply, and {MAX_IMAGES} already are. "
    f"Only those {MAX_IMAGES} are coming: say so, and that they can ask for the rest next."
)
RECENT_MESSAGES = 200  # the channel's latest messages, for who's been around
MENTION_NAME = re.compile(r"^<@!?[0-9]+>$")
# A message ID (a snowflake) in a subject's name, and the clause around it.
# What a subject's name may carry besides the name: a mention (kept, it names a
# person), a Discord message link or a bare message ID (photos).
NAME_TOKEN = re.compile(
    r"(?P<mention><(?:@[!&]?|#)[0-9]+>)"
    r"|(?P<link>(?:https?://)?(?:\w+\.)?discord(?:app)?\.com/channels/[0-9@me]+/[0-9]+/(?P<message>[0-9]+)\S*)"
    r"|(?P<id>\b[0-9]{17,20}\b)"
)
# A bracketed aside holding a photo: "(from the selfie he shared, message 123…)".
NAME_ASIDE = re.compile(r"\s*[(\[][^)\]]*[)\]]")


def listed(value: Any) -> Any:
    """A list as models send one: null for none, one item on its own, or the
    list as a JSON string."""
    if value is None:
        return []
    if isinstance(value, str) and value.strip().startswith("["):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return [value]
    return value if isinstance(value, list) else [value]


# Message IDs, sent as numbers as often as strings.
IDs = Annotated[
    list[Annotated[str, BeforeValidator(lambda v: str(v) if isinstance(v, int) else v)]],
    BeforeValidator(listed),
]


class Pictured(BaseModel):
    """Someone or something in the picture."""

    @model_validator(mode="before")
    @classmethod
    def lenient(cls, value: Any) -> Any:
        """Nulls for left-out fields, a ref as a bare number (2 for u2), and a
        subject given only by ref or as Kiki."""
        if isinstance(value, str):
            return {"name": value}
        if not isinstance(value, dict):
            return value
        value = {k: v for k, v in value.items() if v is not None}
        # A message ID put in the name ("Reece (from the selfie he shared, message
        # 1561574753435652146)", seen in prod with no photos) is a photo.
        name = str(value.get("name") or "")
        if not value.get("photos") and (photos := photos_in(name)):
            value["photos"] = photos
            value["name"] = without_photos(name) or name
        if isinstance(value.get("person"), int):
            value["person"] = f"u{value['person']}"
        if not str(value.get("name") or "").strip() and (value.get("me") or value.get("person")):
            value["name"] = "Kiki" if value.get("me") else value["person"]
        return value

    name: str = Field(description='What to call them in the picture, like "Ethan" or "Nougie".')
    person: str | None = Field(
        default=None, description="Their ref, like u2, when it's someone in this server."
    )
    me: bool = Field(default=False, description="True when it's you, Kiki.")
    photos: IDs = Field(
        default_factory=list,
        description="IDs of messages whose images show them, when you or the people asking "
        "already picked some (like the message in [image: …; message 123]; a Discord message "
        "or attachment link they pasted works too). Add :N for only its Nth image (123:2). "
        "Only photos of them: a picture to redraw them into goes in shared. Otherwise leave "
        "it empty: the drawing step finds their photos in the server's history.",
    )
    description: str = Field(
        default="",
        description="Who or what they are and what they look like, as far as you know from "
        'the chat or memory, like "traumagotchi\'s dog, a small fluffy white mutt".',
    )


def photos_in(name: str) -> list[str]:
    """Message IDs a name carries: a message link's message (not its guild or
    channel), or a bare ID. Mentions are names, not photos."""
    return [t["message"] or t["id"] for t in NAME_TOKEN.finditer(name) if t["message"] or t["id"]]


def without_photos(name: str) -> str:
    """The name without the photos in it: an aside holding one goes whole."""

    def aside(m: re.Match) -> str:
        return "" if photos_in(m[0]) else m[0]

    def token(m: re.Match) -> str:
        return m[0] if m["mention"] else ""

    return NAME_TOKEN.sub(token, NAME_ASIDE.sub(aside, name)).strip(" ,;:-")


# Lenient where models slip: a bare name for a subject, nulls, numbers for IDs, one
# item for a list, or a ratio the image model doesn't take. Each rejection costs a
# tool retry, and running out of them fails the whole reply (seen in prod).
Subjects = Annotated[list[Pictured] | None, BeforeValidator(listed)]
Shared = Annotated[IDs | None, BeforeValidator(listed)]
Text = Annotated[str, BeforeValidator(lambda v: "" if v is None else v)]
Flag = Annotated[bool, BeforeValidator(lambda v: False if v is None else v)]
Ratio = Annotated[
    AspectRatio | None, BeforeValidator(lambda v: v if v in get_args(AspectRatio) else None)
]
DRAW_RETRIES = 3
# A roster line as the prompt shows people: optional ref, name, optional @username.
LABEL = re.compile(r"^(?:u[0-9]+\s+)?(?P<name>.+?)(?:\s*\(@(?P<username>[\w.]+)\))?$")
# Links people paste for a photo: a message, or an attachment on Discord's CDN.
MESSAGE_LINK = re.compile(r"discord(?:app)?\.com/channels/[0-9]+/[0-9]+/(?P<message>[0-9]+)")
ATTACHMENT_LINK = re.compile(
    r"(?:cdn|media)\.discord(?:app)?\.(?:com|net)/attachments/(?P<channel>[0-9]+)/(?P<attachment>[0-9]+)/"
)
# An attachment's ID is a snowflake made as it's uploaded, just before its message's:
# the message is within this many IDs after it (a minute of snowflake time).
ATTACHMENT_TO_MESSAGE = 60_000 << 22
# A link fixer deletes a post and reposts it within seconds (migration 0024).
REPOST_WINDOW = 30_000 << 22
MAX_SHARED = 4


async def generate_image(
    ctx: RunContext[Deps],
    prompt: str,
    subjects: Subjects | None = None,
    style: str | None = None,
    aspect_ratio: Ratio = None,
    caption: Text = "",
    edit: Flag = False,
    shared: Shared = None,
    **unknown: Any,  # keys the model made up (negative_prompt): ignored, not a retry
) -> dict:
    """Make a picture. It's drawn after your reply and posted as its own message.

    Use it when someone asks for a picture, drawing, meme or visual, or when one
    would clearly land better than words. Your reply goes out first and lets them
    know it's on the way; the picture follows in about half a minute, replying to
    their message, with your caption. If drawing fails, you get to tell them.
    A picture the context lists as still being drawn is on its way: don't queue
    it again unless someone asks for something different.

    Name every person, pet or thing whose real look matters in `subjects`: the
    drawing step finds photos of them in the server's history (and people's
    avatars), checks them, and gives them to the image model as numbered
    references with what to keep the same, so their likeness carries over.

    Args:
        prompt: The picture, self-contained and literal (the image model knows
            nothing of the chat or the joke). Say what kind of image it is first,
            then the scene: who does what, where, with framing, gaze, hands and
            how they interact with things ("full body, looking down at the
            needle"); then camera and light as concrete cues ("phone photo at eye
            level, harsh fluorescent light") rather than mood words alone; then
            constraints. Any text in it goes in quotes with where it sits and
            "no other text". Call subjects by their names. Get real-world details
            right (a lumbar puncture goes in the lower back between the
            vertebrae). To edit, say only what changes.
        subjects: The people, pets and things in it whose look matters, you
            included (me: true) when you're in it.
        style: Only when they asked for one or the joke needs it, like "90s anime"
            or "oil painting". Leave it out for the default: a real, candid
            phone-camera photo without retouching.
        aspect_ratio: 3:4 or 4:3 for an everyday phone photo, 9:16 or 16:9 for
            tall or wide scenes, 1:1 for icons; square when left out.
        caption: Your words to post with the picture, written for it.
        edit: True to change a posted photo rather than make a new picture: put
            its message ID first in the first subject's photos, and the prompt
            says only what to change; everything else is kept.
        shared: Messages whose images the picture is made from but that don't
            show a subject: the photo, meme, template or post to redraw or work
            from, by message ID (123, or 123:2 for only its 2nd image, numbered
            as read_image and read_link number them), Discord link, or the
            post's link (an X or Reddit post shared here). The images the newest
            messages carry, reply to or link go in anyway; name it here when
            it's from earlier in the chat, like a tweet asked about a few
            messages ago, or to send only one of a post's images. A pose,
            expression or outfit to copy goes here too: when a follow-up asks
            for the same with someone else ("now do Eden", "his turn"), pass
            the earlier request's image again, or the image model only gets
            your words for it.
    """

    async def work():
        deps = ctx.deps
        if len(deps.drawings) >= MAX_IMAGES:
            return {"error": TOO_MANY}
        pictured = []
        for s in subjects or []:
            user_id = None
            # Models copy roster labels as names: "u2 Ethan (@wheatthin512)".
            label = LABEL.match(s.name.strip())
            name = label["name"] if label else s.name.strip()
            if s.person and s.person.strip():
                ref = s.person.strip()
                if ref not in deps.roster.ids:
                    raise ValueError(f"{ref} isn't in this conversation's people list")
                user_id = str(deps.roster.ids[ref])
            elif not s.me:
                # Models often name a person without their ref: a name only one
                # person here has gone by is them, so their own photos are used.
                tries = [f"@{label['username']}"] if label and label["username"] else []
                for who in [*tries, name]:
                    ids = await resolve_person(deps.connection, deps.scope, deps.roster, who)
                    if len(ids) > 1:
                        ids = await going_by(deps, ids, who)
                    if len(ids) > 1:
                        # A person, but which: never a thing (searched by name, a
                        # game character and a monkey became Reece's references).
                        who_is = await speakers(deps.connection, deps.scope, deps.roster, set(ids))
                        raise ValueError(
                            f"{who!r} could be {' or '.join(sorted(who_is.values()))}: give "
                            "the person's ref, or ask who they mean"
                        )
                    if len(ids) == 1:
                        user_id = str(ids[0])
                        if MENTION_NAME.match(name):  # <@id>: draw them by their name
                            names = await speakers(
                                deps.connection, deps.scope, deps.roster, {ids[0]}
                            )
                            label = LABEL.match(names.get(ids[0], name))
                            name = label["name"] if label else name
                        break
            photos = [await photo_message(deps, p.strip()) for p in s.photos if p.strip()]
            if missing := await without_images(deps, photos):
                raise ValueError(f"No message with images among {missing} in this server")
            if not (s.me or edit) and (drawn := await drawn_by_kiki(deps, photos)):
                # Seen in prod: her own "starring reece" picture as Reece's photo, so
                # the next one was drawn from a drawing and drifted further.
                raise ValueError(
                    f"{', '.join(drawn)} is a picture you drew, not a photo of {name}: put it "
                    "in shared to redraw it, or leave photos empty to use real photos of them"
                )
            pictured.append(
                Subject(
                    name=name,
                    description=s.description,
                    user_id=user_id,
                    kiki=s.me,
                    photos=photos[:4],
                )
            )
        given = [await photo_message(deps, p.strip()) for p in shared or [] if p.strip()]
        if missing := await without_images(deps, given):
            raise ValueError(f"No images in {missing} in this server")
        if edit and not (pictured and pictured[0].photos):
            raise ValueError(
                "To edit, put the photo's message ID first in the first subject's photos"
            )
        drawing = Drawing(
            prompt=prompt.strip(),
            subjects=pictured,
            style=(style or "").strip() or None,
            aspect_ratio=aspect_ratio,
            caption=caption.strip(),
            edit=edit,
            shared=list(dict.fromkeys(given))[:MAX_SHARED],
        )
        # Checked again after the awaits above: parallel calls each passed the first.
        if len(deps.drawings) >= MAX_IMAGES:
            return {"error": TOO_MANY}
        deps.drawings.append(drawing)
        logger.info(
            "drawing queued job_id=%s subjects=%d photos=%d shared=%d styled=%s",
            deps.job_id,
            len(pictured),
            sum(len(s.photos) for s in pictured),
            len(drawing.shared),
            drawing.style is not None,
        )
        return {
            "queued": True,
            "note": "It's drawn after your reply and posted as its own message. Your reply "
            "now is what they see while they wait: let them know a picture is coming, your "
            "way. Don't describe it as if it's already posted, and don't say which photos "
            "it's drawn from: the drawing step picks them after your reply.",
        }

    return await guarded(ctx, "generate_image", work, budget=None)


async def going_by(deps: Deps, ids: list[int], name: str) -> list[int]:
    """Of the people a name has meant, those who go by it now (else all of them),
    narrowed to those in this conversation, then to those who spoke in this channel
    lately. More than one left is for the model to sort out. In prod Ethan had once been nicknamed "Reece", so "Reece" meant
    nobody and Reece was searched for as a thing: a game character and a monkey
    became his references."""
    cursor = await deps.connection.execute(
        "SELECT user_id FROM people WHERE guild_id = %s AND user_id = ANY(%s)"
        " AND name_norm(%s) IN (name_norm(coalesce(nick, '')), name_norm(coalesce(global_name, '')),"
        " name_norm(coalesce(username, '')))",
        (deps.scope.guild_id, ids, name.removeprefix("@")),
    )
    pool = [row[0] for row in await cursor.fetchall()] or ids
    if len(pool) > 1:  # several go by it: the one in this conversation
        pool = [i for i in pool if i in deps.roster.speakers] or pool
    if len(pool) > 1:  # or the one who spoke here lately
        cursor = await deps.connection.execute(
            "SELECT DISTINCT author_id FROM (SELECT author_id FROM messages WHERE channel_id = %s"
            " AND guild_id = %s AND deleted_at IS NULL ORDER BY id DESC LIMIT %s) recent"
            " WHERE author_id = ANY(%s)",
            (deps.scope.channel_id, deps.scope.guild_id, RECENT_MESSAGES, pool),
        )
        pool = [row[0] for row in await cursor.fetchall()] or pool
    return sorted(pool)


async def drawn_by_kiki(deps: Deps, picks: list[str]) -> list[str]:
    """The picks that are Kiki's own pictures (`DRAWN_BY_KIKI`)."""
    ids = [int(m[1]) for p in picks if (m := PICK.match(p))]
    if not ids:
        return []
    cursor = await deps.connection.execute(
        DRAWN_BY_KIKI, {"guild": deps.scope.guild_id, "ids": ids}
    )
    drawn = {str(row[0]) for row in await cursor.fetchall()}
    return [p for p in picks if p.split(":")[0] in drawn]


async def photo_message(deps: Deps, photo: str) -> str:
    """The message (as "ID", or "ID:N" for its Nth image) for a photo given as an
    ID, a message link, the link of an attachment (found among its channel's
    messages), or a post's link (the message in this channel that shared it). A link fixer's
    deleted original is its repost."""
    if m := PICK.match(photo):
        return await reposted(deps, int(m[1])) + (f":{m[2]}" if m[2] else "")
    if link := MESSAGE_LINK.search(photo):
        return await reposted(deps, int(link["message"]))
    if link := ATTACHMENT_LINK.search(photo):
        attachment = int(link["attachment"])
        cursor = await deps.connection.execute(
            "SELECT id, raw FROM messages WHERE guild_id = %s AND channel_id = %s"
            " AND id BETWEEN %s AND %s AND deleted_at IS NULL"
            " AND raw->'attachments' @> jsonb_build_array(jsonb_build_object('id', %s::text))"
            " ORDER BY id LIMIT 1",
            (
                deps.scope.guild_id,
                int(link["channel"]),
                attachment,
                attachment + ATTACHMENT_TO_MESSAGE,
                str(attachment),
            ),
        )
        if row := await cursor.fetchone():
            ids = [i.id for i in images_in(row[1] or {})]
            n = ids.index(str(attachment)) + 1 if str(attachment) in ids else 0
            return str(row[0]) + (f":{n}" if n else "")
        raise ValueError(f"No message in this server has the attachment {photo}")
    if needles := post_needles(photo):
        cursor = await deps.connection.execute(
            # This channel's, newest first: its index stops at the first match.
            f"SELECT m.id FROM messages m WHERE m.channel_id = %s AND m.guild_id = %s"
            f" AND m.deleted_at IS NULL AND {HEARD} AND EXISTS (SELECT 1 FROM unnest(%s::text[]) n"
            "   WHERE strpos(m.content, n) > 0 OR strpos(coalesce(m.raw->>'embeds', ''), n) > 0)"
            " ORDER BY m.id DESC LIMIT 1",
            (deps.scope.channel_id, deps.scope.guild_id, needles),
        )
        if row := await cursor.fetchone():
            return str(row[0])
        raise ValueError(f"No message in this channel shares {photo}")
    raise ValueError("photos must be message IDs, Discord links or a shared post's link")


def post_needles(url: str) -> list[str]:
    """What a message sharing this X or Reddit post contains, whichever host or
    link fixer it was pasted with."""
    link = links.normalize(url)
    if link is None or not links.reader_for(link):
        return []
    if status := x.status_id(link):
        return [f"/status/{status}"]
    if found := reddit.post_id(link):
        post = found[0]
        return [f"/comments/{post}", f"redd.it/{post}", f"/gallery/{post}"]
    return [link.split("://", 1)[1]]


async def reposted(deps: Deps, message_id: int) -> str:
    """The message, or the repost standing for it when a link fixer deleted it."""
    cursor = await deps.connection.execute(
        "SELECT r.id FROM messages o JOIN messages r ON r.channel_id = o.channel_id"
        " AND r.relayed_for = o.author_id AND r.id > o.id AND r.id < o.id + %s"
        " AND r.deleted_at IS NULL"
        " WHERE o.id = %s AND o.guild_id = %s AND o.deleted_at IS NOT NULL"
        " ORDER BY r.id LIMIT 1",
        (REPOST_WINDOW, message_id, deps.scope.guild_id),
    )
    row = await cursor.fetchone()
    return str(row[0] if row else message_id)


async def without_images(deps: Deps, picks: list[str]) -> list[str]:
    """The picks with no image to draw from: no message here, or one with no
    attached image, link preview or readable post (whose photos the drawing
    fetches), or fewer images than the Nth asked for."""
    if not picks:
        return []
    wanted = [(p, PICK.match(p)) for p in picks]
    ids = [int(m[1]) for _, m in wanted if m]
    cursor = await deps.connection.execute(
        f"SELECT m.id, m.content, m.raw FROM messages m WHERE m.guild_id = %s"
        f" AND m.id = ANY(%s) AND m.deleted_at IS NULL AND {HEARD}",
        (deps.scope.guild_id, ids),
    )
    rows = {id: (content, raw or {}) for id, content, raw in await cursor.fetchall()}
    missing = []
    for pick, m in wanted:
        row = rows.get(int(m[1])) if m else None
        if row is None:
            missing.append(pick)
            continue
        count = len(images_in(row[1]))
        if links.post_links(*row):
            continue  # the post's photos are counted when it's drawn
        if count == 0 or (m[2] and int(m[2]) > count):
            missing.append(pick)
    return missing


async def only_with_image_model(
    ctx: RunContext[Deps], tool_def: ToolDefinition
) -> ToolDefinition | None:
    """Offers `generate_image` only when the server has image generation on."""
    return tool_def if ctx.deps.image_model else None


toolset = FunctionToolset(
    [Tool(generate_image, prepare=only_with_image_model, max_retries=DRAW_RETRIES)]
)
