"""What happened to a message, stage by stage, with explicit completion barriers.

stored → classified → run → delivered (→ reactions, eventually). A message can
legitimately stop at any stage, so each wait knows when there's nothing more to
wait for. See "Assertions, by pipeline stage" in docs/kiki-sim-hld.md.
"""

import time
from dataclasses import dataclass, field

from .stack import Config, connect, control

TERMINAL = ("done", "failed", "superseded")
# The router classifies a pool at most POOL_MAX_WAIT_MS after its first message;
# past this it never will.
CLASSIFY_SETTLE_S = 8 + 3
RUN_TIMEOUT_S = 90


@dataclass
class Observation:
    message_id: str
    stored: bool = False
    not_classified: str | None = None  # why there's no classification, if none
    check: str | None = None
    outcome: str | None = None
    jev_choice: str | None = None
    jev_confidence: float | None = None
    jev_error: str | None = None
    conversation_id: int | None = None
    run_id: int | None = None
    run: str | None = None  # done / failed / superseded / none
    run_error: str | None = None
    replies: list[dict] = field(default_factory=list)
    # Pictures the run queued: {status, error, message} each, message being the
    # follow-up Kiki posted for it.
    drawings: list[dict] = field(default_factory=list)

    @property
    def reply(self) -> bool:
        return bool(self.replies)

    def summary(self) -> str:
        if not self.stored:
            return "not stored"
        if self.outcome is None:
            return f"not classified ({self.not_classified})"
        jev = ""
        if self.jev_choice is not None:
            jev = f" (jev {self.jev_choice} {self.jev_confidence:.2f})"
        elif self.jev_error:
            jev = f" (jev error: {self.jev_error})"
        conversation = f", conversation {self.conversation_id}" if self.conversation_id else ""
        run = ""
        if self.run_id:
            run = f" · run {self.run_id} {self.run}"
            if self.run_error:
                run += f": {self.run_error}"
            elif self.run == "done" and not self.replies:
                run += " (NO_REPLY)"
        return f"{self.check} → {self.outcome}{jev}{conversation}{run}"


def poll(check, timeout: float, interval: float = 0.1):
    deadline = time.monotonic() + timeout
    while True:
        value = check()
        if value or time.monotonic() > deadline:
            return value
        time.sleep(interval)


def observe(
    message: dict, config: Config, *, through: str = "delivered", env: dict | None = None
) -> Observation:
    o = Observation(message["id"])
    id = int(message["id"])
    with connect() as db:
        o.stored = bool(
            poll(lambda: db.execute("SELECT 1 FROM messages WHERE id = %s", (id,)).fetchone(), 10)
        )
        if not o.stored or through == "stored":
            return o
        if reason := never_classified(db, message, config, env or {}):
            o.not_classified = reason
            return o
        row = poll(
            lambda: db.execute(
                "SELECT check_hit, outcome, jev_choice, jev_confidence, jev_error, conversation_id"
                " FROM classifications WHERE message_id = %s",
                (id,),
            ).fetchone(),
            CLASSIFY_SETTLE_S,
        )
        if not row:
            o.not_classified = "no classification after the pool window (deleted or dropped?)"
            return o
        (o.check, o.outcome, o.jev_choice, o.jev_confidence, o.jev_error, o.conversation_id) = row
        if o.outcome == "no" or through == "classified":
            o.run = "none" if o.outcome == "no" else None
            return o
        run = poll(lambda: finished_run(db, id), RUN_TIMEOUT_S, 0.2)
        if not run:
            o.run = "timeout"
            return o
        o.run_id, o.run, o.run_error, reply_ids = run
        # Pictures the run queued are posted later, as their own messages.
        finished = poll(lambda: drawn(db, o.run_id), RUN_TIMEOUT_S, 0.2)
        if finished is None:
            o.drawings = [{"status": "timeout", "error": None, "message": None}]
        drawings = finished[0] if finished else []
        followups = {str(d[1]) for d in drawings if d[1]}
        if followups:
            reply_ids = db.execute(
                "SELECT reply_message_ids FROM agent_runs WHERE id = %s", (o.run_id,)
            ).fetchone()[0]
        wanted = {str(r) for r in reply_ids or []} | followups
        messages = []
        if wanted:
            messages = poll(
                lambda: [m for m in control("state")["messages"] if m["id"] in wanted], 5
            )
        by_id = {m["id"]: m for m in messages or []}
        o.replies = [m for id, m in by_id.items() if id not in followups]
        o.drawings += [
            {"status": status, "error": error, "message": by_id.get(str(message_id))}
            for status, message_id, error in drawings
        ]
    return o


def drawn(db, run_id: int):
    """`(rows,)`, the run's drawings, once all are finished (rows are empty if it
    queued none), else None."""
    rows = db.execute(
        "SELECT status, message_id, error FROM agent_image_jobs WHERE run_id = %s"
        " ORDER BY position",
        (run_id,),
    ).fetchall()
    if any(status not in ("done", "failed") for status, _, _ in rows):
        return None
    return (rows,)


def never_classified(db, message: dict, config: Config, env: dict) -> str | None:
    if message["author"].get("bot"):
        return "bot author"
    settings = config.env | env
    max_age_ms = int(settings.get("ROUTE_MAX_MESSAGE_AGE_MS", 5000))
    debounce_ms = int(settings.get("POOL_DEBOUNCE_MS", 500))
    if message.get("_age_s", 0) * 1000 + debounce_ms >= max_age_ms:
        return "too old to route"
    ignored = db.execute(
        "SELECT 1 FROM ignored_authors WHERE user_id = %s", (int(message["author"]["id"]),)
    ).fetchone()
    if ignored:
        return "ignored author"
    return None


def finished_run(db, message_id: int):
    """The run that answered this message, following supersession to the newer run."""
    row = db.execute(
        "SELECT id, conversation_id, status, error, reply_message_ids FROM agent_runs"
        " WHERE %s = ANY(trigger_message_ids) ORDER BY id DESC LIMIT 1",
        (message_id,),
    ).fetchone()
    while row and row[2] == "superseded":
        newer = db.execute(
            "SELECT id, conversation_id, status, error, reply_message_ids FROM agent_runs"
            " WHERE conversation_id = %s AND id > %s AND status <> 'superseded'"
            " ORDER BY id LIMIT 1",
            (row[1], row[0]),
        ).fetchone()
        if not newer:
            return None
        row = newer
    if not row or row[2] not in TERMINAL:
        return None
    return row[0], row[2], row[3], row[4]


def name_of(state: dict, user: dict) -> str:
    for key, member in state["members"].items():
        if member["id"] == user["id"]:
            return "Kiki" if key == "kiki" else key
    return user.get("username", user["id"])


def render(message: dict, state: dict) -> str:
    """One transcript line, with mentions shown as @names."""
    channels = {v: k for k, v in state["channels"].items()}
    text = message["content"]
    for key, member in state["members"].items():
        text = text.replace(f"<@{member['id']}>", "@" + ("Kiki" if key == "kiki" else key))
    reply = ""
    if message.get("referenced_message"):
        reply = f" ↩ {name_of(state, message['referenced_message']['author'])}"
    emoji = reactions(state, message["id"])
    files = "".join(f" [attachment: {a['filename']}]" for a in message.get("attachments") or [])
    flags = (" [deleted]" if message.get("deleted") else "") + (f" {emoji}" if emoji else "")
    text += files
    channel = channels.get(message["channel_id"], message["channel_id"])
    return f"[{channel}] {name_of(state, message['author'])}{reply}: {text}{flags}"


def reactions(state: dict, message_id: str) -> str:
    present: dict[str, bool] = {}
    for r in state["reactions"]:
        if r["message_id"] == message_id:
            present[r["emoji"]] = r["action"] == "add"
    return "".join(e for e, on in present.items() if on)
