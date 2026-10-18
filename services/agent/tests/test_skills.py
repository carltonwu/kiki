"""The reply agent's skills: offered by name and description, loaded on request."""

from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from kiki_agent_host.agent.tools import SKILLS


async def test_unslop_is_offered_and_loads():
    loaded: list[str] = []

    def model(messages, info: AgentInfo) -> ModelResponse:
        if len(messages) == 1:
            assert "- unslop: Cut AI tells" in info.instructions
            return ModelResponse(parts=[ToolCallPart("load_capability", {"id": "unslop"})])
        loaded.extend(str(p.content) for p in messages[-1].parts if isinstance(p, ToolReturnPart))
        return ModelResponse(parts=[TextPart("ok")])

    await Agent(capabilities=[SKILLS]).run("explain the lore", model=FunctionModel(model))

    assert "# Skill: unslop" in loaded[0]
    assert "Em dash overuse" in loaded[0]
