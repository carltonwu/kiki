"""Discord OAuth2 login and server-side sessions (docs/console-hld.md, "Auth").

The cookie holds 32 random bytes; Postgres stores only their SHA-256. Access to
a server needs Manage Server, Administrator or ownership there, re-checked with
Discord at most every RECHECK; operators (CONSOLE_OPERATOR_IDS) see every server.
"""

import hashlib
import logging
import secrets
from dataclasses import dataclass
from datetime import timedelta

import httpx
from psycopg import AsyncConnection

from .config import Settings

logger = logging.getLogger(__name__)

SESSION_COOKIE = "kiki_session"
STATE_COOKIE = "kiki_oauth_state"
SESSION_TTL = timedelta(hours=12)
RECHECK = timedelta(minutes=10)
# Past this without a successful re-check (Discord down), the session stops working.
RECHECK_GRACE = timedelta(hours=1)

ADMINISTRATOR = 0x8
MANAGE_GUILD = 0x20


class DiscordAuthError(Exception):
    """The access token no longer works: end the session."""


@dataclass(frozen=True)
class Session:
    token_hash: bytes
    user_id: int
    user_name: str
    operator: bool
    guild_ids: frozenset[int]
    csrf: str

    def can_manage(self, guild_id: int) -> bool:
        return guild_id in self.guild_ids


def hash_token(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


def manageable(guilds: list[dict]) -> set[int]:
    """Guild IDs where the user is owner, Administrator or has Manage Server."""
    found = set()
    for guild in guilds:
        try:
            permissions = int(guild.get("permissions", "0"))
            guild_id = int(guild["id"])
        except (KeyError, TypeError, ValueError):
            continue
        if guild.get("owner") or permissions & (ADMINISTRATOR | MANAGE_GUILD):
            found.add(guild_id)
    return found


class Discord:
    def __init__(self, settings: Settings, client: httpx.AsyncClient):
        self.settings = settings
        self.client = client

    def authorize_url(self, state: str) -> str:
        query = httpx.QueryParams(
            client_id=self.settings.discord_client_id,
            redirect_uri=self.settings.redirect_uri,
            response_type="code",
            scope="identify guilds",
            state=state,
            prompt="none",
        )
        return f"{self.settings.discord_authorize}?{query}"

    async def exchange(self, code: str) -> str:
        response = await self.client.post(
            f"{self.settings.discord_api}/oauth2/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.settings.redirect_uri,
            },
            auth=(self.settings.discord_client_id, self.settings.discord_client_secret),
            timeout=10,
        )
        if response.status_code != 200:
            raise DiscordAuthError(f"token exchange returned {response.status_code}")
        token = response.json().get("access_token")
        if not isinstance(token, str) or not token:
            raise DiscordAuthError("token exchange returned no access token")
        return token

    async def get(self, access_token: str, path: str):
        response = await self.client.get(
            f"{self.settings.discord_api}{path}",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
        if response.status_code == 401:
            raise DiscordAuthError("access token rejected")
        response.raise_for_status()
        return response.json()


async def known_guilds(db: AsyncConnection) -> set[int]:
    cursor = await db.execute("SELECT id FROM guilds")
    return {row[0] for row in await cursor.fetchall()}


async def allowed_guilds(
    db: AsyncConnection, discord: Discord, access_token: str, operator: bool
) -> set[int]:
    known = await known_guilds(db)
    if operator:
        return known
    return manageable(await discord.get(access_token, "/users/@me/guilds")) & known


async def create_session(
    db: AsyncConnection, settings: Settings, discord: Discord, access_token: str
) -> str | None:
    """Returns the cookie token, or None when the person may manage nothing."""
    user = await discord.get(access_token, "/users/@me")
    user_id = int(user["id"])
    operator = user_id in settings.operator_ids
    guild_ids = await allowed_guilds(db, discord, access_token, operator)
    if not guild_ids and not operator:
        logger.info("console login refused user_id=%s: manages no Kiki server", user_id)
        return None
    name = str(user.get("global_name") or user.get("username") or user_id)[:64]
    token = secrets.token_urlsafe(32)
    await db.execute(
        "INSERT INTO console_sessions (token_hash, user_id, user_name, operator, guild_ids, "
        "access_token, csrf, checked_at, expires_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, now(), now() + %s)",
        (
            hash_token(token),
            user_id,
            name,
            operator,
            sorted(guild_ids),
            access_token,
            secrets.token_urlsafe(32),
            SESSION_TTL,
        ),
    )
    await db.execute("DELETE FROM console_sessions WHERE expires_at < now()")
    logger.info("console login user_id=%s operator=%s guilds=%d", user_id, operator, len(guild_ids))
    return token


async def load_session(db: AsyncConnection, discord: Discord, token: str | None) -> Session | None:
    if not token or len(token) > 100:
        return None
    token_hash = hash_token(token)
    cursor = await db.execute(
        "SELECT user_id, user_name, operator, guild_ids, access_token, csrf, "
        "now() - checked_at FROM console_sessions WHERE token_hash = %s AND expires_at > now()",
        (token_hash,),
    )
    row = await cursor.fetchone()
    if not row:
        return None
    user_id, user_name, operator, guild_ids, access_token, csrf, since_check = row
    if operator and user_id not in discord.settings.operator_ids:
        # Removed from CONSOLE_OPERATOR_IDS: from now on, only the servers they manage.
        operator = False
        since_check = RECHECK_GRACE + RECHECK
        await db.execute(
            "UPDATE console_sessions SET operator = false WHERE token_hash = %s", (token_hash,)
        )
    if operator:
        # Access follows the user ID; the server list follows the database.
        guild_ids = await known_guilds(db)
    elif since_check > RECHECK:
        try:
            guild_ids = await allowed_guilds(db, discord, access_token, False)
        except DiscordAuthError:
            await delete_session(db, token)
            return None
        except httpx.HTTPError as error:
            logger.warning(
                "console permission re-check failed user_id=%s error_type=%s",
                user_id,
                type(error).__name__,
            )
            if since_check > RECHECK_GRACE:
                return None
        else:
            await db.execute(
                "UPDATE console_sessions SET guild_ids = %s, checked_at = now() "
                "WHERE token_hash = %s",
                (sorted(guild_ids), token_hash),
            )
            if not guild_ids:
                await delete_session(db, token)
                return None
    return Session(token_hash, user_id, user_name, operator, frozenset(guild_ids), csrf)


async def delete_session(db: AsyncConnection, token: str) -> None:
    await db.execute("DELETE FROM console_sessions WHERE token_hash = %s", (hash_token(token),))


def same(a: str | None, b: str) -> bool:
    return bool(a) and secrets.compare_digest(a.encode(), b.encode())
