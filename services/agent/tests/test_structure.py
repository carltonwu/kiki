"""The package layout as rules: which parts may import which, no module named after
one chat platform, and an HTTP layer that starts without the agent SDK."""

import ast
import subprocess
import sys
from pathlib import Path

import kiki_agent_host

ROOT = Path(kiki_agent_host.__file__).parent

# What each part may import at module level, lowest layer first. The longest
# matching key decides. Imports inside functions are exempt: they defer a heavy
# import (api.app loads agent.executor only when a key is set).
ALLOWED = {
    "config": [],
    "db": [],
    "logs": [],
    "contracts": [],
    "executor": ["contracts"],
    "prompt": ["contracts"],
    "openrouter": ["openrouter", "config"],
    "media.fetch": ["config"],
    "chat": ["chat", "config", "db", "contracts", "openrouter", "media.fetch"],
    "media": ["media", "chat", "config", "db", "contracts", "prompt", "openrouter"],
    "agent": [
        "agent",
        "chat",
        "media",
        "openrouter",
        "config",
        "db",
        "contracts",
        "executor",
        "prompt",
    ],
    "api": ["api", "", "config", "contracts", "executor", "db"],
    "__main__": ["logs"],
}
PLATFORM_WORDS = ("discord", "guild")


def modules() -> dict[str, Path]:
    found = {}
    for path in ROOT.rglob("*.py"):
        parts = path.relative_to(ROOT).with_suffix("").parts
        if parts[-1] == "__init__":
            parts = parts[:-1]
        found[".".join(parts)] = path
    return found


def imports(name: str, path: Path, known: dict[str, Path]) -> set[str]:
    package = name.split(".") if path.name == "__init__.py" else name.split(".")[:-1]
    package = [p for p in package if p]
    found = set()
    for node in ast.parse(path.read_text()).body:
        if not (isinstance(node, ast.ImportFrom) and node.level):
            continue
        base = package[: len(package) - (node.level - 1)]
        base += node.module.split(".") if node.module else []
        for alias in node.names:
            submodule = ".".join([*base, alias.name])
            found.add(submodule if submodule in known else ".".join(base))
    return found


def rule(name: str) -> list[str]:
    keys = [k for k in ALLOWED if name == k or name.startswith(k + ".")]
    assert keys, f"{name} has no layering rule; add it to ALLOWED"
    return ALLOWED[max(keys, key=len)]


def test_parts_import_only_the_layers_below_them():
    known = modules()
    wrong = []
    for name, path in known.items():
        if not name:
            continue
        allowed = rule(name)
        for target in imports(name, path, known):
            ok = any(target == a or (a and target.startswith(a + ".")) for a in allowed)
            if not ok:
                wrong.append(f"{name} -> {target or 'kiki_agent_host'}")
    assert not wrong, "imports across layers:\n" + "\n".join(sorted(wrong))


def test_no_module_is_named_after_a_chat_platform():
    named = [name for name in modules() if any(word in name.lower() for word in PLATFORM_WORDS)]
    assert not named


def test_the_http_layer_starts_without_the_agent_sdk():
    loaded = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, kiki_agent_host.api.app; print('pydantic_ai' in sys.modules)",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert loaded.stdout.strip() == "False"
