"""Time budgets inside a job: tools, model requests and drawing attempts each cut
their own timeout to what the job has left, so a slow reply still answers and a
slow drawing still explains itself, before the host's job timeout."""

import asyncio
import time
from collections import Counter
from types import SimpleNamespace

import pytest
from pydantic_ai import Agent
from pydantic_ai.exceptions import ContentFilterError
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel
from test_recording import FALLBACK, PRIMARY, ImageModel, executor, image_job

from kiki_agent_host import telemetry
from kiki_agent_host.agent import executor as agent_executor
from kiki_agent_host.agent import guard
from kiki_agent_host.agent.executor import Metered
from kiki_agent_host.agent.guard import guarded
from kiki_agent_host.deadline import run_deadline, time_left
from kiki_agent_host.media import generation


def tool_ctx():
    deps = SimpleNamespace(
        spent=Counter(), job_id="reply:1", scope=SimpleNamespace(channel_id=2, conversation_id=2)
    )
    return SimpleNamespace(deps=deps)


async def test_deadlines_nest_and_only_shorten():
    assert time_left() is None
    with run_deadline(None):
        assert time_left() is None
    with run_deadline(100):
        assert 99 < time_left() <= 100
        with run_deadline(500, margin=10):
            assert 89 < time_left() <= 90  # the outer deadline, less the margin
        with run_deadline(5):
            assert time_left() <= 5
        assert time_left() > 99
    assert time_left() is None


async def test_a_tool_near_the_run_deadline_does_not_start(recorded):
    ran = []

    async def work():
        ran.append(1)
        return {"messages": []}

    ctx = tool_ctx()
    with run_deadline(guard.ANSWER_RESERVE_SECONDS + 0.5):
        result = await guarded(ctx, "search_messages", work)
    assert result == {"error": guard.OUT_OF_TIME}
    assert not ran
    tool = {"tool": "search_messages"}
    # Its own outcome, and not charged to the lookup budget: it never ran.
    assert recorded.value("kiki.tools.calls", **tool, outcome="out_of_time") == 1
    assert recorded.value("kiki.tools.calls", **tool, outcome="budget_exhausted") is None
    assert ctx.deps.spent["Tool"] == 0


async def test_a_tool_gets_no_more_than_the_run_has_left(monkeypatch):
    monkeypatch.setattr(guard, "ANSWER_RESERVE_SECONDS", 0)

    async def slow():
        await asyncio.sleep(5)
        return {"messages": []}

    started = time.monotonic()
    with run_deadline(1.2):
        result = await guarded(tool_ctx(), "read_image", slow, budget=None, timeout=45)
    assert result == {"error": "Timed out."}
    assert time.monotonic() - started < 2


async def test_the_run_deadline_bounds_the_wait_for_a_tool_lock(monkeypatch):
    """Memory tools take turns on a lock before their own deadline starts."""
    monkeypatch.setattr(guard, "ANSWER_RESERVE_SECONDS", 0)
    lock = asyncio.Lock()
    await lock.acquire()  # a call that never finishes

    async def work():
        return {"entries": []}

    started = time.monotonic()
    with run_deadline(1.2):
        result = await guarded(tool_ctx(), "recall", work, budget=None, lock=lock)
    assert result == {"error": "Timed out."}
    assert time.monotonic() - started < 2


async def test_a_model_request_is_bounded_in_all(monkeypatch):
    """The SDK's own timeout is per read, and OpenRouter can hold a response
    open: the whole request, its retries included, gets a deadline."""
    monkeypatch.setattr(agent_executor, "MODEL_REQUEST_TIMEOUT_SECONDS", 0.2)

    async def hang(messages, info):
        await asyncio.Event().wait()

    model = Metered(FunctionModel(hang), telemetry.Call())
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        await Agent().run("hi", model=model)
    assert time.monotonic() - started < 1

    # And no later than the run deadline.
    monkeypatch.setattr(agent_executor, "MODEL_REQUEST_TIMEOUT_SECONDS", 45)
    started = time.monotonic()
    with run_deadline(0.2), pytest.raises(TimeoutError):
        await Agent().run("hi", model=model)
    assert time.monotonic() - started < 1


async def test_a_fast_model_request_is_unaffected():
    def answer(messages, info):
        return ModelResponse(parts=[TextPart("hey")])

    with run_deadline(30):
        result = await Agent().run("hi", model=Metered(FunctionModel(answer), telemetry.Call()))
    assert result.output == "hey"


async def test_the_fallback_is_not_tried_without_time_for_it(recorded):
    class SlowBlock(ImageModel):
        async def generate(self, prompt, *, images=None, settings=None):
            await asyncio.sleep(0.3)
            return await super().generate(prompt, images=images, settings=settings)

    run = executor(image_fallback_model=FALLBACK)
    run.image_models = {PRIMARY: SlowBlock(ContentFilterError("sexual", body=None))}
    run.image_models[FALLBACK] = ImageModel()
    # Time to try the server's model, but not, once it's blocked, the fallback.
    left = agent_executor.NOTE_TIMEOUT_SECONDS + agent_executor.MIN_IMAGE_SECONDS + 0.2
    with run_deadline(left), pytest.raises(generation.SafetyRefused):
        await run.generate(image_job(), [], [])
    assert recorded.value("kiki.images.attempts", model=FALLBACK, result="drawn") is None

    # With time for it, it is.
    with run_deadline(100):
        await run.generate(image_job(), [], [])
    assert recorded.value("kiki.images.attempts", model=FALLBACK, result="drawn") == 1


async def test_an_image_attempt_gets_what_the_drawing_has_left(monkeypatch):
    class Slow(ImageModel):
        async def generate(self, prompt, *, images=None, settings=None):
            await asyncio.sleep(5)

    monkeypatch.setattr(agent_executor, "NOTE_TIMEOUT_SECONDS", 0)
    monkeypatch.setattr(agent_executor, "MIN_IMAGE_SECONDS", 0.1)
    run = executor()
    run.image_models = {image_job().image_model: Slow()}
    started = time.monotonic()
    with run_deadline(0.5), pytest.raises(TimeoutError):
        await run.generate(image_job(), [], [])
    assert time.monotonic() - started < 1.5

    # Too little left to start one: no attempt at all.
    with run_deadline(0.05), pytest.raises(TimeoutError):
        await run.generate(image_job(), [], [])


async def test_the_failure_note_gives_up_in_time(monkeypatch):
    async def slow_note(*args):
        await asyncio.sleep(5)
        return "sorry", 0.01

    monkeypatch.setattr(agent_executor.drawing, "failure_note", slow_note)
    run = executor(model=None)
    started = time.monotonic()
    with run_deadline(0.2):
        assert await run.failure_note(None, image_job(), "the image model took too long") == (
            "",
            0.0,
        )
    assert time.monotonic() - started < 1


async def test_within_caps_a_phase_to_the_time_left():
    from kiki_agent_host.deadline import within

    assert within(4) == 4
    with run_deadline(2):
        assert 1.9 < within(4) <= 2
        assert within(1) == 1
    with run_deadline(-1):
        assert within(4) == 0


async def test_the_deadline_reads_the_same_in_a_worker_thread():
    with run_deadline(30):
        here = time_left()
        there = await asyncio.to_thread(time_left)
    assert 29 < there <= here <= 30


async def test_a_request_waiting_out_a_rate_limit_is_cut_at_its_own_cap(monkeypatch):
    """Through the real OpenAI client: OpenRouter's 429 asks for 60 s, the SDK
    waits, and the request's cap fires while the job still has time. That's a
    TimeoutError the job treats as a step's, not its own (test_host.py)."""
    import httpx2
    from openai import AsyncOpenAI
    from pydantic_ai.providers.openrouter import OpenRouterProvider

    from kiki_agent_host.openrouter.chat import WebOpenRouterModel

    def limited(request):
        return httpx2.Response(
            429, headers={"retry-after": "60"}, json={"error": {"message": "slow down"}}
        )

    client = AsyncOpenAI(
        base_url="http://openrouter.test/api/v1",
        api_key="test",
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(limited)),
    )
    model = WebOpenRouterModel("test/model", provider=OpenRouterProvider(openai_client=client))
    monkeypatch.setattr(agent_executor, "MODEL_REQUEST_TIMEOUT_SECONDS", 0.3)
    started = time.monotonic()
    with run_deadline(100), pytest.raises(TimeoutError):
        await Agent().run("hi", model=Metered(model, telemetry.Call()))
    assert time.monotonic() - started < 1
    with run_deadline(100):
        assert time_left() > 90  # plenty left: a retry has time


async def test_a_thinking_request_leaves_the_fallback_its_time(monkeypatch):
    """PR #68's arrangement, as `chat_model` builds it: a thinking model, then the
    same request without thinking. A thinking request that would run into the
    deadline is cut short, so the fallback still gets its time and answers."""
    from pydantic_ai.models.fallback import FallbackModel

    async def thinks_forever(messages, info):
        await asyncio.sleep(5)

    def answers(messages, info):
        return ModelResponse(parts=[TextPart("quick answer")])

    class Thinking(executor().__class__):
        def model(self, name):
            return FunctionModel(thinks_forever)

        def unthinking(self, name):
            return FunctionModel(answers)

        async def thinks(self, name):
            return True

    monkeypatch.setattr(agent_executor, "UNTHINKING_SECONDS", 0.5)
    model = await Thinking("postgresql://unused.invalid/kiki", "k").chat_model(
        "test/thinker", telemetry.Call(), 100
    )
    assert isinstance(model, FallbackModel)
    started = time.monotonic()
    with run_deadline(1.5):
        result = await Agent().run("hi", model=model)
    assert result.output == "quick answer"
    assert time.monotonic() - started < 1.5

    # Without a fallback to keep time for, the request has the run's time.
    with run_deadline(0.3), pytest.raises(TimeoutError):
        await Agent().run("hi", model=Metered(FunctionModel(thinks_forever), telemetry.Call()))


def test_the_reserve_covers_a_thinking_request_and_its_fallback():
    # prod's slowest thinking request was 31 s (reply:404)
    assert guard.ANSWER_RESERVE_SECONDS >= 31 + agent_executor.UNTHINKING_SECONDS


async def test_a_tool_sees_its_own_deadline():
    """Inside a tool the deadline is the tool's, so its steps can share it out
    (look() keeps time for its retry)."""
    seen = []

    async def work():
        seen.append(time_left())
        return {}

    with run_deadline(100):
        await guarded(tool_ctx(), "read_image", work, budget=None, timeout=4)
    assert 3.5 < seen[0] <= 4


async def test_a_thinking_request_at_the_plain_cap_falls_back_too(monkeypatch):
    """Review round 2: with plenty of run time (100 s), a hanging thinking request
    hits the 45 s cap, not the run's. That must fall back to the model without
    thinking, not fail the job (a 503 that reruns it, possibly twice)."""

    async def thinks_forever(messages, info):
        await asyncio.sleep(30)

    def answers(messages, info):
        return ModelResponse(parts=[TextPart("quick answer")])

    class Thinking(executor().__class__):
        def model(self, name):
            return FunctionModel(thinks_forever)

        def unthinking(self, name):
            return FunctionModel(answers)

        async def thinks(self, name):
            return True

    monkeypatch.setattr(agent_executor, "MODEL_REQUEST_TIMEOUT_SECONDS", 0.3)
    model = await Thinking("postgresql://unused.invalid/kiki", "k").chat_model(
        "test/thinker", telemetry.Call(), 100, "reply:7"
    )
    started = time.monotonic()
    with run_deadline(100):
        result = await Agent().run("hi", model=model)
    assert result.output == "quick answer"
    assert time.monotonic() - started < 2


async def test_a_request_given_up_on_is_logged_with_its_job_and_model(monkeypatch, caplog):
    async def hang(messages, info):
        await asyncio.Event().wait()

    monkeypatch.setattr(agent_executor, "MODEL_REQUEST_TIMEOUT_SECONDS", 0.2)
    model = Metered(FunctionModel(hang), telemetry.Call(), job_id="reply:9")
    with pytest.raises(TimeoutError):
        await Agent().run("hi", model=model)
    [line] = [r.message for r in caplog.records if "model request timed out" in r.message]
    assert "job_id=reply:9" in line and "model=function:hang" in line


def test_the_image_keeps_what_a_resumed_job_needs():
    """Review round 2: the stage left 35 s, not counting its 5 s grace, so a job
    resumed at 50 s got a 15 s attempt, under MIN_IMAGE_SECONDS: no attempt."""
    from kiki_agent_host.media import drawing

    assert drawing.IMAGE_KEEP_SECONDS == (
        agent_executor.MIN_IMAGE_SECONDS + agent_executor.NOTE_TIMEOUT_SECONDS + drawing.STAGE_GRACE
    )


async def test_a_resumed_drawing_still_gets_an_image_attempt():
    """A drawing job resumed 50 s in: 70 s left of 120, 60 s of work. The stage
    takes what it may, plus its grace, and the attempt still has its minimum."""
    from kiki_agent_host.media import drawing

    with run_deadline(60):
        stage = drawing.Gatherer(None, None, None, "", seconds=drawing.REFERENCES_SECONDS)
        used = stage.time_left() + drawing.STAGE_GRACE
        attempt = time_left() - used - agent_executor.NOTE_TIMEOUT_SECONDS
    assert attempt >= agent_executor.MIN_IMAGE_SECONDS - 0.1
