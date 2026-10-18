"""kiki-sim: run Kiki against a fake Discord. See docs/kiki-sim-hld.md."""

import argparse
import json
import sys
import time
from pathlib import Path

from . import stack
from .observe import observe, render
from .scenario import discover, run_file


def main() -> None:
    parser = argparse.ArgumentParser(prog="kiki-sim", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("up", help="start the stack (fake Discord, agent host, ingester)")
    p.add_argument("--fixture", type=Path, default=stack.DEFAULT_FIXTURE)
    p.add_argument("--live", action="store_true", help="real OpenRouter (costs money)")
    p.add_argument(
        "--set",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="ingester setting, e.g. JEV_MIN_CONFIDENCE=0.3",
    )
    p = sub.add_parser("down", help="stop everything")
    p.add_argument("--purge", action="store_true", help="also remove the sim Postgres container")
    sub.add_parser("reset", help="fresh database and guild, same settings")

    for name, help in (
        ("say", "send a message and wait for what happens"),
        ("send", "send a message without waiting"),
    ):
        p = sub.add_parser(name, help=help)
        p.add_argument("--as", dest="author", required=True)
        p.add_argument("--in", dest="channel", default="general")
        p.add_argument("--reply-to", help="message id, or `kiki` for her latest message")
        p.add_argument("--age", type=float, default=0, help="seconds in the past")
        p.add_argument(
            "--image",
            action="append",
            default=[],
            metavar="FILE[=LABEL]",
            help="attach an image; the stub vision model describes it as LABEL (repeat: gallery)",
        )
        p.add_argument("text")

    p = sub.add_parser("slash", help="run /kiki SUBCOMMAND [name=value ...]")
    p.add_argument("--as", dest="author", required=True)
    p.add_argument("--in", dest="channel", default="general")
    p.add_argument("subcommand")
    p.add_argument("options", nargs="*")

    p = sub.add_parser("transcript", help="the guild as Discord would show it")
    p.add_argument("--in", dest="channel")
    p.add_argument("--all", action="store_true", help="include fixture history")
    p = sub.add_parser("inspect", help="the database rows behind a message")
    p.add_argument("message_id")
    p = sub.add_parser("logs")
    p.add_argument(
        "service", choices=["ingester", "agent", "fakecord"], default="ingester", nargs="?"
    )
    p.add_argument("-n", type=int, default=50)

    p = sub.add_parser("test", help="run scenarios (default: tools/sim/scenarios)")
    p.add_argument("paths", nargs="*", type=Path)
    p.add_argument("--live", action="store_true", help="run live-marked scenarios for real")
    p.add_argument("-k", dest="keyword", help="only scenarios whose file name contains this")

    args = parser.parse_args()
    try:
        sys.exit(COMMANDS[args.command](args) or 0)
    except stack.SimError as e:
        print(f"kiki-sim: {e}", file=sys.stderr)
        sys.exit(2)


def cmd_up(args) -> None:
    env = dict(item.split("=", 1) for item in args.set)
    config = stack.Config(args.fixture.resolve(), args.live, env)
    stack.up(config)
    mode = "live OpenRouter" if args.live else "stub LLMs"
    print(f"kiki-sim is up ({mode}, fixture {args.fixture.name}). Logs: .sim/logs/")


def cmd_down(args) -> None:
    stack.down(args.purge)
    print("kiki-sim is down")


def cmd_reset(args) -> None:
    stack.reset(stack.Config.load())
    print("reset: fresh database and guild")


def cmd_say(args, wait: bool = True) -> None:
    config = stack.Config.load()
    body = {"as": args.author, "in": args.channel, "text": args.text, "age_s": args.age}
    if args.reply_to:
        body["reply_to"] = resolve_reply(args.reply_to, args.channel)
    if args.image:
        body["attachments"] = [
            {"filename": f, "label": label or f}
            for f, _, label in (i.partition("=") for i in args.image)
        ]
    message = stack.control("messages", body)
    message["_age_s"] = args.age
    state = stack.control("state")
    print(render(message, state))
    if not wait:
        print(f"  sent {message['id']}")
        return
    o = observe(message, config)
    print(f"  {o.summary()}")
    state = stack.control("state")
    for reply in o.replies:
        print(render(reply, state))


def cmd_send(args) -> None:
    cmd_say(args, wait=False)


def resolve_reply(target: str, channel: str) -> str:
    if target != "kiki":
        return target
    state = stack.control("state")
    ours = [
        m
        for m in state["messages"]
        if m["author"]["id"] == state["bot"]["id"] and m["channel_id"] == state["channels"][channel]
    ]
    if not ours:
        raise stack.SimError(f"Kiki hasn't said anything in #{channel}")
    return ours[-1]["id"]


def cmd_slash(args) -> None:
    options = dict(o.split("=", 1) for o in args.options)
    sent = stack.control(
        "interactions",
        {"as": args.author, "in": args.channel, "command": args.subcommand, "options": options},
    )
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        for i in stack.control("state")["interactions"]:
            if i["interaction_id"] == sent["id"] and i["kind"] == "response":
                print(json.dumps(i["body"], indent=2, ensure_ascii=False))
                return
        time.sleep(0.2)
    raise stack.SimError("no response to the interaction")


def cmd_transcript(args) -> None:
    state = stack.control("state")
    channel = state["channels"].get(args.channel) if args.channel else None
    now = time.time()
    for m in state["messages"]:
        if channel and m["channel_id"] != channel:
            continue
        if not args.all and is_history(m, now):
            continue
        print(render(m, state))


def is_history(message: dict, now: float) -> bool:
    from datetime import datetime

    return now - datetime.fromisoformat(message["timestamp"]).timestamp() > 3600


def cmd_inspect(args) -> None:
    with stack.connect() as db:
        for title, sql in (
            (
                "message",
                "SELECT id, channel_id, author_name, content, created_at, deleted_at"
                " FROM messages WHERE id = %s",
            ),
            ("classification", "SELECT * FROM classifications WHERE message_id = %s"),
            (
                "conversation",
                "SELECT cm.via, c.* FROM conversation_messages cm JOIN"
                " conversations c ON c.id = cm.conversation_id"
                " WHERE cm.message_id = %s",
            ),
            (
                "runs",
                "SELECT id, status, error, reply_message_ids, model, steps, cost_usd,"
                " latency_ms FROM agent_runs WHERE %s = ANY(trigger_message_ids)",
            ),
        ):
            cursor = db.execute(sql, (int(args.message_id),))
            columns = [c.name for c in cursor.description]
            rows = cursor.fetchall()
            print(f"== {title}")
            for row in rows:
                for column, value in zip(columns, row, strict=True):
                    print(f"  {column}: {value}")
            if not rows:
                print("  (none)")


def cmd_logs(args) -> None:
    print(stack.tail(args.service, args.n))


def cmd_test(args) -> int:
    config = stack.Config.load()
    paths = discover(args.paths or [stack.SIM / "scenarios"])
    if args.keyword:
        paths = [p for p in paths if args.keyword in p.stem]
    if args.live and not config.live:
        raise stack.SimError("the stack is in stub mode: run `kiki-sim up --live` first")
    if not args.live and config.live:
        raise stack.SimError("the stack is live: run `kiki-sim up` for stub scenarios")
    results = []
    for path in paths:
        result = run_file(path, config, args.live)
        results.append(result)
        if result.status == "skip":
            continue
        print(f"{result.status.upper():4} {path.stem} ({result.seconds:.1f}s)")
        if result.status == "fail" or args.live:
            for failure in result.failures:
                print(f"     ✗ {failure}")
            for line in result.transcript:
                print(f"     | {line}")
        if result.status == "fail":
            print("     -- ingester log --")
            for line in stack.tail("ingester", 30).splitlines():
                print(f"     {line[:300]}")
    passed = sum(r.status == "pass" for r in results)
    failed = sum(r.status == "fail" for r in results)
    skipped = sum(r.status == "skip" for r in results)
    print(f"{passed} passed, {failed} failed, {skipped} skipped")
    return 1 if failed else 0


COMMANDS = {
    "up": cmd_up,
    "down": cmd_down,
    "reset": cmd_reset,
    "say": cmd_say,
    "send": cmd_send,
    "slash": cmd_slash,
    "transcript": cmd_transcript,
    "inspect": cmd_inspect,
    "logs": cmd_logs,
    "test": cmd_test,
}


if __name__ == "__main__":
    main()
