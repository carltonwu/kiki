"""Backfill every message in a Discord guild into a SQLite database.

Safe to stop at any time (Ctrl+C) and re-run: each channel keeps a cursor of the
newest message stored, and pages forward from there. Re-running later also picks
up any messages sent since the last run.
"""

from __future__ import annotations

import json
import os
import random
import sqlite3
import sys
import time
from collections import deque

import httpx
from dotenv import load_dotenv
from rich.console import Console
from rich.live import Live
from rich.progress_bar import ProgressBar
from rich.table import Table
from rich.text import Text

API = "https://discord.com/api/v10"
PAGE_SIZE = 100  # Discord's max for message history
MAX_RETRIES = 8  # for network errors / 5xx (429s are always waited out)

# Channel types: https://discord.com/developers/docs/resources/channel#channel-object-channel-types
MESSAGE_CHANNEL_TYPES = {0, 2, 5, 13}  # text, voice, announcement, stage
THREAD_PARENT_TYPES = {0, 5, 15, 16}  # text, announcement, forum, media

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY,  -- snowflake; sortable by time
    channel_id  INTEGER NOT NULL,     -- channel or thread id
    author_id   INTEGER,
    author_name TEXT,
    content     TEXT,
    created_at  TEXT NOT NULL,        -- ISO 8601
    edited_at   TEXT,
    type        INTEGER,
    reply_to_id INTEGER,
    raw         TEXT NOT NULL         -- full message object as JSON
);
CREATE INDEX IF NOT EXISTS messages_channel ON messages (channel_id, id);
CREATE INDEX IF NOT EXISTS messages_author ON messages (author_id);

-- Resume state: one row per channel/thread.
CREATE TABLE IF NOT EXISTS channels (
    id        INTEGER PRIMARY KEY,
    parent_id INTEGER,
    name      TEXT,
    type      INTEGER,
    cursor    INTEGER NOT NULL DEFAULT 0  -- highest message id stored
);
"""


class Progress:
    """Live status panel. rich re-renders it on a timer, so it stays current during waits."""

    def __init__(self) -> None:
        self.console = Console(stderr=True, highlight=False)
        self.start = time.monotonic()
        self.channel = "discovering channels and threads…"
        self.index = 0
        self.count = 0
        self.completed = 0
        self.position = ""  # date of the newest message fetched in the current channel
        self.total = 0
        self.requests = 0
        self.wait_until = 0.0
        self.wait_reason = ""
        self.samples: deque[tuple[float, int]] = deque()

    def set_channel(self, index: int, count: int, name: str) -> None:
        self.index, self.count, self.channel, self.position = index, count, name, ""

    def add(self, batch: list[dict]) -> None:
        self.total += len(batch)
        self.position = batch[-1]["timestamp"][:10]
        self.samples.append((time.monotonic(), self.total))

    def sleep(self, seconds: float, reason: str) -> None:
        self.wait_reason, self.wait_until = reason, time.monotonic() + seconds
        time.sleep(seconds)
        self.wait_until = 0.0

    def log(self, msg: str, style: str = "dim") -> None:
        self.console.print(msg, style=style)

    def rate(self, now: float) -> float:
        while self.samples and now - self.samples[0][0] > 10:  # 10s rolling window
            self.samples.popleft()
        if len(self.samples) < 2:
            return 0.0
        t0, n0 = self.samples[0]
        return (self.total - n0) / (now - t0)

    def __rich__(self) -> Table:
        now = time.monotonic()
        mins, secs = divmod(int(now - self.start), 60)
        grid = Table.grid(padding=(0, 1))
        grid.add_row(
            Text(f"[{self.index}/{self.count}]" if self.count else "", style="dim"),
            Text(f"#{self.channel}" if self.count else self.channel, style="bold"),
        )
        grid.add_row(
            "",
            ProgressBar(total=self.count or None, completed=self.completed, width=40),
        )
        grid.add_row(
            "",
            Text.assemble(
                (f"{self.total:,}", "bold cyan"),
                " msgs  ",
                (f"{self.rate(now):,.0f}", "bold green"),
                " msg/s  ",
                (f"{self.requests:,}", "bold"),
                f" requests  {mins:02}:{secs:02}",
            ),
        )
        if self.wait_until > now:
            status = Text(f"{self.wait_reason}, resuming in {self.wait_until - now:.1f}s", "yellow")
        else:
            status = Text(f"fetched up to {self.position}" if self.position else "", "dim")
        grid.add_row("", status)
        return grid


class DiscordAPI:
    """Minimal REST client that honours Discord rate limits and retries failures."""

    def __init__(self, token: str, progress: Progress) -> None:
        self.http = httpx.Client(
            base_url=API,
            headers={
                "Authorization": f"Bot {token}",
                "User-Agent": "DiscordBot (https://github.com/, 0.1) guild-backfill",
            },
            timeout=30,
        )
        self.progress = progress
        self.route_reset: dict[str, float] = {}  # path -> monotonic time it's usable again
        self.global_reset = 0.0

    def get(self, path: str, params: dict | None = None):
        """GET a JSON resource. Returns None if the bot can't access it (403/404)."""
        attempt = 0
        while True:
            route_reset = self.route_reset.get(path, 0.0)
            wait = max(self.global_reset, route_reset) - time.monotonic()
            if wait > 0:
                reason = "global rate limit" if self.global_reset > route_reset else "rate limit"
                self.progress.sleep(wait, reason)

            try:
                r = self.http.get(path, params=params)
            except httpx.TransportError as e:
                attempt = self._backoff(attempt, f"network error ({e!r})")
                continue
            self.progress.requests += 1

            # Pre-emptively pause when this route's bucket is exhausted.
            if r.headers.get("X-RateLimit-Remaining") == "0":
                reset_after = float(r.headers.get("X-RateLimit-Reset-After", 1))
                self.route_reset[path] = time.monotonic() + reset_after

            if r.status_code == 429:
                try:
                    body = r.json()
                except ValueError:  # Cloudflare-level limit returns HTML
                    body = {}
                retry_after = float(body.get("retry_after") or r.headers.get("Retry-After") or 5)
                until = time.monotonic() + retry_after
                if body.get("global") or r.headers.get("X-RateLimit-Scope") == "global":
                    self.global_reset = until
                else:
                    self.route_reset[path] = until
                continue

            if r.status_code >= 500:
                attempt = self._backoff(attempt, f"HTTP {r.status_code} on {path}")
                continue
            if r.status_code == 401:
                raise SystemExit("\n401 Unauthorized: check DISCORD_BOT_TOKEN in .env")
            if r.status_code in (403, 404):
                return None
            r.raise_for_status()
            return r.json()

    def _backoff(self, attempt: int, reason: str) -> int:
        if attempt >= MAX_RETRIES:
            raise RuntimeError(f"giving up after {attempt} retries: {reason}")
        delay = min(60.0, 2.0**attempt) * random.uniform(0.5, 1.0)
        self.progress.log(f"{reason}; retry {attempt + 1}/{MAX_RETRIES}", style="yellow")
        self.progress.sleep(delay, f"{reason}, retry {attempt + 1}/{MAX_RETRIES}")
        return attempt + 1


def discover_channels(api: DiscordAPI, guild_id: str) -> list[dict]:
    """All channels that hold messages, plus every active and archived thread."""
    channels = api.get(f"/guilds/{guild_id}/channels")
    if channels is None:
        raise SystemExit(f"Can't access guild {guild_id}: is the bot in it and the id correct?")

    threads: dict[str, dict] = {}
    active = api.get(f"/guilds/{guild_id}/threads/active") or {}
    for t in active.get("threads", []):
        threads[t["id"]] = t

    for ch in channels:
        if ch["type"] not in THREAD_PARENT_TYPES:
            continue
        # Private threads only exist in text channels and need Manage Threads.
        kinds = ["public", "private"] if ch["type"] == 0 else ["public"]
        for kind in kinds:
            before = None
            while True:
                params = {"limit": 100} | ({"before": before} if before else {})
                page = api.get(f"/channels/{ch['id']}/threads/archived/{kind}", params)
                if not page:
                    break
                for t in page["threads"]:
                    threads[t["id"]] = t
                if not page.get("has_more") or not page["threads"]:
                    break
                before = page["threads"][-1]["thread_metadata"]["archive_timestamp"]

    return [c for c in channels if c["type"] in MESSAGE_CHANNEL_TYPES] + list(threads.values())


def message_row(m: dict) -> tuple:
    ref = m.get("message_reference") or {}
    return (
        int(m["id"]),
        int(m["channel_id"]),
        int(m["author"]["id"]) if m.get("author") else None,
        m["author"].get("username") if m.get("author") else None,
        m.get("content"),
        m["timestamp"],
        m.get("edited_timestamp"),
        m.get("type"),
        int(ref["message_id"]) if ref.get("message_id") else None,
        json.dumps(m, separators=(",", ":"), ensure_ascii=False),
    )


def backfill_channel(api: DiscordAPI, db: sqlite3.Connection, ch: dict, progress: Progress) -> None:
    cid = int(ch["id"])
    with db:
        db.execute(
            """INSERT INTO channels (id, parent_id, name, type) VALUES (?, ?, ?, ?)
               ON CONFLICT (id) DO UPDATE SET
                 parent_id = excluded.parent_id, name = excluded.name, type = excluded.type""",
            (cid, ch.get("parent_id"), ch.get("name"), ch["type"]),
        )
    cursor = db.execute("SELECT cursor FROM channels WHERE id = ?", (cid,)).fetchone()[0]

    # Skip without a request if nothing new since the last run.
    last = ch.get("last_message_id")
    if last is None or int(last) <= cursor:
        return

    while True:
        batch = api.get(f"/channels/{cid}/messages", {"limit": PAGE_SIZE, "after": cursor})
        if batch is None:
            progress.log(f"no access, skipping #{ch.get('name')} ({cid})", style="yellow")
            return
        if not batch:
            return
        batch.sort(key=lambda m: int(m["id"]))
        cursor = int(batch[-1]["id"])
        with db:  # messages and cursor commit atomically, so resume is exact
            db.executemany(
                "INSERT OR IGNORE INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [message_row(m) for m in batch],
            )
            db.execute("UPDATE channels SET cursor = ? WHERE id = ?", (cursor, cid))
        progress.add(batch)
        if len(batch) < PAGE_SIZE:
            return


def main() -> None:
    load_dotenv()
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    guild_id = os.environ.get("DISCORD_GUILD_ID", "").strip()
    db_path = os.environ.get("DB_PATH", "discord.db").strip()
    if not token or not guild_id:
        raise SystemExit("Set DISCORD_BOT_TOKEN and DISCORD_GUILD_ID in .env")

    db = sqlite3.connect(db_path)
    db.execute("PRAGMA journal_mode = WAL")
    db.execute("PRAGMA synchronous = NORMAL")
    db.executescript(SCHEMA)

    progress = Progress()
    api = DiscordAPI(token, progress)
    try:
        with Live(progress, console=progress.console, refresh_per_second=8):
            channels = discover_channels(api, guild_id)
            progress.log(f"found {len(channels)} channels/threads")
            for i, ch in enumerate(channels, 1):
                progress.set_channel(i, len(channels), ch.get("name") or ch["id"])
                backfill_channel(api, db, ch, progress)
                progress.completed = i
    except KeyboardInterrupt:
        progress.log("interrupted; progress is saved, re-run to resume", style="yellow")
        sys.exit(130)
    finally:
        db.close()

    stored = sqlite3.connect(db_path).execute("SELECT count(*) FROM messages").fetchone()[0]
    progress.log(
        f"done in {time.monotonic() - progress.start:.0f}s: {progress.total:,} new messages, "
        f"{stored:,} total in {db_path}",
        style="bold green",
    )


if __name__ == "__main__":
    main()
