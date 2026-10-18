"""Version 1 wire contract shared with the Rust dispatcher."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Message(Contract):
    role: Literal["system", "user", "assistant"]
    content: str


class Scope(Contract):
    # Discord snowflakes are strings to avoid integer precision loss.
    guild_id: str
    channel_id: str
    conversation_id: str


class Person(Contract):
    ref: str = Field(pattern=r"^u[0-9]+$")
    user_id: str = Field(pattern=r"^[0-9]+$")
    label: str = Field(max_length=200)
    names: list[str] = Field(default_factory=list, max_length=20)


class JobRequest(Contract):
    id: str = Field(min_length=1, max_length=200)
    kind: Literal["reply", "topic"]
    scope: Scope
    model: str = Field(min_length=1)
    messages: list[Message] = Field(min_length=1, max_length=200)
    max_output_tokens: int = Field(gt=0, le=16000)
    people: list[Person] = Field(default_factory=list, max_length=500)
    # OpenRouter image model for the generate_image tool; None leaves it out.
    image_model: str | None = Field(default=None, min_length=1)


class DescribeEmojisRequest(Contract):
    """One batch of a server's undescribed custom emoji (`/kiki emojis resync:true`)."""

    guild_id: str = Field(pattern=r"^[0-9]+$")
    model: str = Field(min_length=1)
    # Database clock, Unix ms: emoji already tried since then are skipped.
    attempted_before_ms: int = Field(ge=0)
    redescribe: bool = False


class DescribeEmojisResult(Contract):
    described: int = Field(ge=0)
    failed: int = Field(ge=0)
    remaining: int = Field(ge=0)
    cost: float | None = Field(default=None, ge=0, allow_inf_nan=False)


class Usage(Contract):
    prompt_tokens: int = Field(default=0, ge=0, le=2147483647)
    completion_tokens: int = Field(default=0, ge=0, le=2147483647)
    cost: float | None = Field(default=None, ge=0, allow_inf_nan=False)


class WebUse(Contract):
    """OpenRouter web tools used by the run (informational; Rust ignores it)."""

    searches: int = Field(default=0, ge=0)
    fetches: int = Field(default=0, ge=0)
    sources: list[str] = Field(default_factory=list)


class Image(Contract):
    """A generated image, saved in `agent_host_images`; Rust posts it."""

    id: int = Field(gt=0)
    mime_type: str


# What OpenRouter's GPT Image models take (its image catalog); other models get the
# ratio as given and may reject it.
AspectRatio = Literal["1:1", "3:2", "2:3", "4:3", "3:4", "16:9", "9:16", "21:9"]


class Subject(Contract):
    """Someone or something in a drawing, as the reply's generate_image call named it.
    The image job finds reference images of it (docs/image-generation.md)."""

    name: str = Field(min_length=1, max_length=100)
    # Who or what it is and what it looks like, as far as Kiki knows.
    description: str = Field(default="", max_length=600)
    # A person: their user ID (resolved from the roster ref Kiki gave).
    user_id: str | None = Field(default=None, pattern=r"^[0-9]+$")
    # Kiki herself: her earlier self-portraits are her references.
    kiki: bool = False
    # Messages whose images show it, chosen by Kiki or the people asking.
    photos: list[str] = Field(default_factory=list, max_length=8)


class Drawing(Contract):
    """An image the reply queued: Rust posts the reply, then runs it as an image job."""

    prompt: str = Field(min_length=1, max_length=4000)
    subjects: list[Subject] = Field(default_factory=list, max_length=6)
    style: str | None = Field(default=None, max_length=300)
    aspect_ratio: AspectRatio | None = None
    # Kiki's words to post with the image.
    caption: str = Field(default="", max_length=1500)
    # Changing a posted photo (the first subject's first photo) rather than a new picture.
    edit: bool = False


class JobResult(Contract):
    text: str = Field(min_length=1, max_length=100000)
    model: str | None = None
    steps: int = Field(default=1, ge=0, le=2147483647)
    usage: Usage = Field(default_factory=Usage)
    web: WebUse | None = None
    # Results saved before drawings were queued (attached to the reply itself).
    images: list[Image] = Field(default_factory=list, max_length=10)
    drawings: list[Drawing] = Field(default_factory=list, max_length=2)


class ImageJobRequest(Contract):
    """One queued drawing (`POST /v1/images`), with ID `image:<run>:<n>`."""

    id: str = Field(min_length=1, max_length=200)
    scope: Scope
    image_model: str = Field(min_length=1)
    # The reply job that queued it: its prompt gives a failure note Kiki's voice.
    reply_job: str = Field(min_length=1, max_length=200)
    chat_model: str = Field(min_length=1)
    # What Kiki said when she queued it, for the failure note.
    ack: str = Field(default="", max_length=10000)
    drawing: Drawing


class Reference(Contract):
    """One reference image the image model saw, in order."""

    subject: str
    message_id: str | None = None
    attachment_id: str | None = None
    # The person whose Discord avatar it is.
    avatar_of: str | None = None
    # An earlier drawing of Kiki (`agent_host_images`).
    drawing_id: int | None = None


class ImageJobResult(Contract):
    """A drawn image, or why not and the note Kiki wrote about it."""

    image: Image | None = None
    caption: str = ""
    prompt: str = ""
    references: list[Reference] = Field(default_factory=list)
    model: str
    cost: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    latency_ms: int = Field(default=0, ge=0)
    # refused | failed, with Kiki's note to post instead (empty if she couldn't write one).
    error: str | None = None
    failure_note: str = ""


class JobResponse(Contract):
    id: str
    status: Literal["running", "completed", "failed"]
    result: JobResult | ImageJobResult | None = None
    error: str | None = None
