"""Tool budgets per reply: lookups and memory each have their own, so running out of
one leaves the other, and drawing has neither. Memory calls also take turns on
their own connection."""

import asyncio
from collections import Counter
from types import SimpleNamespace

from psycopg import AsyncConnection
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from kiki_agent_host import db
from kiki_agent_host.agent.deps import MemorySession
from kiki_agent_host.agent.executor import PydanticExecutor
from kiki_agent_host.agent.tools import memory as memory_tools
from kiki_agent_host.contracts import JobRequest


class FakeEmbeddings:
    async def create(self, model, input):
        data = [SimpleNamespace(index=i, embedding=[0.1] * 1024) for i in range(len(input))]
        return SimpleNamespace(data=data, usage=SimpleNamespace(cost=0))


def calls(name: str, args: dict, n: int, start: int) -> ModelResponse:
    return ModelResponse(
        parts=[ToolCallPart(name, args, tool_call_id=str(start + i)) for i in range(n)]
    )


async def test_lookups_and_memory_run_out_separately(database_url):
    returns: list[ToolReturnPart] = []

    def respond(messages, info: AgentInfo) -> ModelResponse:
        returns.extend(p for p in messages[-1].parts if isinstance(p, ToolReturnPart))
        turn = sum(isinstance(m, ModelResponse) for m in messages)
        if turn == 0:
            return calls("find_emoji", {"name": "huh"}, 5, 0)
        if turn == 1:
            return calls("recall", {"subject": "u1"}, 7, 10)
        return ModelResponse(parts=[TextPart("done")])

    class TestExecutor(PydanticExecutor):
        web_tools = ()  # FunctionModel can't run OpenRouter's server tools (see test_web.py)

        def model(self, name):
            return FunctionModel(respond)

    # The emoji tools are offered once the server has emoji synced.
    async with await AsyncConnection.connect(database_url, autocommit=True) as db:
        await db.execute(
            "INSERT INTO guild_emojis (id, guild_id, name, raw) VALUES (11, 1, 'huh', '{}')"
        )
    executor = TestExecutor(database_url, "unused-key")
    executor.embeddings = SimpleNamespace(embeddings=FakeEmbeddings())
    request = JobRequest.model_validate(
        {
            "id": "reply:budgets",
            "kind": "reply",
            "scope": {"guild_id": "1", "channel_id": "2", "conversation_id": "2"},
            "model": "test/model",
            "messages": [{"role": "user", "content": "alex: hi"}],
            "max_output_tokens": 100,
            "people": [{"ref": "u1", "user_id": "100", "label": "alex", "names": ["alex"]}],
        }
    )
    assert (await executor.execute(request)).text == "done"

    def used_up(name: str) -> list[bool]:
        return [
            "budget used up" in str(r.content.get("error", ""))
            for r in sorted(returns, key=lambda r: int(r.tool_call_id))
            if r.tool_name == name
        ]

    assert used_up("find_emoji") == [False] * 4 + [True]
    assert used_up("recall") == [False] * 6 + [True]
    memory = next(r for r in returns if r.tool_name == "recall" and r.tool_call_id == "16")
    assert memory.content == {"error": "Memory budget used up. Answer with what you have."}


class FakeConnection:
    closed = False

    async def close(self):
        self.closed = True


async def test_memory_calls_queue_outside_the_deadline_and_a_timeout_drops_the_connection(
    monkeypatch,
):
    connections: list[FakeConnection] = []

    async def connect(database_url):
        connections.append(FakeConnection())
        return connections[-1]

    monkeypatch.setattr(db, "connect", connect)
    monkeypatch.setattr(memory_tools, "TIMEOUT_SECONDS", 0.2)
    deps = SimpleNamespace(
        memory=MemorySession("unused"),
        spent=Counter(),
        job_id="reply:memory",
        scope=SimpleNamespace(channel_id=2, conversation_id=2),
    )
    ctx = SimpleNamespace(deps=deps)

    async def stuck(connection):
        await asyncio.sleep(10)

    async def slow(connection):
        await asyncio.sleep(0.15)
        return {"connection": connections.index(connection)}

    first = asyncio.create_task(memory_tools.run(ctx, "recall", stuck))
    await asyncio.sleep(0.01)
    # Waits about 0.2 s for the lock, then still gets its whole deadline.
    second = await memory_tools.run(ctx, "recall", slow)
    assert await first == {"error": "Timed out."}
    assert connections[0].closed
    assert second == {"connection": 1}
