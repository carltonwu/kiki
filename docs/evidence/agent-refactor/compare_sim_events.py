"""Counts agent host events per job kind, tool and outcome, split into the run on
main and the run on the refactor by logger name. usage: compare_sim_events.py LOG"""

import collections
import json
import re
import sys

MAIN = {"kiki_agent_host.agent", "kiki_agent_host.memory_tools", "kiki_agent_host.app"}
HEAD = {"kiki_agent_host.agent.guard", "kiki_agent_host.agent.executor", "kiki_agent_host.api.app"}
runs = {"main": collections.Counter(), "head": collections.Counter()}
for line in open(sys.argv[1]):
    event = json.loads(line)
    side = "head" if event["target"] in HEAD else "main" if event["target"] in MAIN else None
    if side is None:
        continue
    message = event["message"]
    if message.startswith("tool completed"):
        tool = re.search(r"tool=(\S+)", message)[1]
        runs[side][f"tool {tool} {re.search(r'outcome=(\S+)', message)[1]}"] += 1
    elif message.startswith("execution completed"):
        runs[side]["execution completed " + re.search(r"kind=(\S+)", message)[1]] += 1
    elif message.startswith("drawing completed"):
        runs[side]["drawing " + re.search(r"outcome=(\S+)", message)[1]] += 1
print("| event | main | refactor |\n|---|---|---|")
for key in sorted(set(runs["main"]) | set(runs["head"])):
    print(f"| {key} | {runs['main'][key]} | {runs['head'][key]} |")
same = runs["main"] == runs["head"]
print(f"\nidentical: {same}")
sys.exit(0 if same else 1)
