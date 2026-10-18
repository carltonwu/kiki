"""Line counts of services/agent/src at a git ref, split into imports, module
docstrings, blank lines and the rest. usage: loc.py REF"""
import ast, subprocess, sys
ref = sys.argv[1]
files = subprocess.run(["git", "ls-tree", "-r", "--name-only", ref, "services/agent/src"], capture_output=True, text=True, check=True).stdout.split()
total = {"files": 0, "imports": 0, "docstrings": 0, "blank": 0, "code": 0}
for f in files:
    if not f.endswith(".py"):
        continue
    src = subprocess.run(["git", "show", f"{ref}:{f}"], capture_output=True, text=True, check=True).stdout
    lines = src.splitlines()
    tree = ast.parse(src)
    imp = sum(n.end_lineno - n.lineno + 1 for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom)))
    doc = 0
    if tree.body and isinstance(tree.body[0], ast.Expr) and isinstance(tree.body[0].value, ast.Constant):
        doc = tree.body[0].end_lineno - tree.body[0].lineno + 1
    blank = sum(1 for l in lines if not l.strip())
    total["files"] += 1
    total["imports"] += imp; total["docstrings"] += doc; total["blank"] += blank
    total["code"] += len(lines) - imp - doc - blank
print(ref, total, "total", sum(v for k, v in total.items() if k != "files"))
