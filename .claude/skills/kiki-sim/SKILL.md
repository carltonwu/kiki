---
name: kiki-sim
description: Test Kiki end to end locally against a fake Discord guild (tools/sim). Use after changing anything in services/ (routing, the agent host, prompts, commands, storage) to verify the change, and to reproduce or debug how Kiki responds to a conversation.
---

# Verifying a change with kiki-sim

kiki-sim runs the real ingester and agent host against **fakecord**, a fake
Discord (Gateway + REST) with stub LLMs, in its own Postgres container. It never
touches production. Design: `docs/kiki-sim-hld.md`.

1. Start it: `tools/sim/kiki-sim up`. It reuses a cached ingester build when
   the Rust sources match one built before, in any worktree, and builds one
   otherwise. After changing Rust or Python code, run `up` again (`down` then
   `up` if fakecord itself changed).
2. Run the scenarios: `tools/sim/kiki-sim test -j 4` (or `-k name`). All must
   pass. `-j 4` runs them on four stacks at once (about 30 s, against 2 min on
   one).
3. Add a scenario in `tools/sim/scenarios/` that fails without your change and
   passes with it. Scenario format: the docstring at the top of
   `tools/sim/src/kiki_sim/scenario.py`. Script Jev and chat answers per
   message with `jev:` / `reply:` (stub mode matches them by text).
4. Poke at it by hand when useful:
   - `tools/sim/kiki-sim say --as alex --in general "kiki hi"` prints the
     classification (check, outcome, Jev choice and confidence, conversation),
     the run, and Kiki's reply.
   - `transcript`, `inspect <message id>`, `logs ingester|agent|fakecord`,
     `slash --as alex config key=chat_model value=sim/other`.
   - Fixture members: alex (admin), sam, ethan, chris, chrissy, chris_l, pilz
     (a bot); channels general, lore, staff, general-thread.
5. For prompt or tone changes, also check real model output:
   `tools/sim/kiki-sim up --live` (uses the OpenRouter keys from the repo's
   `.env`, about $0.001 per reply), `say` a few messages, and quote the
   transcript in your summary. Live output is judged by reading it; say so.
6. `tools/sim/kiki-sim down --all` when done. Stacks hold Postgres
   connections while they're up.

Stacks are isolated: each has its own ports, database and logs, so parallel
agents can each run one. Use your own worktree, or pass `--stack NAME` (or set
`KIKI_SIM_STACK`) in a shared one. `kiki-sim stacks` lists this worktree's.

Report the `test` summary line in your final message. If a scenario fails,
`.sim/<stack>/logs/` has each service's log.
