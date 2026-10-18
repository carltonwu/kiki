# Evidence for the agent host restructure

The stack `refactor/agent-pin` … `refactor/agent-api` moves
`services/agent/src/kiki_agent_host/` from 15 flat modules into packages. It is
meant to keep behavior. This directory holds the checks behind that claim and
the scripts that rerun them.

## What was checked

| File | Shows |
|---|---|
| `verify-stack.tsv` | Ruff, format, and pytest at every branch head, the contract snapshot compared with the pin commit, and line counts. Every head passes, and the snapshot never changes. |
| `sim-agent-events.jsonl`, `compare_sim_events.py` | The agent host's tool, reply and drawing events from one `kiki-sim test` run on main and one on the stack's top. The counts per job kind, tool and outcome match: 33 passed, 0 failed, 3 skipped in both runs. |
| `bench-startup.txt`, `bench_startup.py` | Cold start in 30 interleaved fresh interpreters per side. HTTP import: 152 ms on main, 155 ms on the refactor (medians). Import plus the app factory: 508 ms and 514 ms. The ranges overlap. Importing the agent SDK dominates the second number. |
| `loc.txt`, `loc.py` | Source lines split by kind. Code lines went from 3,446 to 3,421. The total went from 4,250 to 4,441 because 15 files became 40: imports +104, module docstrings +20, blank lines +92. |
| `decisions.tsv` | The decision log, one row per decision, with the evidence for each. |

## How the code moved

`split.py` moves top-level symbols from one module into several and adds the
imports between them. `repoint.py` rewrites imports of the moved symbols. The
specs that drove each split are in `specs/`. Some edits were made by hand
afterwards: the merged tool guard, `Deps`, the toolsets, and the fixes listed in
`decisions.tsv`.

## Rerun the checks

From the repository root, with the local test database running:

```sh
TEST_DATABASE_URL=postgres://kiki:kiki@localhost:54329/kiki \
	docs/evidence/agent-refactor/verify-stack.sh origin/main refactor/agent-pin \
	refactor/agent-foundation refactor/agent-chat refactor/agent-media \
	refactor/agent-toolsets refactor/agent-api
python3 docs/evidence/agent-refactor/compare_sim_events.py \
	docs/evidence/agent-refactor/sim-agent-events.jsonl
python3 docs/evidence/agent-refactor/loc.py origin/main
```

To rerun the startup benchmark, check out main and the refactor in two
worktrees, then run `bench_startup.py MAIN_SRC HEAD_SRC 30` with
`services/agent`'s environment (`uv run python ...`).
