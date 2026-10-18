"""Interleaved cold-start timings, main vs head, in fresh interpreters.
usage: bench_startup.py MAIN_SRC HEAD_SRC RUNS"""
import json, os, statistics, subprocess, sys
main_src, head_src, runs = sys.argv[1], sys.argv[2], int(sys.argv[3])
PROBE = """
import time, logging, sys
logging.disable(logging.CRITICAL)
t = time.perf_counter()
import importlib
app = importlib.import_module(sys.argv[1])
http = time.perf_counter() - t
app.application()
print(http * 1000, (time.perf_counter() - t) * 1000, 'pydantic_ai' in sys.modules)
"""
env = dict(os.environ, DATABASE_URL="postgres://x", AGENT_HOST_TOKEN="t", OPENROUTER_CHAT_API_KEY="k")
results = {"main": [], "head": []}
for i in range(runs):
    for side, src, module in (("main", main_src, "kiki_agent_host.app"), ("head", head_src, "kiki_agent_host.api.app"))[:: 1 if i % 2 else -1]:
        out = subprocess.run([sys.executable, "-c", PROBE, module], env=dict(env, PYTHONPATH=src), capture_output=True, text=True, check=True).stdout.split()
        results[side].append((float(out[0]), float(out[1]), out[2]))
for side, rows in results.items():
    http = [r[0] for r in rows]; full = [r[1] for r in rows]
    print(f"{side}: http-layer import median {statistics.median(http):.0f} ms (min {min(http):.0f}, max {max(http):.0f}); "
          f"import + application() median {statistics.median(full):.0f} ms (min {min(full):.0f}, max {max(full):.0f}); runs {len(rows)}; sdk loaded after factory: {set(r[2] for r in rows)}")
