"""Client for the ingester's private admin API (services/ingester/src/admin.rs)."""

from urllib.parse import quote

import httpx

from .config import Settings

# The ingester's host deadline is 180 s by default, including wake-up retries.
CHAT_TIMEOUT = 200


class IngesterError(Exception):
    """A message safe to show to the person who made the request."""


class Ingester:
    def __init__(self, settings: Settings, client: httpx.AsyncClient):
        self.base = settings.ingester_admin_url
        self.token = settings.admin_api_token
        self.client = client

    @property
    def configured(self) -> bool:
        return bool(self.base and self.token)

    async def call(self, method: str, path: str, timeout: float = 15, **kwargs) -> dict:
        if not self.configured:
            raise IngesterError("The ingester admin API isn't configured (INGESTER_ADMIN_URL).")
        try:
            response = await self.client.request(
                method,
                f"{self.base}/admin/v1{path}",
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=timeout,
                **kwargs,
            )
        except httpx.HTTPError as error:
            raise IngesterError(f"Couldn't reach the ingester ({type(error).__name__}).") from None
        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.status_code >= 400:
            if response.status_code in (400, 404, 422, 502, 503) and body.get("error"):
                raise IngesterError(str(body["error"]))
            raise IngesterError(f"The ingester returned HTTP {response.status_code}.")
        return body

    async def settings(self, guild_id: int) -> dict:
        return await self.call("GET", f"/guilds/{guild_id}/settings")

    async def change(self, guild_id: int, key: str, value: str | None, actor: int) -> dict:
        return await self.call(
            "PUT",
            f"/guilds/{guild_id}/settings/{quote(key, safe='')}",
            json={"value": value, "actor": str(actor)},
        )

    async def prompt(self, guild_id: int, actor_name: str) -> dict:
        return await self.call(
            "GET", f"/guilds/{guild_id}/prompt", params={"actor_name": actor_name}
        )

    async def chat(
        self, guild_id: int, job_id: str, actor: int, actor_name: str, messages: list[dict]
    ) -> dict:
        return await self.call(
            "POST",
            f"/guilds/{guild_id}/chat",
            timeout=CHAT_TIMEOUT,
            json={
                "job_id": job_id,
                "actor": str(actor),
                "actor_name": actor_name,
                "messages": messages,
            },
        )
