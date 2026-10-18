"""Image jobs: drawing what a reply queued (docs/image-generation.md).

Kiki's reply names the subjects of a picture (people, pets, things, herself) and
Rust posts it right away; this job runs after it. For each subject it finds what
the subject looks like, best first:

1. photos Kiki or the people asking pointed at (`Subject.photos`);
2. images in the server's history: for a thing, posts whose text names it; for a
   person, their own posts that look like photos of them (captions like "me",
   camera JPEGs rather than screenshots) and other people's posts naming them.
   A vision model checks each candidate and keeps the clear ones;
3. a person's Discord avatar, if the same check says it's a photo of them;
4. for Kiki herself, her earlier self-portraits.

Then it writes the final prompt around Kiki's, numbering the references ("reference
images 1-2 show Ethan"), draws with them, and saves the image with what it was made
from. A failed drawing gets a note in Kiki's voice, written by the chat model from
the reply's own prompt, so the request never ends in silence.
"""

import asyncio
import logging
import re
from dataclasses import dataclass

import httpx
from psycopg import AsyncConnection, OperationalError
from psycopg.rows import dict_row
from pydantic_ai import Agent
from pydantic_ai.messages import BinaryContent, BinaryImage, ModelResponse, TextPart, UserContent
from pydantic_ai.models import Model
from pydantic_ai.usage import UsageLimits

from .contracts import Drawing, JobRequest, Reference, Subject
from .reading import images_in
from .retrieval import HEARD, Scope
from .vision import ImageRefused, Vision, billed_cost, client, download, look, refresh, sniff

logger = logging.getLogger(__name__)

PER_SUBJECT = 3  # references per subject: enough for a likeness, room for several subjects
CANDIDATE_MESSAGES = 10
CANDIDATES = 8  # images shown to the vision check per subject
VERIFY_TIMEOUT_SECONDS = 30
# One line per photo, without thinking (`look(think=False)`): a reasoning model
# spent over 2,000 tokens and 20 s on five photos, past the check's deadline.
VERIFY_MAX_TOKENS = 1500
NOTE_MAX_TOKENS = 400
# GPT Image edits take PNG, JPEG and WebP.
REFERENCE_TYPES = ("image/png", "image/jpeg", "image/webp")

# The final prompt's wording follows OpenAI's image prompting guide
# (https://developers.openai.com/api/docs/guides/image-prompting): ask for a real
# photograph and against retouching, label references by number and role, list
# what must stay fixed for identity, and phrase edits as "change only…, keep…".
PHOTO_STYLE = (
    "A real photograph, candid, taken on a phone camera: natural, realistic lighting, "
    "ordinary framing and everyday imperfections. No glamorization, no heavy retouching; "
    "not illustrated, painted or rendered."
)
PERSON_LOOK = "face, facial features, hair, skin tone and build"
THING_LOOK = "look (for an animal: breed, size, coat colors and markings)"
REFERENCES_ONLY = (
    "Use the reference images only for how these subjects look, not for their "
    "backgrounds, lighting, clothing or framing: make the new picture described above. "
    "Only the subjects named here look like their references; anyone else in the "
    "picture looks clearly different from them."
)
EDIT = (
    "Reference image 1 is the photo to edit: change only what's described above and "
    "keep everything else unchanged (faces, identity, pose, lighting, framing and "
    "background, unless the request changes them). Don't add new elements or text."
)

VERIFY = """\
You pick reference photos for an image generator. It will draw {name} ({what}), \
and the photos you approve show it what {name} looks like.

For each candidate below, first look at what the image actually is, then decide \
whether it clearly shows {name}. Captions help tell who is in a photo; they \
never make a meme, artwork or screenshot into a photo.
{rules}
Answer one line per candidate and nothing else, in this form, describing that \
image itself:
Image <number>: <what this image shows, in a few words> => <yes or no>"""

PERSON_RULES = """\
- Candidates come from {name}'s own posts or posts that name them, with their \
captions. Say yes for a real camera photo whose main person is plausibly {name}: \
a selfie or a photo of the poster ("me", "me tonight"), or a caption naming them. \
If several photos show the same person, that consistency supports them; a photo \
of someone who looks clearly different from the rest is no.
- Say no for screenshots, memes, art, illustrations, other people, photos of \
several people unless the caption says which one is {name}, and photos where the \
face isn't visible.
- The avatar is the picture they chose for their profile: yes only if it's a real \
photo of the same person as their other approved photos (or, with none, plainly a \
casual photo of an ordinary person rather than a celebrity, model or character)."""

THING_RULES = """\
- Candidates are posts whose captions name {name}. Say yes when {name} is clearly \
visible: the main subject, in focus, not tiny or hidden.
- Say no for screenshots, memes, drawings, photos where it isn't visible (a \
caption can name {name} while the photo shows something else), and photos of \
several animals or things where you can't tell which one is {name}."""

FAILURE_NOTE = """\
(Not from the chat:) The picture you just said you'd make couldn't be made: \
{reason}. Write the one short message you'd send now to tell them, in your own \
words and in the mood of the conversation. Don't promise to try again by \
yourself; they can ask again. Return only the message."""

note_writer = Agent()


@dataclass
class Candidate:
    """A possible reference image, and the reference it would be."""

    label: str
    reference: Reference
    url: str = ""
    media_type: str | None = None
    caption: str = ""


@dataclass
class Found:
    """A subject's chosen references, as bytes for the image model."""

    subject: Subject
    images: list[tuple[Reference, BinaryImage]]


# Messages with image attachments that may show a thing: their text names it.
THING_CANDIDATES = f"""
SELECT m.id, m.author_id, m.content, m.raw FROM messages m
WHERE m.guild_id = %(guild)s AND m.deleted_at IS NULL AND NOT m.author_bot AND {HEARD}
  AND jsonb_array_length(coalesce(m.raw->'attachments', '[]')) > 0
  AND m.content ~* %(pattern)s
ORDER BY m.id DESC LIMIT %(limit)s
"""

# A person's likely photos of themselves first: their own posts with captions about
# themselves ("me tonite" most of all), camera JPEGs (screenshots are PNGs), then
# posts naming them.
PERSON_CANDIDATES = f"""
SELECT m.id, m.author_id, m.content, m.raw FROM messages m
WHERE m.guild_id = %(guild)s AND m.deleted_at IS NULL AND NOT m.author_bot AND {HEARD}
  AND jsonb_array_length(coalesce(m.raw->'attachments', '[]')) > 0
  AND (m.author_id = %(user)s OR m.content LIKE %(mention)s OR m.content ~* %(pattern)s)
ORDER BY (m.author_id = %(user)s)::int * 2
         + (m.author_id = %(user)s AND m.content ~* %(first_person)s)::int * 2
         + (m.author_id = %(user)s AND m.content ~* %(selfie)s)::int * 2
         + coalesce(m.raw->'attachments'->0->>'content_type' = 'image/jpeg', false)::int * 2
         + (m.author_id <> %(user)s)::int DESC,
         m.id DESC
LIMIT %(limit)s
"""
FIRST_PERSON = r"\m(me|my|myself|i|im|i'm|selfie|us)\M"
SELFIE = r"^\W*(me|selfie|my face|pic of me|it'?s me|us)\M"

AVATAR = """
SELECT raw->'author'->>'avatar' AS user_hash, raw->'member'->>'avatar' AS member_hash
FROM messages
WHERE guild_id = %(guild)s AND author_id = %(user)s AND raw->'author' ? 'avatar'
ORDER BY id DESC LIMIT 1
"""

# Kiki's earlier pictures of herself, newest first (scoped to the guild by job).
SELF_PORTRAITS = """
SELECT i.id, i.data FROM agent_host_images i JOIN agent_host_jobs j ON j.id = i.job_id
WHERE j.request->'scope'->>'guild_id' = %(guild)s AND i.subjects @> '[{"kiki": true}]'
ORDER BY i.id DESC LIMIT %(limit)s
"""

PHOTOS = f"""
SELECT m.id, m.author_id, m.content, m.raw FROM messages m
WHERE m.guild_id = %(guild)s AND m.id = ANY(%(ids)s) AND m.deleted_at IS NULL AND {HEARD}
"""


def word(name: str) -> str:
    """A case-insensitive whole-word Postgres pattern for a name."""
    return r"\m" + re.sub(r"([.^$*+?()\[\]{}|\\])", r"\\\1", name.strip()) + r"\M"


def avatar_url(base: str, user_id: str, user_hash: str | None, guild: int, member: str | None):
    """A person's server avatar if they set one, else their account avatar (PNG)."""
    if member:
        return f"{base}/guilds/{guild}/users/{user_id}/avatars/{member}.png?size=512"
    if user_hash:
        return f"{base}/avatars/{user_id}/{user_hash}.png?size=512"
    return None


def candidates_in(rows: list[dict], per_message: int) -> list[Candidate]:
    """Each message's first images (as `read_image` lists them), as candidates."""
    found = []
    for m in rows:
        for image in images_in(m["raw"] or {})[:per_message]:
            if image.id is None:
                continue  # embeds are other sites' pictures, rarely the subject
            found.append(
                Candidate(
                    label="",
                    reference=Reference(
                        subject="", message_id=str(m["id"]), attachment_id=image.id
                    ),
                    url=image.url,
                    media_type=image.media_type,
                    caption=(m["content"] or "").strip()[:200],
                )
            )
    return found


class Gatherer:
    """Finds and checks reference images for one drawing."""

    def __init__(
        self,
        connection: AsyncConnection,
        scope: Scope,
        vision: Vision,
        cdn: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.connection = connection
        self.scope = scope
        self.vision = vision
        self.cdn = cdn
        self.transport = transport

    async def rows(self, query: str, params: dict) -> list[dict]:
        cursor = self.connection.cursor(row_factory=dict_row)
        await cursor.execute(query, {"guild": self.scope.guild_id, **params})
        return await cursor.fetchall()

    async def find(self, subject: Subject) -> Found:
        """`subject`, but a subject whose references can't be found is drawn from
        its description rather than failing the picture."""
        try:
            return await self.subject(subject)
        except OperationalError:
            raise
        except Exception as error:  # noqa: BLE001 - one subject must not sink the drawing
            logger.warning("references failed error_type=%s", type(error).__name__)
            return Found(subject, [])

    async def subject(self, subject: Subject) -> Found:
        """References for one subject, at most PER_SUBJECT."""
        name = subject.name
        chosen: list[tuple[Reference, BinaryImage]] = []
        if subject.photos:
            ids = [int(p) for p in subject.photos if p.isdigit()]
            given = candidates_in(await self.rows(PHOTOS, {"ids": ids}), per_message=4)
            # Picked by Kiki or the people asking: used without a check.
            chosen += [(c.reference, image) for c, image in await self.fetch(given[:PER_SUBJECT])]
        if subject.kiki and len(chosen) < PER_SUBJECT:
            limit = PER_SUBJECT - len(chosen)
            params = {"guild": str(self.scope.guild_id), "limit": limit}
            for row in await self.rows(SELF_PORTRAITS, params):
                if image := binary(bytes(row["data"])):
                    chosen.append((Reference(subject=name, drawing_id=row["id"]), image))
        if len(chosen) < PER_SUBJECT and not subject.kiki:
            candidates = await self.candidates(subject)
            approved = await self.verify(subject, candidates)
            chosen += [(c.reference, image) for c, image in approved][: PER_SUBJECT - len(chosen)]
        for reference, _ in chosen:
            reference.subject = name
        return Found(subject, chosen[:PER_SUBJECT])

    async def candidates(self, subject: Subject) -> list[Candidate]:
        if subject.user_id:
            rows = await self.rows(
                PERSON_CANDIDATES,
                {
                    "user": int(subject.user_id),
                    "mention": f"%<@{subject.user_id}>%",
                    "pattern": word(subject.name),
                    "first_person": FIRST_PERSON,
                    "selfie": SELFIE,
                    "limit": CANDIDATE_MESSAGES,
                },
            )
            found = candidates_in(rows, per_message=2)[: CANDIDATES - 1]
            for row in await self.rows(AVATAR, {"user": int(subject.user_id)}):
                url = avatar_url(
                    self.cdn,
                    subject.user_id,
                    row["user_hash"],
                    self.scope.guild_id,
                    row["member_hash"],
                )
                if url:
                    found.append(
                        Candidate(
                            label="avatar",
                            reference=Reference(subject="", avatar_of=subject.user_id),
                            url=url,
                            media_type="image/png",
                        )
                    )
            return found
        rows = await self.rows(
            THING_CANDIDATES, {"pattern": word(subject.name), "limit": CANDIDATE_MESSAGES}
        )
        return candidates_in(rows, per_message=2)[:CANDIDATES]

    async def fetch(self, candidates: list[Candidate]) -> list[tuple[Candidate, BinaryImage]]:
        """Downloads candidates, refreshing expired links; drops what fails or
        isn't a type the image model takes."""
        if not candidates:
            return []
        async with client(self.transport) as http:
            fresh = await refresh(http, self.vision.discord_token, [c.url for c in candidates])
            got = await asyncio.gather(
                *(download(http, fresh.get(c.url, c.url), c.media_type) for c in candidates)
            )
        fetched = []
        for c, result in zip(candidates, got, strict=True):
            if isinstance(result, str):
                logger.debug(
                    "reference skipped message_id=%s avatar_of=%s reason=%s",
                    c.reference.message_id,
                    c.reference.avatar_of,
                    result,
                )
            elif result.media_type in REFERENCE_TYPES:
                fetched.append((c, BinaryImage(result.data, media_type=result.media_type)))
        return fetched

    async def verify(
        self, subject: Subject, candidates: list[Candidate]
    ) -> list[tuple[Candidate, BinaryImage]]:
        """The candidates a vision model says clearly show the subject, in order."""
        usable = await self.fetch(candidates)
        if not usable:
            return []
        what = subject.description or ("a person in the group" if subject.user_id else "")
        rules = (PERSON_RULES if subject.user_id else THING_RULES).format(name=subject.name)
        prompt: list[UserContent] = [
            VERIFY.format(name=subject.name, what=what or "as named", rules=rules)
        ]
        for n, (c, image) in enumerate(usable, 1):
            label = f"Image {n}" + (" (their avatar)" if c.label == "avatar" else "")
            if c.caption:
                label += f'; caption: "{c.caption}"'
            prompt += [label, BinaryContent(image.data, media_type=image.media_type)]
        try:
            async with asyncio.timeout(VERIFY_TIMEOUT_SECONDS):
                answer = await look(self.vision, prompt, max_tokens=VERIFY_MAX_TOKENS, think=False)
        except (TimeoutError, ImageRefused) as error:
            logger.warning(
                "reference check failed subject_kind=%s error_type=%s",
                "person" if subject.user_id else "thing",
                type(error).__name__,
            )
            return []
        approved = verdicts(answer)
        return [pair for n, pair in enumerate(usable, 1) if n in approved]


def verdicts(answer: str) -> set[int]:
    """The image numbers the vision check approved ("Image 3: a selfie => yes")."""
    return {int(m[1]) for m in re.finditer(r"(?im)^\W*image\s+(\d+)\b[^\n]*?=>\W*yes\b", answer)}


def binary(data: bytes) -> BinaryImage | None:
    media_type = sniff(data[:12])
    if media_type not in REFERENCE_TYPES:
        return None
    return BinaryImage(data, media_type=media_type)


def compose(drawing: Drawing, found: list[Found]) -> tuple[str, list[Reference], list[BinaryImage]]:
    """The final prompt: Kiki's, then the style, then who's who, each reference
    labeled by its number (in the order the image model gets them) and its role,
    with what must stay the same, as OpenAI's image prompting guide advises."""
    references: list[Reference] = []
    images: list[BinaryImage] = []
    lines = []
    for f in found:
        first = len(images) + 1
        for reference, image in f.images:
            references.append(reference)
            images.append(image)
        last = len(images)
        name = f.subject.name
        line = f"- {name}"
        details = []
        if f.subject.description.strip():
            details.append(f.subject.description.strip().rstrip(".") + ".")
        if last >= first:
            span = f"image {first}" if first == last else f"images {first}-{last}"
            line += f", shown in reference {span}"
            keep = PERSON_LOOK if f.subject.user_id or f.subject.kiki else THING_LOOK
            details.append(f"Keep {name}'s {keep} the same; don't change {name}'s identity.")
        if details:
            line += ": " + " ".join(details)
        lines.append(line)
    parts = [drawing.prompt.strip()]
    style = (drawing.style or "").strip() or (None if drawing.edit else PHOTO_STYLE)
    if style:  # an edit keeps the photo's own look unless they asked for a style
        parts.append(f"Style: {style}")
    if lines:
        subjects = "Who and what is in it:\n" + "\n".join(lines)
        if images and drawing.edit:
            subjects += f"\n{EDIT}"
        elif images:
            subjects += f"\n{REFERENCES_ONLY}"
        parts.append(subjects)
    return "\n\n".join(parts), references, images


async def failure_note(
    connection: AsyncConnection,
    model: Model,
    reply_job: str,
    ack: str,
    reason: str,
) -> tuple[str, float]:
    """Kiki's message about a drawing that failed, written by the chat model with the
    reply's own prompt (her voice, the conversation), and its cost. Empty if even
    that fails: Rust then posts its minimal notice."""
    from .agent import history  # agent.py imports this module

    cursor = await connection.execute(
        "SELECT request FROM agent_host_jobs WHERE id = %s", (reply_job,)
    )
    row = await cursor.fetchone()
    if row is None:
        return "", 0.0
    try:
        request = JobRequest.model_validate(row[0])
        messages = history(request.messages)
        if ack.strip():
            messages.append(ModelResponse(parts=[TextPart(ack)]))
        run = await note_writer.run(
            FAILURE_NOTE.format(reason=reason),
            model=model,
            message_history=messages,
            model_settings={"max_tokens": NOTE_MAX_TOKENS, "openrouter_usage": {"include": True}},
            usage_limits=UsageLimits(request_limit=1),
        )
    except Exception as error:  # noqa: BLE001 - the notice must not fail the job
        logger.warning(
            "failure note failed reply_job=%s error_type=%s", reply_job, type(error).__name__
        )
        return "", 0.0
    return run.output.strip(), billed_cost(run) or 0.0
