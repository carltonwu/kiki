"""Starting, resetting and stopping the simulated stack, isolated from production.

Isolation rules (docs/kiki-sim-hld.md): the sim's own Postgres (checked through
`sim_marker` before anything is dropped), a scrubbed environment, and a working
directory outside the repo so no `.env` is ever loaded.

Several stacks run side by side, in one worktree or many: each has a name
(`--stack`, `KIKI_SIM_STACK`), its own state directory, free ports picked at `up`,
and its own database, named after the worktree and the stack.
"""

import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from contextlib import contextmanager, suppress
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlparse

import psycopg
import yaml

ROOT = Path(__file__).resolve().parents[4]
SIM = ROOT / "tools/sim"
MIGRATIONS = ROOT / "services/ingester/migrations"
DEFAULT_FIXTURE = SIM / "fixtures/minimal.yaml"
# Shared by every worktree on this machine: built ingesters and the setup lock.
CACHE = Path(os.getenv("XDG_CACHE_HOME") or Path.home() / ".cache") / "kiki-sim"
# What the ingester binary is built from, besides `services/` (minus the agent).
RUST_INPUTS = ["Cargo.toml", "Cargo.lock", "rust-toolchain.toml", ".cargo"]
KEPT_BUILDS = 8
HOST_TOKEN = "sim-host-token"
# Variables a toolchain needs; everything else is set explicitly per service.
KEEP = [
    "PATH",
    "HOME",
    "USER",
    "LANG",
    "LC_ALL",
    "TERM",
    "TMPDIR",
    "XDG_CACHE_HOME",
    "XDG_RUNTIME_DIR",
    "CARGO_HOME",
    "RUSTUP_HOME",
    "UV_CACHE_DIR",
    "UV_PYTHON_INSTALL_DIR",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
]
LIVE_KEYS = ["OPENROUTER_CHAT_API_KEY", "OPENROUTER_DECISION_API_KEY"]
# Optional in live mode; without it, images go to the chat model.
LIVE_OPTIONAL = ["OPENROUTER_VISION_MODEL"]


class SimError(Exception):
    pass


@dataclass(frozen=True)
class Stack:
    """One simulated deployment: its own processes, ports, logs and database."""

    name: str

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,19}", self.name):
            raise SimError(f"stack names are lowercase letters, digits and dashes: {self.name!r}")

    @property
    def state(self) -> Path:
        return ROOT / ".sim" / self.name

    @property
    def logs(self) -> Path:
        return self.state / "logs"

    @property
    def database(self) -> str:
        """Unique on the shared server: the worktree's path and the stack's name."""
        worktree = hashlib.sha1(str(ROOT).encode()).hexdigest()[:8]
        return f"kiki_sim_{worktree}_{self.name.replace('-', '_')}"


current = Stack(os.getenv("KIKI_SIM_STACK") or "main")


def use(name: str) -> None:
    """Points this process at stack `name`; every command works on one stack."""
    global current, _config
    current, _config = Stack(name), None


def stacks() -> list[Stack]:
    """This worktree's stacks that have state on disk."""
    root = ROOT / ".sim"
    if not root.exists():
        return []
    return [Stack(p.name) for p in sorted(root.iterdir()) if (p / "logs").is_dir()]


@contextmanager
def machine_lock():
    """Serializes setup that every stack on this machine shares (the container,
    the template database, the build cache)."""
    CACHE.mkdir(parents=True, exist_ok=True)
    with open(CACHE / "setup.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def free_port() -> int:
    """A port free on IPv4 and IPv6: the services listen dual-stack."""
    with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        s.bind(("::", 0))
        return s.getsockname()[1]


@dataclass(frozen=True)
class Ports:
    fakecord: int
    host: int
    ingester: int

    @classmethod
    def pick(cls) -> "Ports":
        while len(ports := {free_port() for _ in range(3)}) < 3:
            pass
        return cls(*sorted(ports))


def admin_url() -> str:
    """The sim server. CI may point at its own service, but only on localhost."""
    url = os.getenv("KIKI_SIM_POSTGRES", "postgres://kiki:kiki@127.0.0.1:54339/postgres")
    if urlparse(url).hostname not in ("127.0.0.1", "localhost", "::1"):
        raise SimError(f"KIKI_SIM_POSTGRES must be on localhost, not {urlparse(url).hostname}")
    return url


def database_url(name: str | None = None) -> str:
    return urlparse(admin_url())._replace(path=f"/{name or current.database}").geturl()


def connect(name: str | None = None, **kwargs) -> psycopg.Connection:
    return psycopg.connect(database_url(name), autocommit=True, **kwargs)


def check_marker() -> None:
    try:
        with psycopg.connect(admin_url(), autocommit=True, connect_timeout=3) as c:
            c.execute("SELECT 1 FROM sim_marker LIMIT 1").fetchone()
    except psycopg.errors.UndefinedTable as e:
        raise SimError("refusing to touch a Postgres server without the sim_marker table") from e


def start_postgres() -> None:
    if os.getenv("KIKI_SIM_POSTGRES"):
        check_marker()
        return
    try:
        check_marker()
        return
    except psycopg.OperationalError:
        pass
    with machine_lock():
        try:
            check_marker()
            return
        except psycopg.OperationalError:
            pass
        run(["docker", "compose", "-f", str(SIM / "compose.yml"), "up", "-d", "--wait"])
    check_marker()


def run(cmd: list[str], **kwargs) -> None:
    result = subprocess.run(cmd, capture_output=True, text=True, **kwargs)
    if result.returncode:
        raise SimError(f"{' '.join(cmd)} failed:\n{result.stdout}\n{result.stderr}")


def migrations_hash() -> str:
    digest = hashlib.sha256()
    for path in sorted(MIGRATIONS.glob("*.sql")):
        digest.update(path.name.encode() + path.read_bytes())
    return digest.hexdigest()[:16]


def sources_hash() -> str:
    """The ingester's inputs as they are on disk, untracked files included."""
    listed = subprocess.run(
        ["git", "ls-files", "-co", "--exclude-standard", "-z", "--", *RUST_INPUTS, "services"],
        cwd=ROOT,
        capture_output=True,
        check=True,
    ).stdout.decode()
    digest = hashlib.sha256()
    for name in sorted(n for n in listed.split("\0") if n and not n.startswith("services/agent/")):
        path = ROOT / name
        if path.is_file():
            digest.update(name.encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()[:20]


def build_ingester() -> Path:
    """The ingester built from this worktree's sources, from a cache shared by
    every worktree: a fresh worktree with unchanged Rust skips cargo. The cache is
    keyed by content, not file times, so one worktree never runs another's code."""
    key = sources_hash()
    cached = CACHE / "ingester" / key / "ingester"
    if cached.exists():
        os.utime(cached.parent)
        return cached
    run(["cargo", "build", "--quiet", "-p", "ingester"], cwd=ROOT)
    if sources_hash() != key:
        raise SimError("the Rust sources changed during the build; run `up` again")
    with machine_lock():
        if not cached.exists():
            staging = cached.parent.with_name(f"{key}.{os.getpid()}")
            staging.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / "target/debug/ingester", staging / "ingester")
            staging.rename(cached.parent)
        builds = sorted((CACHE / "ingester").iterdir(), key=lambda p: p.stat().st_mtime)
        for old in builds[:-KEPT_BUILDS]:
            shutil.rmtree(old, ignore_errors=True)
    return cached


def template_name() -> str:
    """One migrated template per set of migrations, shared by every stack."""
    return f"kiki_sim_template_{migrations_hash()}"


def prepare_template(ingester: Path) -> None:
    template = template_name()
    with machine_lock(), psycopg.connect(admin_url(), autocommit=True) as c:
        if c.execute("SELECT 1 FROM pg_database WHERE datname = %s", (template,)).fetchone():
            return
        staging = f"{template}_new"
        c.execute(f"DROP DATABASE IF EXISTS {staging} WITH (FORCE)")
        c.execute(f"CREATE DATABASE {staging}")
        run(
            [str(ingester), "migrate"],
            env=service_env({"DATABASE_URL": database_url(staging)}),
            cwd=workdir(),
        )
        c.execute(f"ALTER DATABASE {staging} RENAME TO {template}")


def recreate_database() -> None:
    check_marker()
    # CREATE DATABASE fails while another session reads the template.
    with machine_lock(), psycopg.connect(admin_url(), autocommit=True) as c:
        c.execute(f"DROP DATABASE IF EXISTS {current.database} WITH (FORCE)")
        c.execute(f"CREATE DATABASE {current.database} TEMPLATE {template_name()}")


def drop_database() -> None:
    check_marker()
    with psycopg.connect(admin_url(), autocommit=True) as c:
        c.execute(f"DROP DATABASE IF EXISTS {current.database} WITH (FORCE)")


def workdir() -> str:
    """Outside the repo, so neither service finds a `.env` in a parent directory."""
    path = Path(tempfile.gettempdir()) / "kiki-sim-cwd"
    path.mkdir(exist_ok=True)
    return str(path)


def service_env(values: dict[str, str]) -> dict[str, str]:
    env = {k: os.environ[k] for k in KEEP if k in os.environ}
    env.update(values)
    return env


def live_keys() -> dict[str, str]:
    """Only the OpenRouter keys, from the environment or the repo's dotenv file."""
    wanted = LIVE_KEYS + LIVE_OPTIONAL
    found = {k: os.environ[k] for k in wanted if os.getenv(k)}
    path = ROOT / ".env"
    if path.exists():
        for line in path.read_text().splitlines():
            key, _, value = line.partition("=")
            if key.strip() in wanted and value.strip() and key.strip() not in found:
                found[key.strip()] = value.strip().strip("'\"")
    missing = [k for k in LIVE_KEYS if k not in found]
    if missing:
        raise SimError(f"live mode needs {', '.join(missing)} in the environment or .env")
    return found


_config: "Config | None" = None


class Config:
    """What `up` decided; saved so later commands talk to the same stack."""

    def __init__(
        self,
        fixture: Path,
        live: bool,
        env: dict | None = None,
        ports: Ports | None = None,
        ingester: Path | None = None,
    ):
        self.fixture = fixture
        self.live = live
        self.env = env or {}
        self.ports = ports or Ports.pick()
        self.ingester = ingester

    def save(self) -> None:
        global _config
        current.state.mkdir(parents=True, exist_ok=True)
        (current.state / "config.json").write_text(
            json.dumps(
                {
                    "fixture": str(self.fixture),
                    "live": self.live,
                    "env": self.env,
                    "ports": asdict(self.ports),
                    "ingester": str(self.ingester),
                }
            )
        )
        _config = self

    @classmethod
    def load(cls) -> "Config":
        global _config
        if _config is None:
            try:
                data = json.loads((current.state / "config.json").read_text())
            except FileNotFoundError as e:
                raise SimError(f"stack {current.name} isn't up: run `kiki-sim up` first") from e
            _config = cls(
                Path(data["fixture"]),
                data["live"],
                data.get("env"),
                Ports(**data["ports"]),
                Path(data["ingester"]),
            )
        return _config

    def guild_id(self) -> str:
        return str(yaml.safe_load(self.fixture.read_text())["guild"]["id"])

    def ingester_env(self, overrides: dict[str, str]) -> dict[str, str]:
        values = {
            "DATABASE_URL": database_url(),
            "PORT": str(self.ports.ingester),
            "LOG_FORMAT": "json",
            "RUST_LOG": "info,ingester=debug,sqlx=warn,twilight_http_ratelimiting=warn",
            "DISCORD_BOT_TOKEN": "sim-token",
            "DISCORD_GUILD_IDS": self.guild_id(),
            "DISCORD_API_PROXY": f"127.0.0.1:{self.ports.fakecord}",
            "DISCORD_GATEWAY_URL": f"ws://127.0.0.1:{self.ports.fakecord}/gateway",
            "AGENT_HOST_URL": f"http://127.0.0.1:{self.ports.host}",
            "AGENT_HOST_TOKEN": HOST_TOKEN,
        }
        if self.live:
            values["OPENROUTER_DECISION_API_KEY"] = live_keys()["OPENROUTER_DECISION_API_KEY"]
        else:
            values |= {
                "OPENROUTER_BASE_URL": f"http://127.0.0.1:{self.ports.fakecord}/openrouter",
                "OPENROUTER_DECISION_API_KEY": "sim-key",
                "OPENROUTER_CHAT_MODEL": "sim/chat",
                "OPENROUTER_IMAGE_MODEL": "sim/image",
            }
        return service_env(values | self.env | overrides)

    def host_env(self) -> dict[str, str]:
        values = {
            "DATABASE_URL": database_url(),
            "PORT": str(self.ports.host),
            "LOG_FORMAT": "json",
            "AGENT_HOST_TOKEN": HOST_TOKEN,
            # Attachments are served by fakecord, not Discord's CDN.
            "DISCORD_MEDIA_EXTRA_HOSTS": f"127.0.0.1:{self.ports.fakecord}",
            # The fake guild's emoji images and avatars, in live mode too.
            "DISCORD_CDN_URL": f"http://127.0.0.1:{self.ports.fakecord}/cdn",
            # Expired attachment links are refreshed through fakecord's REST API.
            "DISCORD_API_URL": f"http://127.0.0.1:{self.ports.fakecord}/api/v10",
            "DISCORD_BOT_TOKEN": "sim-token",
        }
        if self.live:
            keys = live_keys()
            values["OPENROUTER_CHAT_API_KEY"] = keys["OPENROUTER_CHAT_API_KEY"]
            if "OPENROUTER_VISION_MODEL" in keys:
                values["OPENROUTER_VISION_MODEL"] = keys["OPENROUTER_VISION_MODEL"]
        else:
            values |= {
                "OPENROUTER_BASE_URL": f"http://127.0.0.1:{self.ports.fakecord}/openrouter",
                "OPENROUTER_CHAT_API_KEY": "sim-key",
                "OPENROUTER_VISION_MODEL": "sim/vision",
            }
        return service_env(values)


# --- processes ---------------------------------------------------------------


def pid_path(name: str) -> Path:
    return current.state / f"{name}.pid"


def log_path(name: str) -> Path:
    return current.logs / f"{name}.log"


def start(name: str, cmd: list[str], env: dict[str, str]) -> None:
    current.logs.mkdir(parents=True, exist_ok=True)
    with open(log_path(name), "ab") as out:
        out.write(f"\n--- start {time.strftime('%H:%M:%S')} ---\n".encode())
        out.flush()
        process = subprocess.Popen(
            cmd,
            env=env,
            cwd=workdir(),
            stdout=out,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    pid_path(name).write_text(str(process.pid))
    CHILDREN[name] = process


# Processes this CLI started: they must be reaped, or they'd look alive as zombies.
CHILDREN: dict[str, subprocess.Popen] = {}


def running(name: str) -> int | None:
    if name in CHILDREN:
        process = CHILDREN[name]
        return process.pid if process.poll() is None else None
    try:
        pid = int(pid_path(name).read_text())
        os.kill(pid, 0)
    except (FileNotFoundError, ValueError, ProcessLookupError):
        return None
    try:
        if Path(f"/proc/{pid}/stat").read_text().split(")")[-1].split()[0] == "Z":
            return None
    except (FileNotFoundError, IndexError):
        pass
    return pid


def stop(*names: str, timeout: float = 15) -> None:
    """Signals every named service at once, then waits for all of them."""
    pids = {name: pid for name in names if (pid := running(name))}
    for pid in pids.values():
        os.killpg(pid, signal.SIGTERM)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and any(running(n) for n in pids):
        time.sleep(0.05)
    if alive := [n for n in pids if running(n)]:
        for name in alive:
            os.killpg(pids[name], signal.SIGKILL)
        time.sleep(0.2)
    for name in names:
        pid_path(name).unlink(missing_ok=True)
        CHILDREN.pop(name, None)


def http_ok(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=1) as response:
            return response.status == 200
    except (urllib.error.URLError, ConnectionError, TimeoutError):
        return False


def control(path: str, body: dict | None = None, method: str | None = None) -> dict:
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(
        f"http://127.0.0.1:{Config.load().ports.fakecord}/_sim/{path}",
        data=data,
        method=method or ("POST" if data is not None else "GET"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as e:
        raise SimError(f"fakecord {path}: {e.code} {e.read().decode()[:300]}") from e


def wait_for(what: str, check, timeout: float, name: str | None = None) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        if name and not running(name):
            raise SimError(f"{name} exited while waiting for {what}:\n{tail(name)}")
        time.sleep(0.05)
    raise SimError(f"timed out waiting for {what}" + (f":\n{tail(name)}" if name else ""))


def tail(name: str, lines: int = 50) -> str:
    try:
        return "\n".join(log_path(name).read_text(errors="replace").splitlines()[-lines:])
    except FileNotFoundError:
        return "(no log)"


def log_contains(name: str, text: str, since: int) -> bool:
    try:
        with open(log_path(name), "rb") as f:
            f.seek(since)
            return text.encode() in f.read()
    except FileNotFoundError:
        return False


def log_size(name: str) -> int:
    try:
        return log_path(name).stat().st_size
    except FileNotFoundError:
        return 0


# --- lifecycle ---------------------------------------------------------------


def up(config: Config) -> None:
    start_postgres()
    config.ingester = build_ingester()
    prepare_template(config.ingester)
    if running("fakecord"):
        # Already up: keep the ports its services were started with.
        config.ports = Config.load().ports
    config.save()
    if not running("fakecord"):
        start(
            "fakecord",
            [
                sys.executable,
                "-m",
                "kiki_sim.fakecord",
                "--port",
                str(config.ports.fakecord),
                "--fixture",
                str(config.fixture),
            ],
            service_env({"PYTHONPATH": str(SIM / "src")}),
        )
        wait_for(
            "fakecord",
            lambda: http_ok(f"http://127.0.0.1:{config.ports.fakecord}/_sim/state"),
            15,
            "fakecord",
        )
    reset(config)


def reset(config: Config, env: dict[str, str] | None = None, fixture: Path | None = None) -> None:
    """A fresh database and guild, then both services, waiting until ready."""
    stop("ingester", "agent")
    recreate_database()
    control("reset", {"fixture": str(fixture or config.fixture)})
    history = sum(
        h.get("repeat", 1)
        for h in yaml.safe_load((fixture or config.fixture).read_text()).get("history", [])
    )
    start(
        "agent",
        [
            "uv",
            "run",
            "--quiet",
            "--project",
            str(ROOT / "services/agent"),
            "--locked",
            "kiki-agent-host",
        ],
        config.host_env(),
    )
    since = log_size("ingester")
    start("ingester", [str(config.ingester)], config.ingester_env(env or {}))
    wait_for(
        "agent host", lambda: http_ok(f"http://127.0.0.1:{config.ports.host}/health"), 60, "agent"
    )
    wait_for("router", lambda: log_contains("ingester", "router listening", since), 30, "ingester")
    wait_for("GUILD_CREATE", lambda: control("state")["guild_creates"] > 0, 30, "ingester")

    def history_stored() -> bool:
        with connect() as c:
            return c.execute("SELECT count(*) FROM messages").fetchone()[0] >= history

    wait_for("fixture history", history_stored, 30, "ingester")


def down() -> None:
    """Stops this stack's services and drops its database; logs stay for reading."""
    stop("ingester", "agent", "fakecord")
    (current.state / "config.json").unlink(missing_ok=True)
    with suppress(psycopg.OperationalError):  # the server is already gone
        drop_database()


def purge() -> None:
    """Removes the sim Postgres container, and with it every stack's database."""
    if not os.getenv("KIKI_SIM_POSTGRES"):
        run(["docker", "compose", "-f", str(SIM / "compose.yml"), "down", "-v"])
