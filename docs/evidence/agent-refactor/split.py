"""Split one module into several by top-level symbol.

usage: split.py SOURCE SPEC.json

SPEC maps each target path to {"doc": docstring, "symbols": [names], "imports": [lines]}.
Every top-level symbol of SOURCE must land in exactly one target. Each target gets
SOURCE's import block, its own extra import lines, and `from <module> import name`
for names it uses that another target defines; `ruff check --fix` then drops the
unused imports. Comments directly above a symbol move with it.
"""

import ast
import json
import sys
from pathlib import Path

source_path, spec_path = Path(sys.argv[1]), Path(sys.argv[2])
source = source_path.read_text()
lines = source.splitlines(keepends=True)
tree = ast.parse(source)
spec = json.loads(spec_path.read_text())


def defined(node) -> list[str]:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return [node.name]
    if isinstance(node, ast.Assign):
        return [t.id for t in node.targets if isinstance(t, ast.Name)]
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return [node.target.id]
    return []


body = tree.body
if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
    body = body[1:]
imports = [n for n in body if isinstance(n, (ast.Import, ast.ImportFrom))]
import_block = "".join(lines[imports[0].lineno - 1 : imports[-1].end_lineno]) if imports else ""

chunks: dict[str, str] = {}
order: list[str] = []
glued: set[str] = set()  # chunks with no blank line above them in SOURCE
previous_end = imports[-1].end_lineno if imports else 0
for node in body:
    if node in imports:
        continue
    start = node.lineno - 1
    if getattr(node, "decorator_list", None):
        start = min(d.lineno for d in node.decorator_list) - 1
    # Pull up the comment lines directly above (no blank line in between).
    while start > previous_end and lines[start - 1].lstrip().startswith("#"):
        start -= 1
    text = "".join(lines[start : node.end_lineno])
    names = defined(node)
    if not names:
        # Statements like `logger = ...` handled by Assign; anything else is a bug here.
        raise SystemExit(f"unnamed top-level statement at line {node.lineno}: {text[:60]!r}")
    for name in names:
        chunks[name] = text
    if order and start == previous_end:
        glued.add(names[0])
    order.append(names[0])
    previous_end = node.end_lineno

owner = {}
for target, t in spec.items():
    for name in t["symbols"]:
        if name in owner:
            raise SystemExit(f"{name} assigned twice")
        if name not in chunks:
            raise SystemExit(f"{name} not in {source_path}")
        owner[name] = target
missing = [n for n in order if n not in owner and n != "logger"]
if missing:
    raise SystemExit(f"unassigned symbols: {missing}")


def parts_of(path: Path) -> list[str]:
    parts = path.resolve().with_suffix("").parts
    return list(parts[parts.index("kiki_agent_host") + 1 :])


def relative(target: str, module: list[str]) -> str:
    """`from <this> import ...` for `module` (parts below kiki_agent_host), seen from `target`."""
    package = parts_of(Path(target))[:-1]
    common = 0
    while common < min(len(package), len(module)) and package[common] == module[common]:
        common += 1
    return "." * (len(package) - common + 1) + ".".join(module[common:])


def rebase(block: str, target: str) -> str:
    """Relative imports in SOURCE, re-pointed from TARGET's package."""
    origin = parts_of(source_path)[:-1]
    out = []
    for line in block.splitlines(keepends=True):
        stripped = line.lstrip()
        if stripped.startswith("from ."):
            dots = len(stripped[5:]) - len(stripped[5:].lstrip("."))
            rest = stripped[5 + dots :]
            name, _, tail = rest.partition(" import ")
            base = origin[: len(origin) - (dots - 1)] + ([name] if name else [])
            line = f"from {relative(target, base)} import {tail}"
        out.append(line)
    return "".join(out)


for target, t in spec.items():
    mine = [n for n in order if owner.get(n) == target]
    code = ""
    for i, n in enumerate(mine):
        joined = i and n in glued and order.index(mine[i - 1]) == order.index(n) - 1
        code += ("\n" if joined else "\n\n\n" if i else "") + chunks[n].rstrip()
    used = {n.id for n in ast.walk(ast.parse(code)) if isinstance(n, ast.Name)}
    cross: dict[str, list[str]] = {}
    for name in sorted(used):
        other = owner.get(name)
        if other and other != target:
            cross.setdefault(relative(target, parts_of(Path(other))), []).append(name)
    extra = list(t.get("imports", []))
    extra += [f"from {m} import {', '.join(ns)}" for m, ns in sorted(cross.items())]
    logger = "logger = logging.getLogger(__name__)\n\n\n" if "logger" in used else ""
    text = f'"""{t["doc"]}"""\n\n{rebase(import_block, target)}' + "".join(f"{e}\n" for e in extra)
    text += f"\n{logger}{code}\n"
    Path(target).parent.mkdir(parents=True, exist_ok=True)
    Path(target).write_text(text)
    print(f"{target}: {len(mine)} symbols")
