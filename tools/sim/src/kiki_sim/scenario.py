"""YAML scenarios: steps that talk in the fake guild, and stage-aware expectations.

    name: what this checks
    live: false            # true: also runs with --live; only: runs only with --live
    env: {JEV_MIN_CONFIDENCE: "0.3"}   # ingester settings for this scenario
    script: {jev: [...], reply: [...]} # stub answers, matched by text
    steps:
      - say: {as: alex, in: general, text: "kiki hi", id: hi}   # send, then wait
      - send: {...}                                             # send, don't wait
      - batch: [{as: ..., in: ..., text: ...}, ...]             # several, quickly
      - edit: {of: hi, text: "kiki hello"}
      - delete: {of: hi}
      - slash: {as: alex, in: general, command: model, options: {model: sim/other}}
      - expect: {of: hi, outcome: new, reply: true, text_contains: "hello"}
      - drop_gateway: {}
      - wait: 1.5

`say`/`send` take `reply_to` (a step id, or `kiki` for Kiki's latest message in
that channel), `age_s`, and the sugar `jev: {choice, confidence}` and
`reply: {text | tool}` to script the stub's answer to exactly this message.
"""

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from . import stack
from .observe import Observation, observe, poll, render

EVENTUALLY_S = 5


@dataclass
class Result:
    name: str
    path: Path
    status: str = "pass"  # pass / fail / skip
    failures: list[str] = field(default_factory=list)
    transcript: list[str] = field(default_factory=list)
    seconds: float = 0.0


class Run:
    def __init__(self, spec: dict, config: stack.Config):
        self.spec = spec
        self.config = config
        self.env = {str(k): str(v) for k, v in (spec.get("env") or {}).items()}
        self.named: dict[str, dict] = {}
        self.observed: dict[str, Observation] = {}
        self.slashes: dict[str, dict] = {}
        self.last: dict | None = None
        self.last_slash: dict | None = None
        self.failures: list[str] = []
        self.history: set[str] = set()

    def fail(self, step: int, message: str) -> None:
        self.failures.append(f"step {step}: {message}")

    # --- steps -------------------------------------------------------------

    def send(self, args: dict, wait: bool) -> dict:
        body = {"as": args["as"], "in": args["in"], "text": str(args["text"])}
        if "age_s" in args:
            body["age_s"] = args["age_s"]
        if reply_to := args.get("reply_to"):
            body["reply_to"] = self.resolve_reply(reply_to, args["in"])
        script = {}
        exact = re.escape(str(args["text"]))
        if "jev" in args:
            script["jev"] = [{"match": exact, **args["jev"]}]
        if "reply" in args:
            script["reply"] = [{"match": exact, **args["reply"]}]
        if script:
            stack.control("script", script)
        message = stack.control("messages", body)
        message["_age_s"] = float(args.get("age_s", 0))
        self.last = message
        if "id" in args:
            self.named[args["id"]] = message
        if wait:
            self.observed[message["id"]] = observe(message, self.config, env=self.env)
        return message

    def resolve_reply(self, target: str, channel: str) -> str:
        if target in self.named:
            return self.named[target]["id"]
        if target == "kiki":
            state = stack.control("state")
            channel_id = state["channels"][channel]
            ours = [
                m
                for m in state["messages"]
                if m["author"]["id"] == state["bot"]["id"] and m["channel_id"] == channel_id
            ]
            if not ours:
                raise ValueError(f"Kiki hasn't said anything in #{channel} yet")
            return ours[-1]["id"]
        raise ValueError(f"unknown step id {target!r}")

    def target(self, args: dict) -> dict:
        if "of" in args:
            return self.named[args["of"]]
        if self.last is None:
            raise ValueError("expect before any message")
        return self.last

    def observation(self, message: dict) -> Observation:
        if message["id"] not in self.observed:
            self.observed[message["id"]] = observe(message, self.config, env=self.env)
        return self.observed[message["id"]]

    def slash(self, args: dict) -> dict:
        sent = stack.control(
            "interactions",
            {
                "as": args["as"],
                "in": args["in"],
                "command": args["command"],
                "options": args.get("options") or {},
            },
        )

        def response():
            for i in stack.control("state")["interactions"]:
                if i["interaction_id"] == sent["id"] and i["kind"] == "response":
                    return i["body"]
            return None

        result = {"id": sent["id"], "response": poll(response, 15)}
        self.last_slash = result
        if "id" in args:
            self.slashes[args["id"]] = result
        return result

    def expect(self, step: int, args: dict) -> None:
        if "sql" in args:
            self.expect_sql(step, args)
            return
        if "response_contains" in args:
            slash = self.slashes.get(args.get("of")) or self.last_slash
            text = json.dumps(slash and slash["response"], ensure_ascii=False).lower()
            if str(args["response_contains"]).lower() not in text:
                self.fail(step, f"slash response lacks {args['response_contains']!r}: {text[:300]}")
            return
        message = self.target(args)
        o = self.observation(message)
        label = f"{message['content'][:40]!r}"

        def check(name, actual, expected):
            if name in args and actual != expected:
                self.fail(
                    step, f"{label}: {name} is {actual!r}, expected {expected!r} [{o.summary()}]"
                )

        if "stored" in args and o.stored != args["stored"]:
            self.fail(step, f"{label}: stored is {o.stored}")
        for name in ("check", "outcome", "jev_choice", "run"):
            if name in args:
                check(name, getattr(o, name), args[name])
        if "classified" in args and (o.outcome is not None) != args["classified"]:
            self.fail(step, f"{label}: classified is {o.outcome is not None} [{o.summary()}]")
        if "reply" in args:
            check("reply", o.reply, args["reply"])
        if "conversation" in args:
            wanted = args["conversation"]
            if wanted == "new" and o.outcome != "new":
                self.fail(step, f"{label}: expected a new conversation [{o.summary()}]")
            elif wanted == "none" and o.conversation_id is not None:
                self.fail(step, f"{label}: expected no conversation [{o.summary()}]")
            elif isinstance(wanted, dict):
                other = self.observation(self.named[wanted["same_as"]])
                same = o.conversation_id is not None and o.conversation_id == other.conversation_id
                if not same:
                    self.fail(
                        step,
                        f"{label}: conversation {o.conversation_id}, expected"
                        f" {other.conversation_id} (same as {wanted['same_as']})",
                    )
        replies = " ".join(r["content"] for r in o.replies)
        for needle in as_list(args.get("text_contains")):
            if str(needle).lower() not in replies.lower():
                self.fail(step, f"{label}: reply lacks {needle!r}: {replies[:300]!r}")
        for pattern in as_list(args.get("text_matches")):
            if not re.search(pattern, replies, re.IGNORECASE | re.S):
                self.fail(step, f"{label}: reply doesn't match {pattern!r}: {replies[:300]!r}")
        for pattern in as_list(args.get("text_lacks")):
            if re.search(pattern, replies, re.IGNORECASE | re.S):
                self.fail(step, f"{label}: reply matches {pattern!r}: {replies[:300]!r}")
        if (
            "tool_called" in args
            or "tool_result_contains" in args
            or any(k in args for k in ("prompt_contains", "prompt_lacks"))
        ):
            self.expect_llm(step, args, label)
        if "reaction" in args:
            self.expect_reaction(step, args["reaction"], message, label)

    def expect_sql(self, step: int, args: dict) -> None:
        """Escape hatch: `{sql: "SELECT …", equals: value}` (first column, first row)."""
        with stack.connect() as db:
            row = db.execute(args["sql"]).fetchone()
        value = row[0] if row else None
        if str(value) != str(args.get("equals")):
            self.fail(step, f"{args['sql']!r} is {value!r}, expected {args.get('equals')!r}")

    def expect_llm(self, step: int, args: dict, label: str) -> None:
        records = [r for r in stack.control("state")["llm"] if r["kind"] == "reply"]
        name = args.get("tool_called")
        if name and not any((r.get("tool_call") or {}).get("name") == name for r in records):
            self.fail(step, f"{label}: no {name} tool call")
        for needle in as_list(args.get("tool_result_contains")):
            results = " ".join(" ".join(r.get("tool_results", [])) for r in records)
            if str(needle).lower() not in results.lower():
                self.fail(step, f"{label}: tool results lack {needle!r}: {results[:300]!r}")
        prompt = ""
        if records:
            prompt = json.dumps(records[-1]["request"]["messages"], ensure_ascii=False)
        for needle in as_list(args.get("prompt_contains")):
            if str(needle).lower() not in prompt.lower():
                self.fail(step, f"{label}: prompt lacks {needle!r}")
        for needle in as_list(args.get("prompt_lacks")):
            if str(needle).lower() in prompt.lower():
                self.fail(step, f"{label}: prompt contains {needle!r}")

    def expect_reaction(self, step: int, wanted: dict, message: dict, label: str) -> None:
        emoji, present = wanted.get("emoji", "👀"), wanted.get("present", False)

        def matches():
            on = None
            for r in stack.control("state")["reactions"]:
                if r["message_id"] == message["id"] and r["emoji"] == emoji:
                    on = r["action"] == "add"
            return bool(on) == present and (on is not None or not present)

        if not poll(matches, EVENTUALLY_S, 0.2):
            self.fail(step, f"{label}: {emoji} reaction present should be {present}")

    def drop_gateway(self) -> None:
        before = stack.control("state")["guild_creates"]
        stack.control("drop-gateway", {})
        if not poll(lambda: stack.control("state")["guild_creates"] > before, 30, 0.2):
            raise RuntimeError("the ingester didn't reconnect")

    # --- running -----------------------------------------------------------

    def execute(self) -> None:
        stack.control("script", self.spec.get("script") or {})
        self.history = {m["id"] for m in stack.control("state")["messages"]}
        for i, step in enumerate(self.spec["steps"], 1):
            [(kind, args)] = step.items() if len(step) == 1 else self.split(step)
            if kind in ("say", "send"):
                extra = {k: v for k, v in step.items() if k != kind}
                self.send({**args, **extra}, wait=kind == "say")
            elif kind == "batch":
                for item in args:
                    self.send(item, wait=False)
                    time.sleep(float(item.get("gap_s", 0.1)))
            elif kind == "edit":
                stack.control(
                    f"messages/{self.named[args['of']]['id']}", {"text": args["text"]}, "PATCH"
                )
            elif kind == "delete":
                stack.control(f"messages/{self.named[args['of']]['id']}", None, "DELETE")
            elif kind == "slash":
                self.slash(args)
            elif kind == "expect":
                self.expect(i, args)
            elif kind == "drop_gateway":
                self.drop_gateway()
            elif kind == "wait":
                time.sleep(float(args))
            else:
                raise ValueError(f"unknown step {kind!r}")
        state = stack.control("state")
        for call in state["unexpected"]:
            self.failures.append(f"unexpected call: {call['method']} {call['path']}")
        for entry in state["unused_scripts"]:
            self.failures.append(f"unused {entry['kind']} script: {entry['match']!r}")

    @staticmethod
    def split(step: dict):
        """`say: {...}` with sugar keys (`jev:`, `reply:`) next to it."""
        kinds = [k for k in step if k in ("say", "send")]
        if len(kinds) != 1:
            raise ValueError(f"a step needs exactly one action: {step}")
        return [(kinds[0], step[kinds[0]])]

    def transcript(self) -> list[str]:
        state = stack.control("state")
        lines = [render(m, state) for m in state["messages"] if m["id"] not in self.history]
        for i in state["interactions"]:
            if i["kind"] == "response":
                lines.append(f"[/kiki] → {json.dumps(i['body'], ensure_ascii=False)[:200]}")
        return lines


def as_list(value) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def discover(paths: list[Path]) -> list[Path]:
    found = []
    for path in paths:
        found += sorted(path.glob("*.yaml")) if path.is_dir() else [path]
    return found


def run_file(path: Path, config: stack.Config, live: bool) -> Result:
    spec = yaml.safe_load(path.read_text())
    result = Result(spec.get("name", path.stem), path)
    mode = spec.get("live", False)
    if (live and not mode) or (not live and mode == "only"):
        result.status = "skip"
        return result
    started = time.monotonic()
    fixture = stack.SIM / "fixtures" / f"{spec['fixture']}.yaml" if spec.get("fixture") else None
    stack.reset(config, {str(k): str(v) for k, v in (spec.get("env") or {}).items()}, fixture)
    run = Run(spec, config)
    try:
        run.execute()
    except Exception as e:  # noqa: BLE001 - a broken scenario is a failed one
        run.failures.append(f"{type(e).__name__}: {e}")
    result.failures = run.failures
    result.status = "fail" if run.failures else "pass"
    result.transcript = run.transcript()
    result.seconds = time.monotonic() - started
    return result
