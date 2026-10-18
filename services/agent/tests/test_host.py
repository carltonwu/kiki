import asyncio

import httpx
from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from kiki_agent_host.app import Settings, create_app
from kiki_agent_host.contracts import ImageJobRequest, ImageJobResult, JobRequest, JobResult
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


async def test_result_survives_new_app_and_conflicting_input_is_rejected(database_url):
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


async def test_timeout_releases_lock_and_same_job_can_be_recovered(database_url):
    class HangingExecutor:
        async def execute(self, request):
            await asyncio.Event().wait()

    settings = Settings(database_url, TOKEN, job_timeout_seconds=0.2)
    async with client(create_app(settings, HangingExecutor())) as http:
        assert (await http.post("/v1/jobs", json=job(), headers=HEADERS)).status_code == 503
    executor = FakeExecutor()
    async with client(create_app(Settings(database_url, TOKEN), executor)) as http:
        response = await http.post("/v1/jobs", json=job(), headers=HEADERS)
        assert response.json()["status"] == "completed"
        assert executor.calls == 1


async def test_capacity_is_bounded(database_url):
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
