"""A job's messages as Pydantic AI model messages."""

from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    TextPart,
    UserPromptPart,
)

from .contracts import Message


def history(messages: list[Message]) -> list[ModelMessage]:
    def convert(m: Message) -> ModelMessage:
        if m.role == "assistant":
            return ModelResponse(parts=[TextPart(m.content)])
        part = SystemPromptPart(m.content) if m.role == "system" else UserPromptPart(m.content)
        return ModelRequest(parts=[part])

    return [convert(m) for m in messages]
