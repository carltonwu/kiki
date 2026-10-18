# Kiki

Discord chat bot: `services/ingester` (Rust: Gateway, storage, routing, agent
dispatch) and `services/agent` (Python agent host). Design docs are in `docs/`.

## Verifying changes

After changing anything under `services/`, run the local simulator and its
scenarios (details in `.claude/skills/kiki-sim/SKILL.md`):

    tools/sim/kiki-sim up && tools/sim/kiki-sim test

Also run the service checks from `.github/workflows/ci.yml`: cargo fmt, clippy
and test, plus ruff and pytest for `services/agent`.
