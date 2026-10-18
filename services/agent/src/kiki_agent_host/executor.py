"""The adapter boundary between the HTTP host and the agent framework."""

from typing import Protocol

from .contracts import (
    DescribeEmojisRequest,
    DescribeEmojisResult,
    ImageJobRequest,
    ImageJobResult,
    JobRequest,
    JobResult,
)


class TransientError(Exception):
    """A retryable failure (provider 429/5xx, network, database): not persisted,
    so Rust's retry of the same job ID runs it again."""


class Executor(Protocol):
    async def execute(self, request: JobRequest) -> JobResult: ...

    async def draw(self, request: ImageJobRequest) -> ImageJobResult: ...

    async def describe_emojis(self, request: DescribeEmojisRequest) -> DescribeEmojisResult: ...
