"""YAML scenarios: steps that talk in the fake guild, and stage-aware expectations.

    name: what this checks
    live: false            # true: also runs with --live; only: runs only with --live
    env: {JEV_MIN_CONFIDENCE: "0.3"}   # ingester settings for this scenario
    host_env: {AGENT_HOST_JOB_TIMEOUT_SECS: "40"}   # ... and the agent host's
    script: {jev: [...], reply: [...]} # stub answers, matched by text
    steps:
      - say: {as: alex, in: general, text: "kiki hi", id: hi}   # send, then wait
      - send: {...}                                             # send, don't wait
      - batch: [{as: ..., in: ..., text: ...}, ...]             # several, quickly
      - edit: {of: hi, text: "kiki hello"}
      - delete: {of: hi}
      - slash: {as: alex, in: general, command: config,
                options: {key: chat_model, value: sim/other}}
      - expect: {of: hi, outcome: new, reply: true, text_contains: "hello"}
      - expect: {attachments: 1, attachment_type: image/png}   # files on the replies
      - expect: {followup: {status: done, attachments: 1, text_contains: "…"},
                 image_references: [photo of nougie], image_prompt_contains: "…"}
                 # a run's queued pictures, each posted later as a reply to the
                 # request, and what the newest image model call got
      - thread: {from: photo, name: Thread, as: alex}   # a thread on a message
      - drop_gateway: {}
      - expect: {issue: {title_contains: "provider_rejected", labels: [kiki-fault],
                         body_contains: "…", body_lacks: "…"}, issues: 1}
                 # the fault reporter's GitHub issues (it files within ~35 s)
      - expect: {sql: "SELECT …", equals: 2, within_s: 40}
      - exec: "INSERT INTO guild_lingo …"   # SQL against the sim database
      - add_emoji: {name: bruh, id: 900000000000005009, description: "…"}
      - remove_emoji: bruh                  # in fakecord, with GUILD_EMOJIS_UPDATE
      - wait: 1.5

`say`/`send` take `reply_to` (a step id, or `kiki` for Kiki's latest message in
that channel), `age_s`, `embeds` (link previews, delivered like Discord's unfurler as a
follow-up MESSAGE_UPDATE), `components` (a bot's Components V2 card, sent with
the message; in both, `{guild}`, `{channel:NAME}` and `{id:STEP}` become IDs, for
a quote bot's link to a message), `attachments` (`[{filename, label?, content_type?, expired?}]`;
the stub vision model describes an image as its label, the drawing step's
reference check turns down labels saying meme or screenshot, and an `expired`
link only downloads after Discord's refresh), and the sugar
`jev: {choice, confidence}` and `reply: {text | tool | tools | web}` to script the
stub's answer to exactly this message (`delay_s` makes its first request that slow).
In tool arguments, `{image_message}` is the
message ID from the trigger's image marker, else the newest one in the conversation.
`tools` is a sequence of calls; an argument `"{entry_id}"` (or `"{entry_ids:N}"`)
takes memory entry IDs from the previous call's result, and `"{cursor}"` its
paging cursor (channel_history).
`web: [{search: q, source: url}, {fetch: url}]` stands in for OpenRouter's server
tools; `expect: {tool_called: web_search}` reads the agent host's saved result, and
other tools its log, so `tool_called` also holds in live mode; `tools_offered:
openrouter:web_fetch` checks the last reply request (stub mode).
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
# The fault reporter's first pass after a fault: its 5 s debounce, or the 30 s
# minimum gap after the pass at startup.
FAULT_ISSUE_S = 40
# OpenRouter server tools, by the agent host's `web` result field that counts them.
WEB_TOOLS = {"web_search": "searches", "web_fetch": "fetches"}


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
        # Where this scenario's part of the agent host log starts (tool_called, live).
        self.agent_log_start = stack.log_size("agent")

    def fail(self, step: int, message: str) -> None:
        self.failures.append(f"step {step}: {message}")

    # --- steps -------------------------------------------------------------

    def send(self, args: dict, wait: bool) -> dict:
        body = {"as": args["as"], "in": args["in"], "text": str(args["text"])}
        if "age_s" in args:
            body["age_s"] = args["age_s"]
        for key in ("embeds", "components"):
            if key in args:
                body[key] = self.placeholders(args[key])
        if "attachments" in args:
            body["attachments"] = args["attachments"]
        if reply_to := args.get("reply_to"):
            body["reply_to"] = self.resolve_reply(reply_to, args["in"])
        script = {}
        # Matches the text as stored, where fakecord turned "@sam" into "<@id>".
        exact = "".join(
            rf"(?:@{re.escape(part[1:])}|<@\d+>)" if part.startswith("@") else re.escape(part)
            for part in re.split(r"(@[A-Za-z0-9_.]+)", str(args["text"]))
        )
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

    def placeholders(self, value):
        """`{guild}`, `{channel:NAME}` and `{id:STEP}` (a step's message ID) in a
        message's embeds or components, for links between messages."""
        state = stack.control("state")

        def fill(match: re.Match) -> str:
            kind, _, name = match[1].partition(":")
            if kind == "guild":
                return state["guild"]["id"]
            if kind == "channel":
                return state["channels"][name]
            return self.named[name]["id"]

        text = re.sub(r"\{(guild|channel:[\w-]+|id:[\w-]+)\}", fill, json.dumps(value))
        return json.loads(text)

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
        if "issue" in args or "issues" in args:
            self.expect_issues(step, args)
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
        files = [a for r in o.replies for a in r.get("attachments") or []]
        if "attachments" in args and len(files) != args["attachments"]:
            self.fail(step, f"{label}: {len(files)} attachments, expected {args['attachments']}")
        if "attachment_type" in args and not all(
            a.get("content_type") == args["attachment_type"] for a in files
        ):
            types = [a.get("content_type") for a in files]
            self.fail(
                step, f"{label}: attachment types {types}, expected {args['attachment_type']}"
            )
        if (
            "tool_called" in args
            or "tools_offered" in args
            or "tool_result_contains" in args
            or any(k in args for k in ("prompt_contains", "prompt_lacks"))
        ):
            self.expect_llm(step, args, o, label)
        if "followup" in args:
            self.expect_followup(step, args["followup"], o, message, label)
        if any(k in args for k in ("image_references", "image_prompt_contains")):
            self.expect_image_request(step, args, label)
        if "reaction" in args:
            self.expect_reaction(step, args["reaction"], message, label)
        if "decide_contains" in args or "decide_lacks" in args:
            self.expect_decide(step, args, message, label)

    def expect_followup(
        self, step: int, wanted: dict, o: Observation, message: dict, label: str
    ) -> None:
        """The pictures the run queued, each posted as its own message replying to
        the request: `{count, status, attachments, text_contains, text_lacks}`."""
        if len(o.drawings) != wanted.get("count", 1):
            self.fail(
                step, f"{label}: {len(o.drawings)} drawings, expected {wanted.get('count', 1)}"
            )
        for d in o.drawings:
            posted = d["message"]
            if "status" in wanted and d["status"] != wanted["status"]:
                self.fail(step, f"{label}: drawing {d['status']} ({d['error']})")
            if posted is None:
                self.fail(step, f"{label}: drawing {d['status']} posted nothing ({d['error']})")
                continue
            reference = (posted.get("message_reference") or {}).get("message_id")
            if reference != message["id"]:
                self.fail(step, f"{label}: the follow-up replies to {reference}, not the request")
            files = posted.get("attachments") or []
            if "attachments" in wanted and len(files) != wanted["attachments"]:
                self.fail(step, f"{label}: follow-up has {len(files)} attachments")
            for needle in as_list(wanted.get("text_contains")):
                if str(needle).lower() not in posted["content"].lower():
                    self.fail(step, f"{label}: follow-up lacks {needle!r}: {posted['content']!r}")
            for pattern in as_list(wanted.get("text_lacks")):
                if re.search(pattern, posted["content"], re.IGNORECASE):
                    self.fail(step, f"{label}: follow-up matches {pattern!r}")

    def expect_image_request(self, step: int, args: dict, label: str) -> None:
        """What the newest call to the image model got: reference images (by their
        sim labels, in order) and the prompt."""
        calls = [r for r in stack.control("state")["llm"] if r["kind"] == "image"]
        if not calls:
            self.fail(step, f"{label}: the image model wasn't called")
            return
        references, prompt = calls[-1]["references"], calls[-1]["prompt"]
        wanted = as_list(args.get("image_references"))
        if len(references) != len(wanted) or not all(
            str(w).lower() in r.lower() for w, r in zip(wanted, references, strict=False)
        ):
            self.fail(step, f"{label}: image references {references}, expected {wanted}")
        for needle in as_list(args.get("image_prompt_contains")):
            if str(needle).lower() not in prompt.lower():
                self.fail(step, f"{label}: image prompt lacks {needle!r}: {prompt[:400]!r}")

    def expect_decide(self, step: int, args: dict, message: dict, label: str) -> None:
        """What the router sent Jev for this message (JSON, compact)."""
        records = [
            r
            for r in stack.control("state")["llm"]
            if r["kind"] == "jev" and message["content"] in r["texts"]
        ]
        if not records:
            self.fail(step, f"{label}: Jev wasn't asked")
            return
        sent = json.dumps(
            records[-1]["request"]["state"], ensure_ascii=False, separators=(",", ":")
        )
        for needle in as_list(args.get("decide_contains")):
            if str(needle) not in sent:
                self.fail(step, f"{label}: Decide state lacks {needle!r}: {sent[:600]}")
        for needle in as_list(args.get("decide_lacks")):
            if str(needle) in sent:
                self.fail(step, f"{label}: Decide state contains {needle!r}")

    def expect_sql(self, step: int, args: dict) -> None:
        """Escape hatch: `{sql: "SELECT …", equals: value}` (first column, first row),
        eventually: what Gateway events write lands a moment after the step."""
        value = None

        def matches() -> bool:
            nonlocal value
            with stack.connect() as db:
                row = db.execute(args["sql"]).fetchone()
            value = row[0] if row else None
            return str(value) == str(args.get("equals"))

        if not poll(matches, float(args.get("within_s", EVENTUALLY_S))):
            self.fail(step, f"{args['sql']!r} is {value!r}, expected {args.get('equals')!r}")

    def expect_issues(self, step: int, args: dict) -> None:
        """fakecord's GitHub issues: one matching `issue` (`title_contains`,
        `labels`, `body_contains`, `body_lacks`, `comments`), eventually, and
        `issues`, how many there are."""
        wanted = args.get("issue") or {}

        def fits(issue: dict) -> bool:
            title, body = issue["title"].lower(), issue["body"]
            lacks = as_list(wanted.get("body_lacks"))
            return (
                all(str(n).lower() in title for n in as_list(wanted.get("title_contains")))
                and all(label in issue["labels"] for label in as_list(wanted.get("labels")))
                and all(str(n) in body for n in as_list(wanted.get("body_contains")))
                and not any(str(n).lower() in body.lower() for n in lacks)
                and len(issue["comments"]) == wanted.get("comments", len(issue["comments"]))
            )

        def issues() -> list[dict]:
            return stack.control("state")["issues"]

        if "issue" in args and not poll(lambda: any(fits(i) for i in issues()), FAULT_ISSUE_S):
            shown = [{k: i[k] for k in ("title", "labels", "comments")} for i in issues()]
            self.fail(step, f"no issue like {wanted}: {shown}")
        if "issues" in args and len(issues()) != args["issues"]:
            titles = [i["title"] for i in issues()]
            self.fail(step, f"{len(titles)} issues, expected {args['issues']}: {titles}")

    def expect_llm(self, step: int, args: dict, o: Observation, label: str) -> None:
        records = [r for r in stack.control("state")["llm"] if r["kind"] == "reply"]
        for name in as_list(args.get("tool_called")):
            if name in WEB_TOOLS:
                self.expect_web(step, name, o, label)
            elif not any(
                (r.get("tool_call") or {}).get("name") == name for r in records
            ) and not self.logged_tool_call(o, name):
                self.fail(step, f"{label}: no {name} tool call")
        offered = set()
        if records:
            offered = {t.get("type") for t in records[-1]["request"].get("tools", [])}
        for kind in as_list(args.get("tools_offered")):
            if kind not in offered:
                self.fail(step, f"{label}: the last reply request didn't offer {kind}")
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

    def logged_tool_call(self, o: Observation, name: str) -> bool:
        """The agent host's log line for a tool call in this run. Live models don't
        go through fakecord's stub, so this is the evidence in live mode."""
        return o.run_id is not None and stack.log_contains(
            "agent", f"job_id=reply:{o.run_id} tool={name} ", self.agent_log_start
        )

    def expect_web(self, step: int, name: str, o: Observation, label: str) -> None:
        """OpenRouter runs web tools itself, so the agent host's saved result (which
        counts them from OpenRouter's usage) is the evidence, in stub and live modes."""
        web = None
        if o.run_id is not None:
            with stack.connect() as db:
                row = db.execute(
                    "SELECT result->'web' FROM agent_host_jobs WHERE id = %s",
                    (f"reply:{o.run_id}",),
                ).fetchone()
            web = row[0] if row else None
        if not (web or {}).get(WEB_TOOLS[name]):
            self.fail(step, f"{label}: no {name} call (web use: {web}) [{o.summary()}]")

    def expect_reaction(self, step: int, wanted: dict, message: dict, label: str) -> None:
        emoji, present = wanted.get("emoji", "👀"), wanted.get("present", False)
        if "added" in wanted:
            added = any(
                r["message_id"] == message["id"] and r["emoji"] == emoji and r["action"] == "add"
                for r in stack.control("state")["reactions"]
            )
            if added != wanted["added"]:
                self.fail(step, f"{label}: {emoji} reaction added is {added}")

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
            elif kind == "thread":
                body = {
                    "from": self.named[args["from"]]["id"],
                    "name": args["name"],
                    "as": args["as"],
                }
                stack.control("threads", body)
                time.sleep(0.5)  # let the ingester store the thread before anyone talks in it
            elif kind == "expect":
                self.expect(i, args)
            elif kind == "exec":
                with stack.connect() as db:
                    db.execute(args)
            elif kind == "add_emoji":
                stack.control("emojis", args)
            elif kind == "remove_emoji":
                stack.control(f"emojis/{args}", None, "DELETE")
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
    stack.reset(
        config,
        {str(k): str(v) for k, v in (spec.get("env") or {}).items()},
        fixture,
        {str(k): str(v) for k, v in (spec.get("host_env") or {}).items()},
    )
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
