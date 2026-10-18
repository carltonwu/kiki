"""Pins what the host shows the outside world: the wire contract Rust speaks, the HTTP
routes, and what the chat model is offered on each kind of job (tools, their schemas
and the model settings). A change that keeps behavior keeps `snapshots/contract.json`
byte for byte; an intended change rewrites it with `UPDATE_SNAPSHOT=1 pytest`."""

import json
import os
from pathlib import Path
from types import SimpleNamespace

from fastapi.routing import APIRoute
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from kiki_agent_host import contracts
from kiki_agent_host.agent import PydanticExecutor
from kiki_agent_host.app import Settings, create_app
from kiki_agent_host.contracts import JobRequest

SNAPSHOT = Path(__file__).parent / "snapshots/contract.json"
WIRE = [
    contracts.JobRequest,
    contracts.JobResponse,
    contracts.ImageJobRequest,
    contracts.ImageJobResult,
    contracts.DescribeEmojisRequest,
    contracts.DescribeEmojisResult,
]


class FakeEmbeddings:
    async def create(self, model, input):
        data = [SimpleNamespace(index=i, embedding=[0.1] * 1024) for i in range(len(input))]
        return SimpleNamespace(data=data, usage=SimpleNamespace(cost=0))


async def offered(database_url: str, kind: str, image_model: str | None) -> dict:
    seen: list[AgentInfo] = []

    def respond(messages, info: AgentInfo) -> ModelResponse:
        seen.append(info)
        return ModelResponse(parts=[TextPart("ok")])

    class TestExecutor(PydanticExecutor):
        web_tools = ()  # FunctionModel can't run OpenRouter's server tools (see test_web.py)

        def model(self, name):
            return FunctionModel(respond)

    executor = TestExecutor(database_url, "unused-key")
    executor.embeddings = SimpleNamespace(embeddings=FakeEmbeddings())
    request = JobRequest.model_validate(
        {
            "id": f"{kind}:snapshot",
            "kind": kind,
            "scope": {"guild_id": "1", "channel_id": "2", "conversation_id": "2"},
            "model": "test/model",
            "messages": [
                {"role": "system", "content": "You are Kiki."},
                {"role": "user", "content": "alex: hi"},
            ],
            "max_output_tokens": 100,
            "people": [{"ref": "u1", "user_id": "100", "label": "alex", "names": ["alex"]}],
            **({"image_model": image_model} if image_model else {}),
        }
    )
    result = await executor.execute(request)
    [info] = seen
    return {
        "text": result.text,
        "model_settings": info.model_settings,
        "tools": [
            {
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters_json_schema,
                "kind": t.kind,
                "strict": t.strict,
            }
            for t in info.function_tools
        ],
    }


async def test_contract_matches_snapshot(database_url):
    app = create_app(Settings(database_url="postgres://unused", token="t"))
    contract = {
        "routes": sorted(
            [r.path, sorted(r.methods)] for r in app.routes if isinstance(r, APIRoute)
        ),
        "wire": {m.__name__: m.model_json_schema() for m in WIRE},
        "jobs": {
            "reply": await offered(database_url, "reply", None),
            "reply_with_image_model": await offered(database_url, "reply", "test/image"),
            "topic": await offered(database_url, "topic", None),
        },
    }
    text = json.dumps(contract, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    if os.getenv("UPDATE_SNAPSHOT"):
        SNAPSHOT.parent.mkdir(exist_ok=True)
        SNAPSHOT.write_text(text)
    assert text == SNAPSHOT.read_text()
