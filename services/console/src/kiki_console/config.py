"""Environment settings. Secrets are never logged or rendered."""

import os
from dataclasses import dataclass, field
from urllib.parse import urlparse


@dataclass(frozen=True)
class Settings:
    database_url: str
    # Public base URL, e.g. https://console.example.com: OAuth redirect and cookie flags.
    console_url: str
    discord_client_id: str
    discord_client_secret: str
    ingester_admin_url: str
    admin_api_token: str
    operator_ids: frozenset[int] = field(default_factory=frozenset)
    discord_api: str = "https://discord.com/api/v10"
    discord_authorize: str = "https://discord.com/oauth2/authorize"

    def __post_init__(self) -> None:
        for name in ("database_url", "console_url", "discord_client_id", "discord_client_secret"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name.upper()} must be set")
        url = urlparse(self.console_url)
        if url.scheme not in ("http", "https") or not url.netloc:
            raise ValueError("CONSOLE_URL must be an http(s) base URL")
        if url.scheme == "http" and url.hostname not in ("localhost", "127.0.0.1"):
            raise ValueError("CONSOLE_URL must use https outside localhost")
        if self.ingester_admin_url and len(self.admin_api_token) < 32:
            raise ValueError("ADMIN_API_TOKEN must be at least 32 characters")

    @property
    def redirect_uri(self) -> str:
        return self.console_url.rstrip("/") + "/auth/callback"

    @property
    def secure_cookies(self) -> bool:
        return urlparse(self.console_url).scheme == "https"

    @classmethod
    def from_env(cls) -> "Settings":
        operators = frozenset(
            int(x) for x in os.getenv("CONSOLE_OPERATOR_IDS", "").replace(" ", "").split(",") if x
        )
        extra = {}
        if os.getenv("DISCORD_API_BASE"):
            extra["discord_api"] = os.environ["DISCORD_API_BASE"].rstrip("/")
        return cls(
            database_url=os.environ.get("DATABASE_URL", ""),
            console_url=os.getenv("CONSOLE_URL", "http://localhost:8095"),
            discord_client_id=os.environ.get("DISCORD_CLIENT_ID", ""),
            discord_client_secret=os.environ.get("DISCORD_CLIENT_SECRET", ""),
            ingester_admin_url=os.getenv("INGESTER_ADMIN_URL", "").rstrip("/"),
            admin_api_token=os.getenv("ADMIN_API_TOKEN", ""),
            operator_ids=operators,
            **extra,
        )
