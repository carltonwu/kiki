"""Starting, resetting and stopping the simulated stack, isolated from production.

Isolation rules (docs/kiki-sim-hld.md): the sim's own Postgres (checked through
`sim_marker` before anything is dropped), a scrubbed environment, and a working
directory outside the repo so no `.env` is ever loaded.
"""

import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

import psycopg
import yaml

ROOT = Path(__file__).resolve().parents[4]
SIM = ROOT / "tools/sim"
STATE = ROOT / ".sim"
LOGS = STATE / "logs"
MIGRATIONS = ROOT / "services/ingester/migrations"
DEFAULT_FIXTURE = SIM / "fixtures/minimal.yaml"

FAKECORD_PORT = 8090
HOST_PORT = 8091
INGESTER_PORT = 8092
DATABASE = "kiki_sim"
TEMPLATE = "kiki_sim_template"
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


def admin_url() -> str:
    """The sim server. CI may point at its own service, but only on localhost."""
    url = os.getenv("KIKI_SIM_POSTGRES", "postgres://kiki:kiki@127.0.0.1:54339/postgres")
    if urlparse(url).hostname not in ("127.0.0.1", "localhost", "::1"):
        raise SimError(f"KIKI_SIM_POSTGRES must be on localhost, not {urlparse(url).hostname}")
    return url


def database_url(name: str = DATABASE) -> str:
    return urlparse(admin_url())._replace(path=f"/{name}").geturl()


def connect(name: str = DATABASE, **kwargs) -> psycopg.Connection:
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


def build_ingester() -> Path:
    run(["cargo", "build", "--quiet", "-p", "ingester"], cwd=ROOT)
    return ROOT / "target/debug/ingester"


def prepare_template(ingester: Path) -> None:
    """A migrated template database, rebuilt when the migrations change."""
    wanted = migrations_hash()
    with psycopg.connect(admin_url(), autocommit=True) as c:
        row = c.execute(
            "SELECT shobj_description(oid, 'pg_database') FROM pg_database WHERE datname = %s",
            (TEMPLATE,),
        ).fetchone()
        if row and row[0] == wanted:
            return
        c.execute(f"DROP DATABASE IF EXISTS {TEMPLATE} WITH (FORCE)")
        c.execute(f"CREATE DATABASE {TEMPLATE}")
    run(
        [str(ingester), "migrate"],
        env=service_env({"DATABASE_URL": database_url(TEMPLATE)}),
        cwd=workdir(),
    )
    with psycopg.connect(admin_url(), autocommit=True) as c:
        c.execute(f"COMMENT ON DATABASE {TEMPLATE} IS '{wanted}'")


def recreate_database() -> None:
    check_marker()
    with psycopg.connect(admin_url(), autocommit=True) as c:
        c.execute(f"DROP DATABASE IF EXISTS {DATABASE} WITH (FORCE)")
        c.execute(f"CREATE DATABASE {DATABASE} TEMPLATE {TEMPLATE}")


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


class Config:
    """What `up` decided; saved so later commands talk to the same stack."""

    def __init__(self, fixture: Path, live: bool, env: dict | None = None):
        self.fixture = fixture
        self.live = live
        self.env = env or {}

    def save(self) -> None:
        STATE.mkdir(exist_ok=True)
        (STATE / "config.json").write_text(
            json.dumps({"fixture": str(self.fixture), "live": self.live, "env": self.env})
        )

    @classmethod
    def load(cls) -> "Config":
        try:
            data = json.loads((STATE / "config.json").read_text())
        except FileNotFoundError as e:
            raise SimError("the simulator isn't up: run `kiki-sim up` first") from e
        return cls(Path(data["fixture"]), data["live"], data.get("env"))

    def guild_id(self) -> str:
        return str(yaml.safe_load(self.fixture.read_text())["guild"]["id"])

    def ingester_env(self, overrides: dict[str, str]) -> dict[str, str]:
        values = {
            "DATABASE_URL": database_url(),
            "PORT": str(INGESTER_PORT),
            "LOG_FORMAT": "json",
            "RUST_LOG": "info,ingester=debug,sqlx=warn,twilight_http_ratelimiting=warn",
            "DISCORD_BOT_TOKEN": "sim-token",
            "DISCORD_GUILD_IDS": self.guild_id(),
            "DISCORD_API_PROXY": f"127.0.0.1:{FAKECORD_PORT}",
            "DISCORD_GATEWAY_URL": f"ws://127.0.0.1:{FAKECORD_PORT}/gateway",
            "AGENT_HOST_URL": f"http://127.0.0.1:{HOST_PORT}",
            "AGENT_HOST_TOKEN": HOST_TOKEN,
        }
        if self.live:
            values["OPENROUTER_DECISION_API_KEY"] = live_keys()["OPENROUTER_DECISION_API_KEY"]
        else:
            values |= {
                "OPENROUTER_BASE_URL": f"http://127.0.0.1:{FAKECORD_PORT}/openrouter",
                "OPENROUTER_DECISION_API_KEY": "sim-key",
                "OPENROUTER_CHAT_MODEL": "sim/chat",
            }
        return service_env(values | self.env | overrides)

    def host_env(self) -> dict[str, str]:
        values = {
            "DATABASE_URL": database_url(),
            "PORT": str(HOST_PORT),
            "LOG_FORMAT": "json",
            "AGENT_HOST_TOKEN": HOST_TOKEN,
            # Attachments are served by fakecord, not Discord's CDN.
            "DISCORD_MEDIA_EXTRA_HOSTS": f"127.0.0.1:{FAKECORD_PORT}",
            # The fake guild's emoji images, in live mode too.
            "DISCORD_CDN_URL": f"http://127.0.0.1:{FAKECORD_PORT}/cdn",
        }
        if self.live:
            keys = live_keys()
            values["OPENROUTER_CHAT_API_KEY"] = keys["OPENROUTER_CHAT_API_KEY"]
            if "OPENROUTER_VISION_MODEL" in keys:
                values["OPENROUTER_VISION_MODEL"] = keys["OPENROUTER_VISION_MODEL"]
        else:
            values |= {
                "OPENROUTER_BASE_URL": f"http://127.0.0.1:{FAKECORD_PORT}/openrouter",
                "OPENROUTER_CHAT_API_KEY": "sim-key",
                "OPENROUTER_VISION_MODEL": "sim/vision",
            }
        return service_env(values)


# --- processes ---------------------------------------------------------------


def pid_path(name: str) -> Path:
    return STATE / f"{name}.pid"


def log_path(name: str) -> Path:
    return LOGS / f"{name}.log"


def start(name: str, cmd: list[str], env: dict[str, str]) -> None:
    LOGS.mkdir(parents=True, exist_ok=True)
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


def stop(name: str, timeout: float = 15) -> None:
    pid = running(name)
    if pid:
        os.killpg(pid, signal.SIGTERM)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and running(name):
            time.sleep(0.1)
        if running(name):
            os.killpg(pid, signal.SIGKILL)
            time.sleep(0.2)
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
        f"http://127.0.0.1:{FAKECORD_PORT}/_sim/{path}",
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
        time.sleep(0.1)
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
    ingester = build_ingester()
    prepare_template(ingester)
    config.save()
    if not running("fakecord"):
        start(
            "fakecord",
            [
                sys.executable,
                "-m",
                "kiki_sim.fakecord",
                "--port",
                str(FAKECORD_PORT),
                "--fixture",
                str(config.fixture),
            ],
            service_env({"PYTHONPATH": str(SIM / "src")}),
        )
        wait_for(
            "fakecord",
            lambda: http_ok(f"http://127.0.0.1:{FAKECORD_PORT}/_sim/state"),
            15,
            "fakecord",
        )
    reset(config)


def reset(config: Config, env: dict[str, str] | None = None, fixture: Path | None = None) -> None:
    """A fresh database and guild, then both services, waiting until ready."""
    stop("ingester")
    stop("agent")
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
    start("ingester", [str(ROOT / "target/debug/ingester")], config.ingester_env(env or {}))
    wait_for("agent host", lambda: http_ok(f"http://127.0.0.1:{HOST_PORT}/health"), 60, "agent")
    wait_for("router", lambda: log_contains("ingester", "router listening", since), 30, "ingester")
    wait_for("GUILD_CREATE", lambda: control("state")["guild_creates"] > 0, 30, "ingester")

    def history_stored() -> bool:
        with connect() as c:
            return c.execute("SELECT count(*) FROM messages").fetchone()[0] >= history

    wait_for("fixture history", history_stored, 30, "ingester")


def down(purge: bool = False) -> None:
    for name in ("ingester", "agent", "fakecord"):
        stop(name)
    (STATE / "config.json").unlink(missing_ok=True)
    if purge and not os.getenv("KIKI_SIM_POSTGRES"):
        run(["docker", "compose", "-f", str(SIM / "compose.yml"), "down", "-v"])
