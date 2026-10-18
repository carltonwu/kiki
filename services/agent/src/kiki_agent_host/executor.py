"""The adapter boundary between the HTTP host and the agent framework."""

from typing import Protocol

from .contracts import JobRequest, JobResult


class TransientError(Exception):
    """A retryable failure (provider 429/5xx, network, database): not persisted,
    so Rust's retry of the same job ID runs it again."""


class Executor(Protocol):
    async def execute(self, request: JobRequest) -> JobResult: ...
