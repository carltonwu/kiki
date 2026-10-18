"""Kiki's web console (docs/console-hld.md): per-server dashboards and costs, a
master view for operators, settings/prompt/fact editing and chat with Kiki.

Server-rendered, no client-side scripts. Every page under /g/{guild_id} checks
the session's allowed servers; every POST checks the session's CSRF token.
"""

import logging
import re
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated
from uuid import UUID, uuid4

import httpx
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from psycopg import AsyncConnection
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from . import auth, chat, content, queries
from .auth import Discord, DiscordAuthError, Session
from .charts import SERIES, daily_cost_svg, usd
from .config import Settings
from .ingester import Ingester, IngesterError

logger = logging.getLogger(__name__)
HERE = Path(__file__).parent
SETTING_KEY = re.compile(r"^[a-z_]{1,64}$")

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'none'; "
        "form-action 'self' https://discord.com; frame-ancestors 'none'; base-uri 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "X-Frame-Options": "DENY",
    "Cache-Control": "no-store",
}


class LoginRequired(Exception):
    pass


def create_app(settings: Settings, http: httpx.AsyncClient | None = None) -> FastAPI:
    client = http or httpx.AsyncClient(follow_redirects=False)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        if http is None:
            await client.aclose()

    app = FastAPI(
        title="Kiki console", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan
    )
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.globals["usd"] = usd
    templates.env.filters["ms"] = lambda v: "–" if v is None else f"{v / 1000:.1f} s"
    templates.env.filters["when"] = lambda v: v.strftime("%Y-%m-%d %H:%M UTC") if v else "–"
    discord = Discord(settings, client)
    ingester = Ingester(settings, client)

    @app.middleware("http")
    async def headers(request: Request, call_next):
        response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        return response

    @app.exception_handler(LoginRequired)
    async def login_required(request: Request, _: LoginRequired):
        return RedirectResponse("/login", status_code=303)

    # Daily buckets and "today" are UTC whatever the database's default.
    options = conninfo_to_dict(settings.database_url).get("options") or ""
    conninfo = make_conninfo(settings.database_url, options=f"{options} -c TimeZone=UTC".strip())

    async def database() -> AsyncIterator[AsyncConnection]:
        async with await AsyncConnection.connect(
            conninfo, autocommit=True, connect_timeout=10
        ) as connection:
            yield connection

    Db = Annotated[AsyncConnection, Depends(database)]

    async def current(request: Request, db: Db) -> Session:
        session = await auth.load_session(db, discord, request.cookies.get(auth.SESSION_COOKIE))
        if session is None:
            raise LoginRequired
        return session

    User = Annotated[Session, Depends(current)]

    def guild(guild_id: int, session: User) -> int:
        # Not 403: don't reveal which servers exist.
        if not session.can_manage(guild_id):
            raise HTTPException(status_code=404)
        return guild_id

    Guild = Annotated[int, Depends(guild)]

    def csrf(session: User, token: Annotated[str, Form(alias="csrf")] = "") -> None:
        if not auth.same(token, session.csrf):
            raise HTTPException(status_code=403, detail="invalid form token; reload the page")

    Csrf = Depends(csrf)

    async def page(
        request: Request,
        name: str,
        session: Session,
        db: AsyncConnection,
        status_code: int = 200,
        guild_id: int | None = None,
        **context,
    ) -> HTMLResponse:
        names = await queries.guild_names(db, session.guild_ids)
        return templates.TemplateResponse(
            request,
            name,
            {
                "session": session,
                "guilds": names,
                "guild_id": guild_id,
                "guild_name": names.get(guild_id) if guild_id else None,
                **context,
            },
            status_code=status_code,
        )

    # --- auth --------------------------------------------------------------

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    @app.get("/login", response_class=HTMLResponse)
    async def login(request: Request):
        return templates.TemplateResponse(request, "login.html", {"session": None})

    @app.get("/login/discord")
    async def login_discord():
        state = secrets.token_urlsafe(32)
        response = RedirectResponse(discord.authorize_url(state), status_code=303)
        response.set_cookie(
            auth.STATE_COOKIE,
            state,
            max_age=600,
            httponly=True,
            secure=settings.secure_cookies,
            samesite="lax",
            path="/auth",
        )
        return response

    @app.get("/auth/callback")
    async def callback(request: Request, db: Db, code: str = "", state: str = ""):
        expected = request.cookies.get(auth.STATE_COOKIE, "")
        if not code or not expected or not auth.same(state, expected):
            return templates.TemplateResponse(
                request,
                "login.html",
                {"session": None, "error": "That login link expired. Try again."},
                status_code=400,
            )
        try:
            access_token = await discord.exchange(code)
            token = await auth.create_session(db, settings, discord, access_token)
        except (DiscordAuthError, httpx.HTTPError) as error:
            logger.warning("console login failed error_type=%s", type(error).__name__)
            token = None
            message = "Discord didn't accept the login. Try again."
        else:
            message = "You don't manage any server Kiki is in (Manage Server is required)."
        if token is None:
            response = templates.TemplateResponse(
                request, "login.html", {"session": None, "error": message}, status_code=403
            )
        else:
            response = RedirectResponse("/", status_code=303)
            response.set_cookie(
                auth.SESSION_COOKIE,
                token,
                max_age=int(auth.SESSION_TTL.total_seconds()),
                httponly=True,
                secure=settings.secure_cookies,
                samesite="lax",
            )
        response.delete_cookie(auth.STATE_COOKIE, path="/auth")
        return response

    @app.post("/logout", dependencies=[Csrf])
    async def logout(request: Request, db: Db):
        await auth.delete_session(db, request.cookies.get(auth.SESSION_COOKIE, ""))
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(auth.SESSION_COOKIE)
        return response

    # --- overview ------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request, session: User, db: Db):
        servers = await queries.master(db, session.guild_ids)
        context = {"servers": servers}
        if session.operator:
            days = await queries.daily_costs(db, None)
            context |= {
                "chart": daily_cost_svg(days),
                "series": SERIES,
                "days": days,
                "failures": await queries.recent_failures(db, session.guild_ids),
                "totals": {
                    k: sum(s[k] for s in servers)
                    for k in ("cost_24h", "cost_7d", "cost_30d", "replies_7d", "failed_7d")
                },
            }
        return await page(request, "index.html", session, db, **context)

    @app.get("/audit", response_class=HTMLResponse)
    async def audit_all(request: Request, session: User, db: Db):
        if not session.operator:
            raise HTTPException(status_code=404)
        rows = await queries.audit(db, None)
        return await page(request, "audit.html", session, db, rows=rows)

    # --- one server ----------------------------------------------------------

    @app.get("/g/{guild_id}", response_class=HTMLResponse)
    async def dashboard(request: Request, session: User, db: Db, guild_id: Guild):
        days = await queries.daily_costs(db, guild_id)
        return await page(
            request,
            "guild.html",
            session,
            db,
            guild_id=guild_id,
            windows=await queries.windows(db, guild_id),
            chart=daily_cost_svg(days),
            series=SERIES,
            days=days,
            routing=await queries.routing(db, guild_id),
            conversations=await queries.active_conversations(db, guild_id),
            people=await queries.top_people(db, guild_id),
        )

    @app.get("/g/{guild_id}/runs", response_class=HTMLResponse)
    async def runs(
        request: Request,
        session: User,
        db: Db,
        guild_id: Guild,
        status: str = "",
        before: int | None = None,
    ):
        status = status if status in queries.RUN_STATUSES else ""
        found = await queries.runs(db, guild_id, status or None, before)
        return await page(
            request,
            "runs.html",
            session,
            db,
            guild_id=guild_id,
            runs=found,
            status=status,
            statuses=queries.RUN_STATUSES,
            next_before=found[-1]["id"] if len(found) == 50 else None,
        )

    @app.get("/g/{guild_id}/runs/{run_id}", response_class=HTMLResponse)
    async def run(request: Request, session: User, db: Db, guild_id: Guild, run_id: int):
        found = await queries.run_detail(db, guild_id, run_id)
        if found is None:
            raise HTTPException(status_code=404)
        return await page(request, "run.html", session, db, guild_id=guild_id, run=found)

    async def settings_page(request, session, db, guild_id, error=None, notice=None, status=200):
        try:
            data = await ingester.settings(guild_id)
        except IngesterError as e:
            data, error = None, error or str(e)
        return await page(
            request,
            "settings.html",
            session,
            db,
            status_code=status,
            guild_id=guild_id,
            data=data,
            error=error,
            notice=notice,
        )

    @app.get("/g/{guild_id}/settings", response_class=HTMLResponse)
    async def settings_view(request: Request, session: User, db: Db, guild_id: Guild):
        return await settings_page(request, session, db, guild_id)

    @app.post("/g/{guild_id}/settings", response_class=HTMLResponse, dependencies=[Csrf])
    async def settings_change(
        request: Request,
        session: User,
        db: Db,
        guild_id: Guild,
        key: Annotated[str, Form()],
        value: Annotated[str, Form()] = "",
        reset: Annotated[str, Form()] = "",
    ):
        if not SETTING_KEY.match(key):
            return await settings_page(
                request, session, db, guild_id, "Unknown setting.", None, 422
            )
        try:
            before = await ingester.settings(guild_id)
            old = next((s for s in before["settings"] if s["key"] == key), None)
            new = await ingester.change(guild_id, key, None if reset else value, session.user_id)
            current = next((s for s in new["settings"] if s["key"] == key), {})
        except IngesterError as e:
            return await settings_page(request, session, db, guild_id, str(e), None, 422)
        await content.audit(
            db,
            session.user_id,
            guild_id,
            "setting.reset" if reset else "setting.set",
            {
                "key": key,
                "old": old and old["value"],
                "old_overridden": old and old["overridden"],
                "new": current.get("value"),
            },
        )
        return await settings_page(
            request, session, db, guild_id, notice=f"{key} = {current.get('value')}"
        )

    async def prompts_page(request, session, db, guild_id, error=None, notice=None, status=200):
        try:
            defaults = await ingester.prompt(guild_id, session.user_name)
        except IngesterError as e:
            defaults, error = None, error or str(e)
        return await page(
            request,
            "prompts.html",
            session,
            db,
            status_code=status,
            guild_id=guild_id,
            overrides=await content.prompts(db, guild_id),
            defaults=defaults,
            parts=content.PROMPT_PARTS,
            error=error,
            notice=notice,
        )

    @app.get("/g/{guild_id}/prompts", response_class=HTMLResponse)
    async def prompts_view(request: Request, session: User, db: Db, guild_id: Guild):
        return await prompts_page(request, session, db, guild_id)

    @app.post("/g/{guild_id}/prompts", response_class=HTMLResponse, dependencies=[Csrf])
    async def prompts_change(
        request: Request,
        session: User,
        db: Db,
        guild_id: Guild,
        part: Annotated[str, Form()],
        body: Annotated[str, Form()] = "",
        reset: Annotated[str, Form()] = "",
    ):
        if not reset and part in content.PROMPT_PARTS:
            # The editor starts from the default: saving it unchanged keeps following
            # the built-in text (and its future updates) rather than freezing a copy.
            try:
                default = (await ingester.prompt(guild_id, session.user_name))[f"{part}_default"]
            except (IngesterError, KeyError):
                default = None
            if default is not None and content.same_text(body, default):
                reset = "1"
        try:
            await content.set_prompt(db, guild_id, session.user_id, part, None if reset else body)
        except content.Invalid as e:
            return await prompts_page(request, session, db, guild_id, str(e), None, 422)
        notice = (
            f"{part} {'reset to the default' if reset else 'saved'}; applies to the next reply."
        )
        return await prompts_page(request, session, db, guild_id, notice=notice)

    async def facts_page(request, session, db, guild_id, error=None, notice=None, status=200):
        facts = await content.facts(db, guild_id)
        enabled_chars = sum(len(f[1]) + len(f[2]) for f in facts if f[3])
        return await page(
            request,
            "facts.html",
            session,
            db,
            status_code=status,
            guild_id=guild_id,
            facts=facts,
            lingo=await content.lingo(db, guild_id),
            enabled_chars=enabled_chars,
            budget=content.FACTS_BUDGET_CHARS,
            error=error,
            notice=notice,
        )

    @app.get("/g/{guild_id}/facts", response_class=HTMLResponse)
    async def facts_view(request: Request, session: User, db: Db, guild_id: Guild):
        return await facts_page(request, session, db, guild_id)

    @app.post("/g/{guild_id}/facts", response_class=HTMLResponse, dependencies=[Csrf])
    async def facts_change(
        request: Request,
        session: User,
        db: Db,
        guild_id: Guild,
        action: Annotated[str, Form()],
        fact_id: Annotated[int | None, Form()] = None,
        title: Annotated[str, Form()] = "",
        body: Annotated[str, Form()] = "",
        enabled: Annotated[str, Form()] = "",
        phrase: Annotated[str, Form()] = "",
        meaning: Annotated[str, Form()] = "",
    ):
        actor = session.user_id
        try:
            match action:
                case "add":
                    await content.add_fact(db, guild_id, actor, title, body)
                case "edit" if fact_id is not None:
                    await content.edit_fact(
                        db, guild_id, actor, fact_id, title, body, enabled == "on"
                    )
                case "delete" if fact_id is not None:
                    await content.delete_fact(db, guild_id, actor, fact_id)
                case "lingo_set":
                    await content.set_lingo(db, guild_id, actor, phrase, meaning)
                case "lingo_delete":
                    await content.delete_lingo(db, guild_id, actor, phrase)
                case _:
                    raise content.Invalid("Unknown action.")
        except content.Invalid as e:
            return await facts_page(request, session, db, guild_id, str(e), None, 422)
        return await facts_page(request, session, db, guild_id, notice="Saved.")

    async def chat_page(request, session, db, guild_id, thread_id, error=None, status=200):
        return await page(
            request,
            "chat.html",
            session,
            db,
            status_code=status,
            guild_id=guild_id,
            thread_id=thread_id,
            messages=await chat.thread(db, guild_id, session.user_id, thread_id),
            threads=await chat.threads(db, guild_id, session.user_id),
            error=error,
        )

    @app.get("/g/{guild_id}/chat", response_class=HTMLResponse)
    async def chat_view(
        request: Request, session: User, db: Db, guild_id: Guild, thread: UUID | None = None
    ):
        return await chat_page(request, session, db, guild_id, thread or uuid4())

    @app.post("/g/{guild_id}/chat", response_class=HTMLResponse, dependencies=[Csrf])
    async def chat_send(
        request: Request,
        session: User,
        db: Db,
        guild_id: Guild,
        thread: Annotated[UUID, Form()],
        message: Annotated[str, Form()] = "",
    ):
        try:
            await chat.send(
                db, ingester, guild_id, session.user_id, session.user_name, thread, message
            )
        except (content.Invalid, IngesterError) as e:
            return await chat_page(request, session, db, guild_id, thread, str(e), 422)
        return RedirectResponse(f"/g/{guild_id}/chat?thread={thread}", status_code=303)

    @app.get("/g/{guild_id}/audit", response_class=HTMLResponse)
    async def audit_guild(request: Request, session: User, db: Db, guild_id: Guild):
        rows = await queries.audit(db, guild_id)
        return await page(request, "audit.html", session, db, guild_id=guild_id, rows=rows)

    @app.get("/favicon.ico")
    async def favicon():
        return Response(status_code=204)

    return app


def application() -> FastAPI:
    """Uvicorn factory."""
    return create_app(Settings.from_env())
