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


class Usage(Contract):
    prompt_tokens: int = Field(default=0, ge=0, le=2147483647)
    completion_tokens: int = Field(default=0, ge=0, le=2147483647)
    cost: float | None = Field(default=None, ge=0, allow_inf_nan=False)


class Image(Contract):
    """A generated image, saved in `agent_host_images`; Rust attaches it to the reply."""

    id: int = Field(gt=0)
    mime_type: str


class JobResult(Contract):
    text: str = Field(min_length=1, max_length=100000)
    model: str | None = None
    steps: int = Field(default=1, ge=0, le=2147483647)
    usage: Usage = Field(default_factory=Usage)
    images: list[Image] = Field(default_factory=list, max_length=10)


class JobResponse(Contract):
    id: str
    status: Literal["running", "completed", "failed"]
    result: JobResult | None = None
    error: str | None = None
