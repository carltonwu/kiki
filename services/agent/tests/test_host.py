import asyncio

import httpx
from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from kiki_agent_host.api.app import application, create_app
from kiki_agent_host.config import Settings
from kiki_agent_host.contracts import ImageJobRequest, ImageJobResult, JobRequest, JobResult, Usage
from kiki_agent_host.executor import TransientError

TOKEN = "test-host-token"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


def job(job_id="reply:42"):
    return {
        "id": job_id,
        "kind": "reply",
        "scope": {"guild_id": "1", "channel_id": "2", "conversation_id": "3"},
        "model": "test/model",
        "messages": [{"role": "user", "content": "hello"}],
        "max_output_tokens": 100,
    }


def client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://host")


async def test_scaffold_health_auth_and_unconfigured_executor_do_not_connect_to_database():
    app = create_app(Settings("postgresql://unreachable.invalid/kiki", TOKEN))
    async with client(app) as http:
        assert (await http.get("/health")).json() == {"status": "ok", "executor_configured": False}
        assert (await http.post("/v1/jobs", json=job())).status_code == 401
        assert (await http.post("/v1/jobs", json=job(), headers=HEADERS)).status_code == 501


class FakeExecutor:
    def __init__(self, *, fail=False):
        self.calls = 0
        self.fail = fail

    async def execute(self, request: JobRequest) -> JobResult:
        self.calls += 1
        if self.fail:
            raise RuntimeError("secret provider detail")
        return JobResult(text="hello from the test executor", model=request.model)


async def test_result_survives_new_app_and_conflicting_input_is_rejected(database_url, recorded):
    executor = FakeExecutor()
    settings = Settings(database_url, TOKEN)
    async with client(create_app(settings, executor)) as http:
        first = await http.post("/v1/jobs", json=job(), headers=HEADERS)
        assert first.status_code == 200
        assert first.json()["result"]["text"] == "hello from the test executor"
    # A fresh application must return the persisted result without inference.
    async with client(create_app(settings, executor)) as http:
        second = await http.post("/v1/jobs", json=job(), headers=HEADERS)
        assert second.json() == first.json()
        assert executor.calls == 1
        changed = job()
        changed["messages"][0]["content"] = "different"
        assert (await http.post("/v1/jobs", json=changed, headers=HEADERS)).status_code == 409
    assert recorded.value("kiki.agent.jobs", kind="reply", outcome="completed") == 2
    assert recorded.value("kiki.agent.jobs.rejected", reason="conflict") == 1
    # Only the attempt that ran it: the second was answered from the saved result.
    assert recorded.value("kiki.agent.job.duration", kind="reply") == 1


async def test_requests_saved_before_image_model_existed_still_match(database_url):
    executor = FakeExecutor()
    async with await AsyncConnection.connect(database_url, autocommit=True) as db:
        # A completed job saved by a host version without `image_model`.
        await db.execute(
            "INSERT INTO agent_host_jobs (id, request, status, result) "
            "VALUES (%s, %s, 'completed', %s)",
            (
                "reply:42",
                Jsonb({**job(), "people": []}),
                Jsonb({"text": "saved", "model": None, "steps": 1, "usage": {}}),
            ),
        )
    async with client(create_app(Settings(database_url, TOKEN), executor)) as http:
        response = await http.post("/v1/jobs", json=job(), headers=HEADERS)
        assert response.status_code == 200
        assert response.json()["result"]["text"] == "saved"
        assert executor.calls == 0
        drawn = {**job("reply:43"), "image_model": "test/image"}
        assert (await http.post("/v1/jobs", json=drawn, headers=HEADERS)).status_code == 200
        assert (await http.post("/v1/jobs", json=drawn, headers=HEADERS)).status_code == 200
        assert executor.calls == 1


async def test_simultaneous_requests_execute_once_and_close_connections(database_url):
    entered, release = asyncio.Event(), asyncio.Event()

    class BlockingExecutor(FakeExecutor):
        async def execute(self, request):
            entered.set()
            await release.wait()
            return await super().execute(request)

    executor = BlockingExecutor()
    async with client(create_app(Settings(database_url, TOKEN), executor)) as http:
        first = asyncio.create_task(http.post("/v1/jobs", json=job(), headers=HEADERS))
        await asyncio.wait_for(entered.wait(), 5)
        try:
            duplicate = await http.post("/v1/jobs", json=job(), headers=HEADERS)
            assert duplicate.status_code == 202
            assert duplicate.json()["status"] == "running"
        finally:
            release.set()
        assert (await first).status_code == 200
        assert executor.calls == 1
    # All per-job connections must have closed, allowing immediate session locks.
    async with await AsyncConnection.connect(database_url, autocommit=True) as connection:
        cursor = await connection.execute(
            "SELECT pg_try_advisory_lock(hashtextextended(%s, 0))", (job()["id"],)
        )
        assert (await cursor.fetchone())[0]


async def test_failure_is_durable_and_does_not_expose_provider_details(database_url, caplog):
    executor = FakeExecutor(fail=True)
    async with client(create_app(Settings(database_url, TOKEN), executor)) as http:
        first = await http.post("/v1/jobs", json=job(), headers=HEADERS)
        second = await http.post("/v1/jobs", json=job(), headers=HEADERS)
        assert first.json() == second.json()
        assert first.json()["status"] == "failed"
        assert "secret" not in first.text
        assert executor.calls == 1
    assert "secret provider detail" not in caplog.text
    failures = [record for record in caplog.records if "executor failed" in record.message]
    assert len(failures) == 1
    assert "job_id=reply:42" in failures[0].message
    assert "error_type=RuntimeError" in failures[0].message
    assert failures[0].exc_info is None


async def test_image_jobs_are_durable_so_a_retry_does_not_draw_again(database_url):
    class Drawer(FakeExecutor):
        async def draw(self, request: ImageJobRequest) -> ImageJobResult:
            self.calls += 1
            return ImageJobResult(
                model=request.image_model, error="refused", failure_note="nope, sorry"
            )

    executor = Drawer()
    body = {
        "id": "image:42:1",
        "scope": {"guild_id": "1", "channel_id": "2", "conversation_id": "3"},
        "image_model": "test/image",
        "reply_job": "reply:42",
        "chat_model": "test/model",
        "ack": "brb",
        "drawing": {"prompt": "a frog", "subjects": [{"name": "Nougie", "photos": ["7"]}]},
    }
    async with client(create_app(Settings(database_url, TOKEN), executor)) as http:
        first = await http.post("/v1/images", json=body, headers=HEADERS)
        second = await http.post("/v1/images", json=body, headers=HEADERS)
        assert first.status_code == second.status_code == 200
        assert first.json() == second.json()
        assert first.json()["result"]["failure_note"] == "nope, sorry"
        assert executor.calls == 1
        changed = {**body, "ack": "different"}
        assert (await http.post("/v1/images", json=changed, headers=HEADERS)).status_code == 409


async def test_a_job_past_its_deadline_fails_for_good_and_releases_its_lock(database_url):
    """A timeout is the job's result: a retry of the same ID (Rust retries while its
    own deadline lasts) gets it instead of running the whole job again."""

    class HangingExecutor(FakeExecutor):
        async def execute(self, request):
            self.calls += 1
            await asyncio.Event().wait()

    hanging = HangingExecutor()
    settings = Settings(database_url, TOKEN, job_timeout_seconds=0.2)
    async with client(create_app(settings, hanging)) as http:
        first = await http.post("/v1/jobs", json=job(), headers=HEADERS)
        assert first.status_code == 200
        assert first.json()["status"] == "failed"
        assert first.json()["error"] == "timed out"
    executor = FakeExecutor()
    async with client(create_app(Settings(database_url, TOKEN), executor)) as http:
        again = await http.post("/v1/jobs", json=job(), headers=HEADERS)
        assert again.json()["error"] == "timed out"
    assert hanging.calls == 1 and executor.calls == 0
    async with await AsyncConnection.connect(database_url, autocommit=True) as connection:
        cursor = await connection.execute(
            "SELECT pg_try_advisory_lock(hashtextextended(%s, 0))", (job()["id"],)
        )
        assert (await cursor.fetchone())[0]


async def test_the_work_is_cancelled_at_its_deadline_and_the_timeout_saved_after(database_url):
    """The work stops 10 s before the job's deadline, leaving that for saving."""
    cancelled = asyncio.Event()

    class Slow(FakeExecutor):
        async def execute(self, request):
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cancelled.set()
                raise

    settings = Settings(database_url, TOKEN, job_timeout_seconds=10.5)
    started = asyncio.get_running_loop().time()
    async with client(create_app(settings, Slow())) as http:
        response = await http.post("/v1/jobs", json=job(), headers=HEADERS)
    assert response.json()["error"] == "timed out"
    assert cancelled.is_set()
    assert asyncio.get_running_loop().time() - started < 3  # at 0.5 s, not 10.5


async def test_a_step_timing_out_with_job_time_left_is_retried(database_url):
    """A model request cut at its own cap (waiting out a 429's Retry-After, say)
    isn't the job running out: the host answers 503 and the retry runs it again."""

    class StepTimesOut(FakeExecutor):
        async def execute(self, request):
            self.calls += 1
            if self.calls == 1:
                raise TimeoutError
            return JobResult(text="second time lucky", model=request.model)

    executor = StepTimesOut()
    async with client(create_app(Settings(database_url, TOKEN), executor)) as http:
        first = await http.post("/v1/jobs", json=job(), headers=HEADERS)
        assert first.status_code == 503
        second = await http.post("/v1/jobs", json=job(), headers=HEADERS)
        assert second.json()["result"]["text"] == "second time lucky"
    assert executor.calls == 2


async def test_a_resumed_job_gets_only_what_is_left_of_its_deadline(database_url):
    """A job interrupted mid-run (a deploy, a dropped connection) runs again on
    retry, but its deadline counts from its first attempt."""
    seen = []

    class Timed(FakeExecutor):
        async def execute(self, request):
            from kiki_agent_host.deadline import time_left

            seen.append(time_left())
            return await super().execute(request)

    async with await AsyncConnection.connect(database_url, autocommit=True) as connection:
        payload = JobRequest.model_validate(job()).model_dump(mode="json", exclude_none=True)
        await connection.execute(
            "INSERT INTO agent_host_jobs (id, request, status, created_at) "
            "VALUES (%s, %s, 'running', now() - interval '60 seconds')",
            ("reply:42", Jsonb(payload)),
        )
    executor = Timed()
    async with client(create_app(Settings(database_url, TOKEN), executor)) as http:
        response = await http.post("/v1/jobs", json=job(), headers=HEADERS)
        assert response.json()["status"] == "completed"
    # 120 s from 60 s ago, less the margin kept to save the result.
    [left] = seen
    assert 40 < left <= 50

    async with await AsyncConnection.connect(database_url, autocommit=True) as connection:
        await connection.execute(
            "INSERT INTO agent_host_jobs (id, request, status, created_at) "
            "VALUES (%s, %s, 'running', now() - interval '10 minutes')",
            ("reply:43", Jsonb({**payload, "id": "reply:43"})),
        )
    async with client(create_app(Settings(database_url, TOKEN), executor)) as http:
        late = await http.post("/v1/jobs", json=job("reply:43"), headers=HEADERS)
        assert late.json()["error"] == "timed out"
    assert executor.calls == 1


async def test_capacity_is_bounded(database_url, recorded):
    entered, release = asyncio.Event(), asyncio.Event()

    class BlockingExecutor(FakeExecutor):
        async def execute(self, request):
            entered.set()
            await release.wait()
            return await super().execute(request)

    settings = Settings(database_url, TOKEN, max_concurrent_jobs=1)
    async with client(create_app(settings, BlockingExecutor())) as http:
        first = asyncio.create_task(http.post("/v1/jobs", json=job(), headers=HEADERS))
        await asyncio.wait_for(entered.wait(), 5)
        try:
            response = await http.post("/v1/jobs", json=job("reply:43"), headers=HEADERS)
            assert response.status_code == 503
            assert recorded.value("kiki.agent.jobs.rejected", reason="busy") == 1
        finally:
            release.set()
        await first


async def test_transient_failure_is_not_persisted_and_retry_runs_again(database_url):
    class FlakyExecutor(FakeExecutor):
        async def execute(self, request):
            self.calls += 1
            if self.calls == 1:
                raise TransientError("provider returned 503")
            return JobResult(text="recovered", model=request.model)

    executor = FlakyExecutor()
    async with client(create_app(Settings(database_url, TOKEN), executor)) as http:
        assert (await http.post("/v1/jobs", json=job(), headers=HEADERS)).status_code == 503
        response = await http.post("/v1/jobs", json=job(), headers=HEADERS)
        assert response.json()["result"]["text"] == "recovered"


async def test_the_factory_configures_an_executor_only_with_an_openrouter_key(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://unreachable.invalid/kiki")
    monkeypatch.setenv("AGENT_HOST_TOKEN", TOKEN)
    monkeypatch.delenv("OPENROUTER_CHAT_API_KEY", raising=False)
    async with client(application()) as http:
        assert (await http.get("/health")).json()["executor_configured"] is False
    monkeypatch.setenv("OPENROUTER_CHAT_API_KEY", "test-key")
    async with client(application()) as http:
        assert (await http.get("/health")).json()["executor_configured"] is True


async def test_a_completed_topic_job_records_its_cost_once(database_url):
    """topic_summaries (an ingester migration's trigger) records the cost where the
    host saves the result, whichever ingester asked for it; a retry answered from
    the saved result adds nothing."""

    class TopicExecutor(FakeExecutor):
        async def execute(self, request):
            self.calls += 1
            return JobResult(text="weekend plans", model=request.model, usage=Usage(cost=0.25))

    async with await AsyncConnection.connect(database_url, autocommit=True) as db:
        await db.execute(
            "INSERT INTO conversations (id, guild_id, channel_id, started_by) VALUES (3, 1, 2, 9)"
        )
    topic = {**job("topic:3:100:12"), "kind": "topic"}
    executor = TopicExecutor()
    async with client(create_app(Settings(database_url, TOKEN), executor)) as http:
        for _ in range(2):
            response = await http.post("/v1/jobs", json=topic, headers=HEADERS)
            assert response.json()["status"] == "completed"
        assert (await http.post("/v1/jobs", json=job(), headers=HEADERS)).status_code == 200
    assert executor.calls == 2  # the topic once, the reply once
    async with await AsyncConnection.connect(database_url, autocommit=True) as db:
        cursor = await db.execute("SELECT job_id, conversation_id, cost_usd FROM topic_summaries")
        assert await cursor.fetchall() == [("topic:3:100:12", 3, 0.25)]


async def test_a_job_that_keeps_timing_out_retries_until_it_is_saved_as_timed_out(
    database_url, monkeypatch
):
    """Review round 2: the whole chain. Each attempt's step times out with job
    time left (503, Rust retries the same ID); each retry gets only what's left
    of the job; once that's under MIN_RESUME_SECONDS the timeout is saved, and
    later retries get it without running anything."""
    from kiki_agent_host.api import jobs

    monkeypatch.setattr(jobs, "JOB_MARGIN_SECONDS", 0.1)
    monkeypatch.setattr(jobs, "MIN_RESUME_SECONDS", 0.5)

    class StepAlwaysTimesOut(FakeExecutor):
        async def execute(self, request):
            self.calls += 1
            await asyncio.sleep(0.4)
            raise TimeoutError

    executor = StepAlwaysTimesOut()
    statuses = []
    async with client(
        create_app(Settings(database_url, TOKEN, job_timeout_seconds=1.6), executor)
    ) as http:
        for _ in range(10):  # Rust's loop, without its back-off
            response = await http.post("/v1/jobs", json=job(), headers=HEADERS)
            statuses.append(response.status_code)
            if response.status_code != 503:
                break
        assert response.json()["error"] == "timed out"
        again = await http.post("/v1/jobs", json=job(), headers=HEADERS)
        assert again.json()["error"] == "timed out"
    assert statuses[:-1] == [503] * (len(statuses) - 1) and statuses[-1] == 200
    assert 2 <= len(statuses) <= 4, statuses
    assert executor.calls == len(statuses)  # the saved timeout ran nothing
    async with await AsyncConnection.connect(database_url, autocommit=True) as connection:
        cursor = await connection.execute(
            "SELECT status, error FROM agent_host_jobs WHERE id = %s", (job()["id"],)
        )
        assert await cursor.fetchone() == ("failed", "timed out")
