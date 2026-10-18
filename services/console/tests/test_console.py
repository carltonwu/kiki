"""End to end through the ASGI app, with a migrated schema and a fake Discord
and ingester behind httpx.MockTransport."""

import json
import re
from datetime import timedelta

import httpx
import pytest
from psycopg import AsyncConnection

from kiki_console import auth
from kiki_console.app import create_app
from kiki_console.config import Settings

GUILD_A, GUILD_B = 100, 200
ADMIN, MEMBER, OPERATOR = 11, 12, 13
TOKEN = "t" * 40


class Fakes:
    """Discord OAuth/user endpoints and the ingester admin API."""

    def __init__(self):
        self.users = {
            "code-admin": {"id": str(ADMIN), "username": "alex", "global_name": "Alex"},
            "code-member": {"id": str(MEMBER), "username": "sam"},
            "code-operator": {"id": str(OPERATOR), "username": "carlton"},
        }
        # Admin manages A (Manage Server) and an unrelated guild; only reads B.
        self.guilds = {
            str(ADMIN): [
                {"id": str(GUILD_A), "permissions": str(auth.MANAGE_GUILD)},
                {"id": str(GUILD_B), "permissions": "1024"},
                {"id": "999", "permissions": str(auth.ADMINISTRATOR)},
            ],
            str(MEMBER): [{"id": str(GUILD_A), "permissions": "1024"}],
            str(OPERATOR): [],
        }
        self.revoked: set[str] = set()
        self.ingester_calls: list[tuple[str, str, dict | None]] = []
        self.settings = {"pool_max_messages": {"value": "10", "overridden": False}}

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.startswith("https://discord.test/oauth2/token"):
            code = dict(httpx.QueryParams(request.content.decode()))["code"]
            if code not in self.users:
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(200, json={"access_token": f"at-{code}"})
        if url.startswith("https://discord.test/users/@me"):
            token = request.headers["authorization"].removeprefix("Bearer at-")
            if token in self.revoked:
                return httpx.Response(401, json={})
            user = self.users[token]
            if url.endswith("/guilds"):
                return httpx.Response(200, json=self.guilds[user["id"]])
            return httpx.Response(200, json=user)
        if url.startswith("http://ingester.test/admin/v1/"):
            assert request.headers["authorization"] == f"Bearer {TOKEN}"
            path = url.removeprefix("http://ingester.test/admin/v1")
            body = json.loads(request.content) if request.content else None
            self.ingester_calls.append((request.method, path, body))
            if path.endswith("/settings") and request.method == "GET":
                return httpx.Response(200, json=self.settings_body())
            if "/settings/" in path:
                if body["value"] == "0":
                    return httpx.Response(422, json={"error": "pool_max_messages must be 1–50"})
                key = path.rsplit("/", 1)[1]
                value = body["value"] or "10"
                self.settings[key] = {"value": value, "overridden": body["value"] is not None}
                return httpx.Response(200, json=self.settings_body())
            if path.split("?")[0].endswith("/prompt"):
                return httpx.Response(
                    200,
                    json={
                        "persona_default": "Where you are: default persona.",
                        "guidance_default": "Default guidance.",
                        "facts_budget_chars": 12000,
                        "system": "SYSTEM PROMPT",
                    },
                )
            if path.endswith("/chat"):
                last = body["messages"][-1]["content"]
                if last == "fail please":
                    return httpx.Response(502, json={"error": "agent host job failed"})
                return httpx.Response(
                    200,
                    json={
                        "text": f"echo: {last}",
                        "model": "sim/chat",
                        "steps": 1,
                        "prompt_tokens": 10,
                        "completion_tokens": 3,
                        "cost_usd": 0.0012,
                        "latency_ms": 42,
                    },
                )
        return httpx.Response(404)

    def settings_body(self) -> dict:
        return {
            "settings": [
                {
                    "key": key,
                    "description": "Messages before routing a pool",
                    "bounds": "1–50",
                    "kind": "integer",
                    "default": "10",
                    "ignored": False,
                    **value,
                }
                for key, value in self.settings.items()
            ],
            "global": [{"name": "Decide model", "value": "sim/jev"}],
        }


@pytest.fixture
async def env(database_url):
    async with await AsyncConnection.connect(database_url, autocommit=True) as db:
        await db.execute(
            "INSERT INTO guilds (id, name) VALUES (%s, 'Alpha'), (%s, 'Beta')", (GUILD_A, GUILD_B)
        )
        await db.execute(
            "INSERT INTO channels (id, guild_id, name, type) VALUES (1, %s, 'general', 0), "
            "(2, %s, 'secret', 0)",
            (GUILD_A, GUILD_B),
        )
        fakes = Fakes()
        settings = Settings(
            database_url=database_url,
            console_url="http://localhost:8095",
            discord_client_id="cid",
            discord_client_secret="csecret",
            ingester_admin_url="http://ingester.test",
            admin_api_token=TOKEN,
            operator_ids=frozenset({OPERATOR}),
            discord_api="https://discord.test",
            discord_authorize="https://discord.test/oauth2/authorize",
        )
        mock = httpx.AsyncClient(transport=httpx.MockTransport(fakes.handler))
        app = create_app(settings, mock)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
            yield client, db, fakes


async def login(client: httpx.AsyncClient, code: str) -> httpx.Response:
    start = await client.get("/login/discord")
    assert start.status_code == 303
    location = httpx.URL(start.headers["location"])
    assert location.params["scope"] == "identify guilds"
    assert location.params["redirect_uri"] == "http://localhost:8095/auth/callback"
    state = location.params["state"]
    return await client.get("/auth/callback", params={"code": code, "state": state})


def csrf(page: httpx.Response) -> str:
    return re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)


async def test_login_requires_state_and_manage_server(env):
    client, db, _ = env
    assert (await client.get("/")).headers["location"] == "/login"
    # A forged callback without the state cookie.
    forged = await client.get("/auth/callback", params={"code": "code-admin", "state": "x"})
    assert forged.status_code == 400
    await client.get("/login/discord")
    wrong = await client.get("/auth/callback", params={"code": "code-admin", "state": "nope"})
    assert wrong.status_code == 400

    refused = await login(client, "code-member")
    assert refused.status_code == 403
    assert "Manage Server" in refused.text
    assert "kiki_session" not in refused.headers.get("set-cookie", "")

    ok = await login(client, "code-admin")
    assert ok.status_code == 303
    cookie = ok.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie
    cursor = await db.execute("SELECT user_id, guild_ids, operator FROM console_sessions")
    # Only servers Kiki is in, and only those with Manage Server.
    assert await cursor.fetchall() == [(ADMIN, [GUILD_A], False)]
    # The cookie value is never stored.
    token = client.cookies["kiki_session"]
    cursor = await db.execute(
        "SELECT count(*) FROM console_sessions WHERE token_hash = %s", (token.encode(),)
    )
    assert (await cursor.fetchone())[0] == 0


async def test_guild_isolation_csrf_and_headers(env):
    client, db, _ = env
    await login(client, "code-admin")
    home = await client.get("/")
    assert home.status_code == 200
    assert "Alpha" in home.text and "Beta" not in home.text
    assert "script-src 'none'" in home.headers["content-security-policy"]
    assert home.headers["x-frame-options"] == "DENY"

    for path in (
        "/g/200",
        "/g/200/runs",
        "/g/200/settings",
        "/g/200/facts",
        "/g/200/chat",
        "/g/200/runs/1",
        "/audit",
        "/g/999",
    ):
        assert (await client.get(path)).status_code == 404, path
    page = await client.get("/g/100/facts")
    token = csrf(page)
    denied = await client.post(
        "/g/200/facts", data={"csrf": token, "action": "add", "title": "x", "body": "y"}
    )
    assert denied.status_code == 404
    no_csrf = await client.post("/g/100/facts", data={"action": "add", "title": "x", "body": "y"})
    assert no_csrf.status_code == 403
    bad_csrf = await client.post(
        "/g/100/facts", data={"csrf": "wrong", "action": "add", "title": "x", "body": "y"}
    )
    assert bad_csrf.status_code == 403
    cursor = await db.execute("SELECT count(*) FROM guild_facts")
    assert (await cursor.fetchone())[0] == 0


async def test_permission_recheck_and_revocation(env):
    client, db, fakes = env
    await login(client, "code-admin")
    assert (await client.get("/g/100")).status_code == 200
    # Manage Server removed in Discord: effective after the re-check interval.
    fakes.guilds[str(ADMIN)] = [{"id": str(GUILD_A), "permissions": "0"}]
    assert (await client.get("/g/100")).status_code == 200
    await db.execute(
        "UPDATE console_sessions SET checked_at = now() - %s",
        (auth.RECHECK + timedelta(seconds=1),),
    )
    assert (await client.get("/g/100")).headers["location"] == "/login"
    cursor = await db.execute("SELECT count(*) FROM console_sessions")
    assert (await cursor.fetchone())[0] == 0

    # A revoked access token ends the session too.
    fakes.guilds[str(ADMIN)] = [{"id": str(GUILD_A), "permissions": "32"}]
    await login(client, "code-admin")
    fakes.revoked.add("code-admin")
    await db.execute(
        "UPDATE console_sessions SET checked_at = now() - %s",
        (auth.RECHECK + timedelta(seconds=1),),
    )
    assert (await client.get("/g/100")).status_code == 303
    # Expired sessions don't load.
    fakes.revoked.clear()
    await login(client, "code-admin")
    await db.execute("UPDATE console_sessions SET expires_at = now() - interval '1 second'")
    assert (await client.get("/")).status_code == 303


async def test_logout_deletes_the_session(env):
    client, db, _ = env
    await login(client, "code-admin")
    token = csrf(await client.get("/"))
    assert (await client.post("/logout", data={"csrf": token})).status_code == 303
    cursor = await db.execute("SELECT count(*) FROM console_sessions")
    assert (await cursor.fetchone())[0] == 0


async def test_operator_sees_every_server_and_the_audit_log(env):
    client, db, _ = env
    await db.execute(
        "INSERT INTO conversations (id, guild_id, channel_id, started_by) VALUES "
        "(1, %s, 1, 5), (2, %s, 2, 5)",
        (GUILD_A, GUILD_B),
    )
    await db.execute(
        "INSERT INTO agent_runs (conversation_id, trigger_message_ids, status, cost_usd, "
        "latency_ms, finished_at, error) VALUES (1, '{}', 'done', 0.5, 1000, now(), NULL), "
        "(2, '{}', 'failed', 0.25, NULL, now(), 'agent host job failed: boom')"
    )
    await login(client, "code-operator")
    home = await client.get("/")
    assert "Alpha" in home.text and "Beta" in home.text
    assert "$0.7500" in home.text  # 24 h total across servers
    assert "boom" in home.text  # recent failures
    assert (await client.get("/g/200")).status_code == 200
    assert (await client.get("/audit")).status_code == 200


async def test_dashboard_runs_and_run_detail(env):
    client, db, _ = env
    await db.execute(
        "INSERT INTO messages (id, guild_id, channel_id, author_id, author_name, content, "
        "created_at, raw) VALUES (501, %s, 1, 21, 'sam', 'kiki <b>hi</b>', now(), '{}'), "
        "(502, %s, 1, 1, 'Kiki', 'hey sam', now(), '{}')",
        (GUILD_A, GUILD_A),
    )
    await db.execute(
        "INSERT INTO people (guild_id, user_id, username, seen_at) VALUES (%s, 21, 'sam', now())",
        (GUILD_A,),
    )
    await db.execute(
        "INSERT INTO conversations (id, guild_id, channel_id, started_by, topic) "
        "VALUES (7, %s, 1, 21, 'greetings')",
        (GUILD_A,),
    )
    await db.execute(
        "INSERT INTO classifications (message_id, pool_id, check_hit, outcome, jev_cost_usd, "
        "conversation_id) VALUES (501, 501, 'keyword', 'new', 0.01, 7)"
    )
    await db.execute(
        "INSERT INTO agent_runs (id, conversation_id, trigger_message_ids, status, cost_usd, "
        "latency_ms, model, reply_message_ids, finished_at, host_request) VALUES "
        "(9, 7, '{501}', 'done', 0.02, 1500, 'sim/chat', '{502}', now(), %s)",
        (json.dumps({"messages": [{"role": "system", "content": "You are Kiki <system>"}]}),),
    )
    await login(client, "code-admin")
    dashboard = await client.get("/g/100")
    assert dashboard.status_code == 200
    assert "$0.0300" in dashboard.text  # chat + Decide, 24 h
    assert "keyword" in dashboard.text and "greetings" in dashboard.text
    assert "<svg" in dashboard.text and "sam" in dashboard.text
    runs = await client.get("/g/100/runs", params={"status": "done"})
    assert 'href="/g/100/runs/9"' in runs.text
    detail = await client.get("/g/100/runs/9")
    assert detail.status_code == 200
    # Message content and prompts are escaped.
    assert "kiki &lt;b&gt;hi&lt;/b&gt;" in detail.text
    assert "You are Kiki &lt;system&gt;" in detail.text
    assert "hey sam" in detail.text


async def test_settings_go_through_the_ingester_and_are_audited(env):
    client, db, fakes = env
    await login(client, "code-admin")
    page = await client.get("/g/100/settings")
    assert "pool_max_messages" in page.text and "sim/jev" in page.text
    token = csrf(page)
    saved = await client.post(
        "/g/100/settings", data={"csrf": token, "key": "pool_max_messages", "value": "3"}
    )
    assert saved.status_code == 200 and "pool_max_messages = 3" in saved.text
    assert (
        "PUT",
        "/guilds/100/settings/pool_max_messages",
        {"value": "3", "actor": str(ADMIN)},
    ) in fakes.ingester_calls
    rejected = await client.post(
        "/g/100/settings", data={"csrf": token, "key": "pool_max_messages", "value": "0"}
    )
    assert rejected.status_code == 422 and "1–50" in rejected.text
    bad_key = await client.post(
        "/g/100/settings", data={"csrf": token, "key": "../chat", "value": "x"}
    )
    assert bad_key.status_code == 422
    reset = await client.post(
        "/g/100/settings", data={"csrf": token, "key": "pool_max_messages", "reset": "1"}
    )
    assert reset.status_code == 200
    # The change, then the refreshed page.
    assert fakes.ingester_calls[-2][2] == {"value": None, "actor": str(ADMIN)}
    cursor = await db.execute(
        "SELECT action, detail->>'old', detail->>'new' FROM console_audit ORDER BY id"
    )
    assert await cursor.fetchall() == [("setting.set", "10", "3"), ("setting.reset", "3", "10")]


async def test_prompts_facts_and_lingo(env):
    client, db, _ = env
    await login(client, "code-admin")
    page = await client.get("/g/100/prompts")
    assert "Where you are: default persona." in page.text and "SYSTEM PROMPT" in page.text
    token = csrf(page)
    saved = await client.post(
        "/g/100/prompts", data={"csrf": token, "part": "persona", "body": "  A theatre crew.  "}
    )
    assert saved.status_code == 200
    assert (
        await client.post("/g/100/prompts", data={"csrf": token, "part": "persona", "body": "   "})
    ).status_code == 422
    assert (
        await client.post("/g/100/prompts", data={"csrf": token, "part": "tools", "body": "x"})
    ).status_code == 422
    cursor = await db.execute("SELECT guild_id, part, body, updated_by FROM guild_prompts")
    assert await cursor.fetchall() == [(GUILD_A, "persona", "A theatre crew.", ADMIN)]
    await client.post("/g/100/prompts", data={"csrf": token, "part": "persona", "reset": "1"})
    cursor = await db.execute("SELECT count(*) FROM guild_prompts")
    assert (await cursor.fetchone())[0] == 0

    added = await client.post(
        "/g/100/facts",
        data={"csrf": token, "action": "add", "title": "Show", "body": "Opens Nov 3"},
    )
    assert added.status_code == 200 and "Opens Nov 3" in added.text
    cursor = await db.execute("SELECT id FROM guild_facts")
    fact_id = (await cursor.fetchone())[0]
    await client.post(
        "/g/100/facts",
        data={
            "csrf": token,
            "action": "edit",
            "fact_id": fact_id,
            "title": "Show",
            "body": "Opens Nov 4",
        },
    )
    cursor = await db.execute("SELECT body, enabled FROM guild_facts")
    assert await cursor.fetchone() == ("Opens Nov 4", False)
    too_long = await client.post(
        "/g/100/facts", data={"csrf": token, "action": "add", "title": "x", "body": "y" * 2001}
    )
    assert too_long.status_code == 422
    # Another server's fact can't be edited through this one.
    await db.execute(
        "INSERT INTO guild_facts (id, guild_id, title, body, updated_by) "
        "VALUES (900, %s, 't', 'b', 1)",
        (GUILD_B,),
    )
    other = await client.post(
        "/g/100/facts", data={"csrf": token, "action": "delete", "fact_id": 900}
    )
    assert other.status_code == 422
    await client.post("/g/100/facts", data={"csrf": token, "action": "delete", "fact_id": fact_id})

    await client.post(
        "/g/100/facts",
        data={"csrf": token, "action": "lingo_set", "phrase": "bby", "meaning": "pet name"},
    )
    cursor = await db.execute(
        "SELECT guild_id, phrase FROM guild_lingo WHERE guild_id = %s", (GUILD_A,)
    )
    assert await cursor.fetchall() == [(GUILD_A, "bby")]
    await client.post(
        "/g/100/facts", data={"csrf": token, "action": "lingo_delete", "phrase": "bby"}
    )
    cursor = await db.execute("SELECT action FROM console_audit ORDER BY id")
    assert [r[0] for r in await cursor.fetchall()] == [
        "prompt.set",
        "prompt.reset",
        "fact.add",
        "fact.edit",
        "fact.delete",
        "lingo.set",
        "lingo.delete",
    ]
    cursor = await db.execute("SELECT count(*) FROM guild_facts WHERE guild_id = %s", (GUILD_B,))
    assert (await cursor.fetchone())[0] == 1


async def test_chat_threads_costs_and_failures(env):
    client, db, fakes = env
    await login(client, "code-admin")
    page = await client.get("/g/100/chat")
    token = csrf(page)
    thread = re.search(r'name="thread" value="([^"]+)"', page.text).group(1)
    sent = await client.post(
        "/g/100/chat", data={"csrf": token, "thread": thread, "message": "hi kiki"}
    )
    assert sent.status_code == 303
    failed = await client.post(
        "/g/100/chat", data={"csrf": token, "thread": thread, "message": "fail please"}
    )
    assert failed.status_code == 422 and "agent host job failed" in failed.text
    await client.post("/g/100/chat", data={"csrf": token, "thread": thread, "message": "again"})
    _method, path, body = fakes.ingester_calls[-1]
    assert path == "/guilds/100/chat" and body["actor"] == str(ADMIN)
    assert body["job_id"].startswith("console-")
    # The failed message isn't sent again; turns alternate and end with the user.
    assert [m["content"] for m in body["messages"]] == ["hi kiki", "echo: hi kiki", "again"]
    view = await client.get("/g/100/chat", params={"thread": thread})
    assert "echo: again" in view.text and "Not answered" in view.text
    # Console spend shows up on the dashboard.
    dashboard = await client.get("/g/100")
    assert "$0.0024" in dashboard.text

    # Someone else's thread can't be continued.
    await db.execute("UPDATE console_chat_messages SET user_id = 4242")
    taken = await client.post(
        "/g/100/chat", data={"csrf": token, "thread": thread, "message": "mine?"}
    )
    assert taken.status_code == 422
