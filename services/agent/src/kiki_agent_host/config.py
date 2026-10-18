"""Everything the host reads from the environment."""

import os
from dataclasses import dataclass

# Draws what the image model's safety filter blocks (seen in prod: most failed
# drawings were OpenAI blocks of campy, harmless requests). Empty turns it off.
DEFAULT_IMAGE_FALLBACK_MODEL = "qwen/qwen-image-2.1-turbo"


@dataclass(frozen=True)
class Settings:
    database_url: str
    token: str
    max_concurrent_jobs: int = 2
    job_timeout_seconds: float = 120
    openrouter_api_key: str = ""
    openrouter_vision_model: str = ""
    openrouter_image_fallback_model: str = DEFAULT_IMAGE_FALLBACK_MODEL
    # Refreshes expired Discord attachment links (reference images; `media.fetch.refresh`).
    discord_bot_token: str = ""

    def __post_init__(self) -> None:
        if not self.database_url.strip() or not self.token.strip():
            raise ValueError("DATABASE_URL and AGENT_HOST_TOKEN must be nonempty")
        if self.max_concurrent_jobs < 1 or self.job_timeout_seconds <= 0:
            raise ValueError("job concurrency and timeout must be positive")

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            database_url=os.environ["DATABASE_URL"],
            token=os.environ["AGENT_HOST_TOKEN"],
            max_concurrent_jobs=int(os.getenv("AGENT_HOST_MAX_CONCURRENT_JOBS", "2")),
            job_timeout_seconds=float(os.getenv("AGENT_HOST_JOB_TIMEOUT_SECS", "120")),
            openrouter_api_key=os.getenv("OPENROUTER_CHAT_API_KEY", ""),
            openrouter_vision_model=os.getenv("OPENROUTER_VISION_MODEL", "").strip(),
            openrouter_image_fallback_model=os.getenv(
                "OPENROUTER_IMAGE_FALLBACK_MODEL", DEFAULT_IMAGE_FALLBACK_MODEL
            ).strip(),
            discord_bot_token=os.getenv("DISCORD_BOT_TOKEN", "").strip(),
        )


def openrouter_base() -> str:
    """OpenRouter's API root; `OPENROUTER_BASE_URL` points at the local simulator."""
    return (os.getenv("OPENROUTER_BASE_URL") or "https://openrouter.ai/api").rstrip("/")


def discord_api() -> str:
    """Discord's REST root; `DISCORD_API_URL` points at the local simulator."""
    return (os.getenv("DISCORD_API_URL") or "https://discord.com/api/v10").rstrip("/")


def cdn_base() -> str:
    """Discord's CDN; `DISCORD_CDN_URL` points at the local simulator."""
    return (os.getenv("DISCORD_CDN_URL") or "https://cdn.discordapp.com").rstrip("/")


def extra_media_hosts() -> set[str]:
    """Hosts besides Discord's that images may come from (the simulator)."""
    return {h.strip() for h in os.getenv("DISCORD_MEDIA_EXTRA_HOSTS", "").split(",")} - {""}
