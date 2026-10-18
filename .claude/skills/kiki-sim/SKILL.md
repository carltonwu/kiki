---
name: kiki-sim
description: Test Kiki end to end locally against a fake Discord guild (tools/sim). Use after changing anything in services/ (routing, the agent host, prompts, commands, storage) to verify the change, and to reproduce or debug how Kiki responds to a conversation.
---

# Verifying a change with kiki-sim

kiki-sim runs the real ingester and agent host against **fakecord**, a fake
Discord (Gateway + REST) with stub LLMs, in its own Postgres container. It never
touches production. Design: `docs/kiki-sim-hld.md`.

1. Start it (rebuilds the ingester and migrates a template database):
   `tools/sim/kiki-sim up`
   After changing Rust or Python code, run `up` again (`down` then `up` if
   fakecord itself changed).
2. Run the scenarios: `tools/sim/kiki-sim test` (or `-k name`). All must pass.
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
6. `tools/sim/kiki-sim down` when done.

Report the `test` summary line in your final message. If a scenario fails,
`.sim/logs/` has each service's log.
