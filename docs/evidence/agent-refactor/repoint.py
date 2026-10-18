"""Re-point imports of moved symbols.

usage: repoint.py [--no-attrs] OLD_MODULE SPEC.json FILE...

--no-attrs leaves `<old>.a` references alone, for a module name that is also a
variable name (`vision.cost`).

OLD_MODULE is a module path below kiki_agent_host (like `retrieval`). SPEC is the
split spec that moved its symbols. In each FILE, `from <old> import a, b` becomes
imports from the new modules, and `<old>.a` attribute references become `a` with an
import added; `<old>` is dropped from `from <package> import x, <old>, y` lists.
Symbols the spec doesn't list stay on OLD_MODULE. Run `ruff check --fix` after.
"""

import json
import re
import sys
from pathlib import Path

args = sys.argv[1:]
attrs = "--no-attrs" not in args
old, spec_path, *files = [a for a in args if a != "--no-attrs"]
spec = json.loads(Path(spec_path).read_text())
ROOT = "kiki_agent_host"


def module_of(target: str) -> str:
    return ".".join(Path(target).with_suffix("").parts)


new = {name: module_of(t) for t, s in spec.items() for name in s["symbols"]}
old_last = old.rsplit(".", 1)[-1]


def spelled(module: str, file: Path) -> str:
    """How FILE writes an import of `module` (relative inside the package)."""
    if ROOT not in file.resolve().parts:
        return f"{ROOT}.{module}"
    parts = file.resolve().parts
    package = list(parts[parts.index(ROOT) + 1 : -1])
    target = module.split(".")
    common = 0
    while common < min(len(package), len(target)) and package[common] == target[common]:
        common += 1
    return "." * (len(package) - common + 1) + ".".join(target[common:])


for name in files:
    path = Path(name)
    text = path.read_text()
    wanted: dict[str, set[str]] = {}

    def need(symbol: str) -> str:
        wanted.setdefault(new.get(symbol, old), set()).add(symbol)
        return symbol

    # from <...old> import a, b   (single line or parenthesized)
    pattern = re.compile(
        r"^from (\.+(?:\w+\.)*|kiki_agent_host\.(?:\w+\.)*)" + re.escape(old) + r" import (\([^)]*\)|[^\n]*)\n", re.M
    )

    def from_old(m: re.Match) -> str:
        for s in re.split(r"[\s,()]+", m[2]):
            if s:
                need(s)
        return ""

    text = pattern.sub(from_old, text)
    # <old>.Symbol
    uses = re.compile(r"(?<![.\w])" + re.escape(old_last) + r"\.([A-Za-z_]\w*)")
    if attrs and uses.search(text):
        text = uses.sub(lambda m: need(m[1]), text)
        # drop `old` from `from <pkg> import x, old, y`
        def drop(m: re.Match) -> str:
            names = [n for n in re.split(r"\s*,\s*", m[2].strip()) if n and n != old_last]
            return f"from {m[1]} import {', '.join(names)}\n" if names else ""

        text = re.sub(
            r"^from ([.\w]+) import ([\w ,]*\b" + re.escape(old_last) + r"\b[\w ,]*)\n",
            drop,
            text,
            flags=re.M,
        )
    if not wanted:
        continue
    lines = "".join(
        f"from {spelled(m, path)} import {', '.join(sorted(s))}\n" for m, s in sorted(wanted.items())
    )
    # after the last top-level import
    last = max(
        (m.end() for m in re.finditer(r"^(?:from [^\n]*?import (?:\([^)]*\)|[^\n]*)|import [^\n]*)\n", text, re.M)),
        default=0,
    )
    text = text[:last] + lines + text[last:]
    path.write_text(text)
    print(f"{name}: {', '.join(f'{m}({len(s)})' for m, s in sorted(wanted.items()))}")
