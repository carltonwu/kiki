import httpx
from psycopg import AsyncConnection
from pydantic_ai.exceptions import ModelHTTPError, UnexpectedModelBehavior
from test_deadlines import tool_ctx
from test_host import HEADERS, TOKEN, FakeExecutor, client, job

from kiki_agent_host import faults
from kiki_agent_host.agent import guard
from kiki_agent_host.agent.guard import guarded
from kiki_agent_host.api.app import create_app
from kiki_agent_host.config import Settings
from kiki_agent_host.contracts import JobResult
from kiki_agent_host.executor import TransientError

ROWS = """
SELECT fingerprint, service, kind, job_kind, error_type, stage, http_status, model, tool,
       finish_reason, environment, run_id
FROM fault_events ORDER BY id
"""


async def rows(database_url):
    async with await AsyncConnection.connect(database_url) as connection:
        cursor = await connection.execute(ROWS)
        names = [column.name for column in cursor.description]
        return [dict(zip(names, row, strict=True)) for row in await cursor.fetchall()]


def test_fingerprints_match_the_ingester():
    """The same value as `faults::tests` in Rust: both services group by it."""
    assert (
        faults.fingerprint(
            "agent", "provider_rejected", "ModelHTTPError", "reply_agent.run", 400, None
        )
        == "2a7c3c50decec229"
    )
    assert (
        faults.fingerprint("ingester", "host_protocol", None, "host.response", None, None)
        == "b5e34f012e3eb402"
    )


def test_the_model_is_not_part_of_the_fingerprint():
    one = faults.Fault(faults.PROVIDER_REJECTED, "ModelHTTPError", "run", 400, model="a/one")
    other = faults.Fault(faults.PROVIDER_REJECTED, "ModelHTTPError", "run", 400, model="b/two")
    assert one.fingerprint() == other.fingerprint()


def test_classify_finds_a_rejection_in_the_chain():
    try:
        with faults.stage("reply_agent.run"):
            try:
                raise ModelHTTPError(400, "test/model", body={"error": "secret prompt"})
            except ModelHTTPError as e:
                raise RuntimeError("wrapped") from e
    except RuntimeError as error:
        fault = faults.classify(error)
    assert fault.kind == faults.PROVIDER_REJECTED
    assert (fault.error_type, fault.http_status, fault.model) == (
        "ModelHTTPError",
        400,
        "test/model",
    )
    assert fault.stage == "reply_agent.run"


def test_classify_tells_misbehavior_from_a_bug():
    error = UnexpectedModelBehavior("Exceeded maximum retries")
    faults.annotate(error, finish_reason="length", stage="reply_agent.run")
    fault = faults.classify(error)
    assert (fault.kind, fault.finish_reason, fault.stage) == (
        faults.MODEL_MISBEHAVED,
        "length",
        "reply_agent.run",
    )
    bug = faults.classify(KeyError("user text"))
    assert (bug.kind, bug.error_type, bug.stage) == (faults.EXECUTOR_FAILED, "KeyError", None)


def test_the_innermost_stage_wins():
    try:
        with faults.stage("draw"), faults.stage("draw.generate"):
            raise ValueError
    except ValueError as error:
        assert faults.classify(error).stage == "draw.generate"


def test_quiet_fallbacks_skip_what_is_expected():
    with faults.collecting() as noted:
        faults.degraded("embeddings", "search.embed", error=TimeoutError())
        faults.degraded("embeddings", "search.embed", error=httpx.ReadTimeout("slow"))
        faults.degraded("read_link.reddit", "reddit.fetch", http_status=429)
        faults.degraded("read_link.reddit", "reddit.fetch", http_status=404)
        faults.degraded("read_link.reddit", "reddit.fetch", http_status=503)
        faults.degraded("read_link.x", "x.fetch", error=httpx.ConnectError("refused"))
    assert [(f.kind, f.tool, f.http_status, f.error_type) for f in noted] == [
        (faults.TOOL_DEGRADED, "read_link.reddit", 503, None),
        (faults.TOOL_DEGRADED, "read_link.x", None, "ConnectError"),
    ]


def test_noting_outside_a_job_or_past_its_cap_is_dropped():
    faults.note(faults.TOOL_CRASHED, tool="search_messages")  # no job: only logged
    with faults.collecting() as noted:
        for _ in range(faults.MAX_PER_JOB + 5):
            faults.note(faults.TOOL_CRASHED, tool="search_messages")
    assert len(noted) == faults.MAX_PER_JOB


async def test_a_crashing_tool_tells_the_model_and_is_a_fault(recorded, caplog):
    async def work():
        raise KeyError("private message text")

    with faults.collecting() as noted:
        result = await guarded(tool_ctx(), "search_messages", work)
    assert result == {"error": guard.CRASHED}
    assert [(f.kind, f.error_type, f.tool) for f in noted] == [
        (faults.TOOL_CRASHED, "KeyError", "search_messages")
    ]
    assert "private message text" not in caplog.text
    tool = {"tool": "search_messages"}
    assert recorded.value("kiki.tools.calls", **tool, outcome="crashed") == 1


async def test_transient_tool_failures_still_fail_the_job():
    async def work():
        raise TransientError("database restarting")

    with faults.collecting() as noted:
        try:
            await guarded(tool_ctx(), "search_messages", work)
        except TransientError:
            pass
        else:
            raise AssertionError("expected the transient error to escape")
    assert noted == []


async def test_a_job_saves_its_faults_with_structured_fields_only(database_url):
    class DegradedExecutor(FakeExecutor):
        async def execute(self, request):
            self.calls += 1
            faults.degraded("embeddings", "search.embed", error=ConnectionError("secret"))
            return JobResult(text="answered lexically", model=request.model)

    async with client(create_app(Settings(database_url, TOKEN), DegradedExecutor())) as http:
        response = await http.post("/v1/jobs", json=job(), headers=HEADERS)
        assert response.json()["status"] == "completed"
    [row] = await rows(database_url)
    assert row == {
        "fingerprint": faults.fingerprint(
            "agent", faults.TOOL_DEGRADED, "ConnectionError", "search.embed", None, "embeddings"
        ),
        "service": "agent",
        "kind": faults.TOOL_DEGRADED,
        "job_kind": "reply",
        "error_type": "ConnectionError",
        "stage": "search.embed",
        "http_status": None,
        "model": "test/model",
        "tool": "embeddings",
        "finish_reason": None,
        "environment": "local",
        "run_id": "reply:42",
    }


async def test_a_failed_job_is_a_fault_without_its_message(database_url):
    async with client(create_app(Settings(database_url, TOKEN), FakeExecutor(fail=True))) as http:
        await http.post("/v1/jobs", json=job(), headers=HEADERS)
        await http.post("/v1/jobs", json=job(), headers=HEADERS)  # answered from the saved row
    [row] = await rows(database_url)
    assert (row["kind"], row["error_type"]) == (faults.EXECUTOR_FAILED, "RuntimeError")
    assert "secret" not in repr(row)


async def test_a_transient_failure_saves_no_fault(database_url):
    class FlakyExecutor(FakeExecutor):
        async def execute(self, request):
            faults.note(faults.TOOL_CRASHED, error_type="KeyError", tool="recall")
            raise TransientError("provider returned 503")

    async with client(create_app(Settings(database_url, TOKEN), FlakyExecutor())) as http:
        assert (await http.post("/v1/jobs", json=job(), headers=HEADERS)).status_code == 503
    assert await rows(database_url) == []


async def test_saving_never_fails_the_job(caplog):
    class Broken:
        def cursor(self):
            raise RuntimeError("connection gone")

    await faults.save(
        Broken(), [faults.Fault(faults.TOOL_CRASHED)], job_kind="reply", run_id="r", model=""
    )
    assert "couldn't record faults" in caplog.text
