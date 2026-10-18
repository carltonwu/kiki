"""fakecord: a fake Discord (Gateway + the REST calls Kiki makes) and a stub
OpenRouter (Jev, chat completions, embeddings, models), in one aiohttp app.

Run: python -m kiki_sim.fakecord --port 8090 --fixture fixtures/minimal.yaml
The CLI drives it through `/_sim/…`; see docs/kiki-sim-hld.md.
"""

import argparse
import asyncio
import base64
import hashlib
import itertools
import json
import logging
import math
import re
import struct
import time
import zlib
from datetime import UTC, datetime
from pathlib import Path

import yaml
from aiohttp import WSMsgType, web

log = logging.getLogger("fakecord")

DISCORD_EPOCH_MS = 1420070400000
EMBEDDING_DIMENSIONS = 1024
ADMINISTRATOR = 1 << 3
MENTION_TOKEN = re.compile(r"(?<![\w<])@([a-z0-9_.]+)", re.IGNORECASE)
STUB_MODELS = ["sim/chat", "sim/other"]
STUB_IMAGE_MODELS = ["sim/image"]
# A 1×1 transparent PNG: what the stub image model "draws".
STUB_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)


def iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).isoformat(timespec="milliseconds")


class World:
    """Everything fakecord knows, rebuilt from the fixture on reset."""

    def __init__(self, fixture_path: Path):
        fixture = yaml.safe_load(fixture_path.read_text())
        self.fixture_path = fixture_path
        self.guild = {"id": str(fixture["guild"]["id"]), "name": fixture["guild"]["name"]}
        kiki = fixture["kiki"]
        self.bot = {
            "id": str(kiki["id"]),
            "username": kiki["username"],
            "global_name": None,
            "discriminator": "0",
            "avatar": None,
            "bot": True,
        }
        self.application_id = str(kiki.get("application_id", kiki["id"]))
        self.bot_role_id = str(kiki["role_id"])
        self.members: dict[str, dict] = {}
        for key, m in fixture["members"].items():
            self.members[key] = {
                "user": {
                    "id": str(m["id"]),
                    "username": m["username"],
                    "global_name": m.get("global_name"),
                    "discriminator": "0",
                    "avatar": None,
                    "bot": bool(m.get("bot", False)),
                },
                "nick": m.get("nick"),
                "admin": bool(m.get("admin", False)),
            }
        self.members["kiki"] = {"user": self.bot, "nick": None, "admin": False}
        self.channels: dict[str, dict] = {}
        for name, c in fixture["channels"].items():
            channel = {
                "id": str(c["id"]),
                "guild_id": self.guild["id"],
                "name": name,
                "type": c.get("type", 0),
                "parent_id": None,
                "last_message_id": None,
            }
            if c.get("parent"):
                channel["parent_id"] = str(fixture["channels"][c["parent"]]["id"])
            if channel["type"] in (10, 11, 12):
                channel["thread_metadata"] = {"archived": False, "archive_timestamp": iso(0)}
            self.channels[name] = channel
        # Custom emoji: `description` is what the stub vision model answers.
        self.emojis: dict[str, dict] = {}
        for name, e in (fixture.get("emojis") or {}).items():
            self.add_emoji(name, e)
        self.messages: dict[str, dict] = {}  # id → payload, insertion (= id) order
        self.deleted: set[str] = set()
        self.reactions: list[dict] = []  # {message_id, emoji, action, at}
        self.interactions: list[dict] = []  # responses Kiki sent
        self.calls: list[dict] = []  # every Discord REST call
        self.unexpected: list[dict] = []
        self.llm: list[dict] = []  # every OpenRouter request, with what we answered
        self.scripts: dict[str, list[dict]] = {"jev": [], "reply": []}
        self.guild_creates = 0
        self.identifies = 0
        self.last_snowflake = 0
        self.ids = itertools.count(1)
        now = time.time() * 1000
        for h in fixture.get("history", []):
            age_ms = (h.get("days_ago", 0) * 86400 + h.get("minutes_ago", 0) * 60) * 1000
            # `repeat: n` writes n messages one second apart; `{i}` numbers them.
            count = h.get("repeat", 1)
            for i in range(count):
                at_ms = now - age_ms - (count - 1 - i) * 1000
                self.create_message(h["as"], h["in"], h["text"].format(i=i), at_ms=at_ms)

    # --- lookups -----------------------------------------------------------

    def member(self, key: str) -> dict:
        if key in self.members:
            return self.members[key]
        for m in self.members.values():
            if key in (m["user"]["id"], m["user"]["username"]):
                return m
        raise web.HTTPBadRequest(text=f"unknown member {key!r}")

    def channel(self, key: str) -> dict:
        if key in self.channels:
            return self.channels[key]
        for c in self.channels.values():
            if c["id"] == key:
                return c
        raise web.HTTPBadRequest(text=f"unknown channel {key!r}")

    def snowflake(self, at_ms: float) -> str:
        """Minted from the message's own time, strictly increasing."""
        value = max((int(at_ms) - DISCORD_EPOCH_MS) << 22, self.last_snowflake + 1)
        self.last_snowflake = value
        return str(value)

    def member_payload(self, m: dict) -> dict:
        return {
            "nick": m["nick"],
            "roles": [self.bot_role_id] if m["user"]["id"] == self.bot["id"] else [],
            "joined_at": iso(DISCORD_EPOCH_MS + 86400000),
            "permissions": str(ADMINISTRATOR if m["admin"] else 0),
        }

    def add_emoji(self, name: str, spec: dict) -> dict:
        emoji = {
            "id": str(spec["id"]),
            "name": name,
            "animated": bool(spec.get("animated", False)),
            "available": bool(spec.get("available", True)),
            "require_colons": True,
            "managed": False,
            "roles": [],
            "description": spec.get("description"),
        }
        self.emojis[emoji["id"]] = emoji
        return emoji

    def emoji_payloads(self) -> list[dict]:
        return [{k: v for k, v in e.items() if k != "description"} for e in self.emojis.values()]

    # --- messages ----------------------------------------------------------

    def create_message(
        self,
        author: str,
        channel: str,
        content: str,
        *,
        at_ms: float | None = None,
        reply_to: str | None = None,
        attachments: list[dict] | None = None,
    ) -> dict:
        m = self.member(author)
        c = self.channel(channel)
        at_ms = time.time() * 1000 if at_ms is None else at_ms
        mentions = []

        def mention(match: re.Match) -> str:
            try:
                target = self.member(match[1].lower())
            except web.HTTPBadRequest:
                return match[0]
            if target["user"] not in mentions:
                mentions.append(target["user"])
            return f"<@{target['user']['id']}>"

        content = MENTION_TOKEN.sub(mention, content)
        message = {
            "id": self.snowflake(at_ms),
            "channel_id": c["id"],
            "guild_id": self.guild["id"],
            "author": m["user"],
            "content": content,
            "timestamp": iso(at_ms),
            "edited_timestamp": None,
            "tts": False,
            "mention_everyone": False,
            "mentions": mentions,
            "mention_roles": [],
            "attachments": attachments or [],
            "embeds": [],
            "pinned": False,
            "type": 0,
            "flags": 0,
        }
        if not m["user"].get("bot") or m["user"]["id"] == self.bot["id"]:
            message["member"] = self.member_payload(m)
        if reply_to:
            parent = self.messages[reply_to]
            message["type"] = 19
            message["message_reference"] = {
                "message_id": parent["id"],
                "channel_id": parent["channel_id"],
                "guild_id": self.guild["id"],
            }
            message["referenced_message"] = {k: v for k, v in parent.items() if k != "member"}
            # Replies ping the replied-to author by default.
            if parent["author"] not in mentions:
                mentions.append(parent["author"])
        self.messages[message["id"]] = message
        c["last_message_id"] = message["id"]
        return message


class Fakecord:
    def __init__(self, fixture: Path, port: int):
        self.port = port
        self.world = World(fixture)
        self.sockets: set[web.WebSocketResponse] = set()
        self.identified: set[web.WebSocketResponse] = set()
        self.sequence = 0
        self.sessions = itertools.count(1)

    # --- Gateway -----------------------------------------------------------

    async def gateway(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(max_msg_size=0)
        await ws.prepare(request)
        self.sockets.add(ws)
        await ws.send_json({"op": 10, "d": {"heartbeat_interval": 41250}})
        try:
            async for frame in ws:
                if frame.type != WSMsgType.TEXT:
                    continue
                payload = json.loads(frame.data)
                op = payload.get("op")
                if op == 1:
                    await ws.send_json({"op": 11})
                elif op == 2:
                    await self.identify(ws)
                elif op == 6:
                    # No real resumes: re-identify, which also re-runs catch-up.
                    await ws.send_json({"op": 9, "d": False})
        finally:
            self.sockets.discard(ws)
            self.identified.discard(ws)
        return ws

    async def identify(self, ws: web.WebSocketResponse) -> None:
        w = self.world
        w.identifies += 1
        session = f"sim-session-{next(self.sessions)}"
        await self.send(
            ws,
            "READY",
            {
                "v": 10,
                "user": w.bot,
                "guilds": [{"id": w.guild["id"], "unavailable": True}],
                "session_id": session,
                "resume_gateway_url": f"ws://127.0.0.1:{self.port}/gateway",
                "application": {"id": w.application_id, "flags": 0},
                "shard": [0, 1],
            },
        )
        self.identified.add(ws)
        channels = [c for c in w.channels.values() if c["type"] not in (10, 11, 12)]
        threads = [c for c in w.channels.values() if c["type"] in (10, 11, 12)]
        await self.send(
            ws,
            "GUILD_CREATE",
            {
                **w.guild,
                "unavailable": False,
                "channels": channels,
                "threads": threads,
                "roles": self.roles(),
                "members": [],
            },
        )
        w.guild_creates += 1

    async def send(self, ws: web.WebSocketResponse, event: str, data: dict) -> None:
        self.sequence += 1
        await ws.send_json({"op": 0, "t": event, "s": self.sequence, "d": data})

    async def dispatch(self, event: str, data: dict) -> None:
        for ws in list(self.identified):
            try:
                await self.send(ws, event, data)
            except ConnectionError:
                self.identified.discard(ws)

    def roles(self) -> list[dict]:
        w = self.world
        role = {"color": 0, "hoist": False, "mentionable": False, "flags": 0, "permissions": "0"}
        return [
            {**role, "id": w.guild["id"], "name": "@everyone", "position": 0, "managed": False},
            {
                **role,
                "id": w.bot_role_id,
                "name": w.bot["username"],
                "position": 1,
                "managed": True,
                "tags": {"bot_id": w.bot["id"]},
            },
        ]

    # --- Discord REST ------------------------------------------------------

    async def rest(self, request: web.Request) -> web.StreamResponse:
        w = self.world
        path = request.match_info["path"].rstrip("/")
        files: list[dict] = []
        if request.content_type == "multipart/form-data":
            body, files = await self.multipart(request)
        else:
            body = await request.json() if request.can_read_body else None
        call = {"method": request.method, "path": path, "query": dict(request.query)}
        w.calls.append({**call, "body": body})
        route = f"{request.method} {path}"
        g = w.guild["id"]

        def json_response(value, status=200):
            return web.json_response(value, status=status)

        if route == "GET users/@me":
            return json_response(w.bot)
        if route == f"GET guilds/{g}/roles":
            return json_response(self.roles())
        if route in ("GET applications/@me", "GET oauth2/applications/@me"):
            return json_response({"id": w.application_id, "name": w.bot["username"]})
        if re.fullmatch(rf"PUT applications/{w.application_id}/guilds/{g}/commands", route):
            commands = [{**c, "id": str(900000000000009000 + i)} for i, c in enumerate(body)]
            return json_response(commands)
        if route == f"GET guilds/{g}":
            return json_response({**w.guild, "roles": self.roles()})
        if route == f"GET guilds/{g}/emojis":
            return json_response(w.emoji_payloads())
        if route == f"GET guilds/{g}/channels":
            return json_response([c for c in w.channels.values() if c["type"] not in (10, 11, 12)])
        if route == f"GET guilds/{g}/threads/active":
            threads = [c for c in w.channels.values() if c["type"] in (10, 11, 12)]
            return json_response({"threads": threads, "members": []})
        if m := re.fullmatch(r"GET channels/(\d+)/threads/archived/(public|private)", route):
            return json_response({"threads": [], "members": [], "has_more": False})
        if m := re.fullmatch(r"GET channels/(\d+)/messages", route):
            return json_response(self.page(m[1], request.query))
        if m := re.fullmatch(
            r"(PUT|DELETE) channels/(\d+)/messages/(\d+)/reactions/(.+)/@me", route
        ):
            from urllib.parse import unquote

            emoji = unquote(m[4])
            # Custom emoji are `name:id`; Discord rejects ones Kiki can't use.
            if ":" in emoji and emoji.rsplit(":", 1)[1] not in w.emojis:
                return json_response({"message": "Unknown Emoji", "code": 10014}, status=400)
            w.reactions.append(
                {
                    "message_id": m[3],
                    "emoji": emoji,
                    "action": "add" if m[1] == "PUT" else "remove",
                    "at": time.time(),
                }
            )
            return web.Response(status=204)
        if m := re.fullmatch(r"POST channels/(\d+)/messages", route):
            reference = (body.get("message_reference") or {}).get("message_id")
            reply_to = reference if reference in w.messages else None
            message = w.create_message(
                "kiki",
                m[1],
                body.get("content", ""),
                reply_to=reply_to,
                attachments=self.attachments(m[1], body, files),
            )
            # Discord echoes our own message over the Gateway, usually a bit later.
            asyncio.get_running_loop().call_later(
                0.05, lambda: asyncio.ensure_future(self.dispatch("MESSAGE_CREATE", message))
            )
            return json_response(message)
        if m := re.fullmatch(r"POST interactions/(\d+)/([^/]+)/callback", route):
            w.interactions.append({"interaction_id": m[1], "kind": "callback", "body": body})
            return web.Response(status=204)
        if m := re.fullmatch(
            rf"PATCH webhooks/{w.application_id}/([^/]+)/messages/@original", route
        ):
            interaction = next((i for i in reversed(w.interactions) if i.get("token") == m[1]), {})
            w.interactions.append(
                {
                    "interaction_id": interaction.get("interaction_id"),
                    "kind": "response",
                    "body": body,
                }
            )
            return json_response({"id": w.snowflake(time.time() * 1000), **(body or {})})

        log.warning("unexpected Discord call: %s", route)
        w.unexpected.append(call)
        return json_response({"message": "Unknown (kiki-sim)", "code": 0}, status=404)

    async def multipart(self, request: web.Request) -> tuple[dict, list[dict]]:
        """Discord uploads: `payload_json` plus `files[n]` parts."""
        body, files = {}, []
        reader = await request.multipart()
        while part := await reader.next():
            data = await part.read()
            if part.name == "payload_json":
                body = json.loads(data)
            elif part.name and part.name.startswith("files["):
                files.append(
                    {
                        "id": part.name[len("files[") : -1],
                        "filename": part.filename,
                        "content_type": part.headers.get("Content-Type"),
                        "data": bytes(data),
                    }
                )
        return body, files

    def attachments(self, channel_id: str, body: dict, files: list[dict]) -> list[dict]:
        """Discord's attachment objects for uploaded files, declared in `attachments`."""
        declared = {str(a["id"]): a for a in body.get("attachments") or []}
        if set(declared) != {f["id"] for f in files}:
            raise web.HTTPBadRequest(text="uploaded files don't match payload attachments")
        found = []
        for f in files:
            id = self.world.snowflake(time.time() * 1000)
            url = f"https://cdn.discordapp.com/attachments/{channel_id}/{id}/{f['filename']}"
            found.append(
                {
                    "id": id,
                    "filename": declared[f["id"]].get("filename") or f["filename"],
                    "size": len(f["data"]),
                    "url": url,
                    "proxy_url": url,
                    "content_type": f["content_type"] or sniff(f["data"]),
                    "sha256": hashlib.sha256(f["data"]).hexdigest(),
                }
            )
        return found

    def page(self, channel_id: str, query) -> list[dict]:
        w = self.world
        after = int(query.get("after", "0"))
        limit = min(int(query.get("limit", "50")), 100)
        found = [
            m
            for id, m in w.messages.items()
            if m["channel_id"] == channel_id and int(id) > after and id not in w.deleted
        ][:limit]
        return list(reversed(found))  # Discord returns newest first

    # --- stub OpenRouter ---------------------------------------------------

    async def openrouter(self, request: web.Request) -> web.StreamResponse:
        path = request.match_info["path"].rstrip("/")
        body = await request.json() if request.can_read_body else None
        if request.method == "GET" and path == "v1/models":
            return web.json_response(
                {
                    "data": [
                        {
                            "id": model,
                            "name": model,
                            "architecture": {"output_modalities": ["text"]},
                            "supported_parameters": ["tools"],
                            "pricing": {"prompt": "0", "completion": "0"},
                        }
                        for model in STUB_MODELS
                    ]
                }
            )
        if request.method == "GET" and path == "v1/images/models":
            return web.json_response(
                {
                    "data": [
                        {
                            "id": model,
                            "name": model,
                            "architecture": {
                                "input_modalities": ["text", "image"],
                                "output_modalities": ["image"],
                            },
                        }
                        for model in STUB_IMAGE_MODELS
                    ]
                }
            )
        if request.method == "POST" and path == "v1/images":
            return self.image(body)
        if request.method == "POST" and path == "alpha/decisions":
            return web.json_response(self.jev(body))
        if request.method == "POST" and path == "v1/chat/completions":
            return web.json_response(self.chat(body))
        if request.method == "POST" and path == "v1/embeddings":
            return web.json_response(embeddings(body))
        self.world.unexpected.append({"method": request.method, "path": f"openrouter/{path}"})
        return web.json_response({"error": {"code": 404, "message": "kiki-sim"}}, status=404)

    def take(self, kind: str, text: str) -> dict | None:
        for entry in self.world.scripts[kind]:
            if not entry.get("used") and re.search(entry["match"], text, re.IGNORECASE | re.S):
                entry["used"] = True
                return entry
        return None

    async def cdn(self, request: web.Request) -> web.StreamResponse:
        """Discord's CDN, for emoji images: a small PNG per emoji."""
        path = request.match_info["path"]
        if (m := re.fullmatch(r"emojis/(\d+)\.png", path)) and m[1] in self.world.emojis:
            return web.Response(body=png(int(m[1])), content_type="image/png")
        self.world.unexpected.append({"method": request.method, "path": f"cdn/{path}"})
        return web.Response(status=404)

    def jev(self, body: dict) -> dict:
        state = body["state"]
        new = state["new_messages"]
        text = "\n".join(new["texts"])
        criteria = body["questions"]["intent"]["criteria"]
        conversations = sorted(
            (c for c in criteria if c.startswith("conversation_")),
            key=lambda c: int(c.removeprefix("conversation_")),
        )
        scripted = self.take("jev", text)
        if scripted:
            choice, confidence = scripted["choice"], scripted.get("confidence", 0.9)
            if choice == "conversation_latest":
                choice = conversations[-1] if conversations else "new"
        else:
            choice, confidence = self.default_jev(state, conversations, text), 0.9
        self.world.llm.append(
            {
                "kind": "jev",
                "texts": new["texts"],
                "choice": choice,
                "confidence": confidence,
                "scripted": bool(scripted),
                "request": body,
            }
        )
        return {
            "model": body.get("model", "sim/jev"),
            "answers": {"intent": {"choice": choice, "confidence": confidence}},
            "usage": {"cost": 0.00001},
        }

    def default_jev(self, state: dict, conversations: list[str], text: str) -> str:
        """Talking to Kiki when named or when answering her latest message."""
        speaker = state["new_messages"]["from"]
        mine = [
            f"conversation_{c['id']}"
            for c in state.get("conversations", [])
            if speaker in c.get("participants", [])
        ]
        mine = [c for c in conversations if c in mine]
        if re.search(r"\bkiki\b", text, re.IGNORECASE):
            return mine[-1] if mine else "new"
        recent = state.get("recent_channel_messages") or []
        if recent and recent[-1].get("from") == "Kiki" and mine:
            return mine[-1]
        return "no"

    def chat(self, body: dict) -> dict:
        messages = body["messages"]
        w = self.world
        n = next(w.ids)
        record = {"kind": "topic" if not body.get("tools") else "reply", "request": body}
        usage = {}
        if has_image(messages):
            # Describing a custom emoji: the fixture's description, by name.
            record["kind"] = "emoji"
            prompt = " ".join(content_text(m.get("content")) for m in messages)
            name = (re.search(r":(\w+):", prompt) or [None, ""])[1]
            emoji = next((e for e in w.emojis.values() if e["name"] == name), {})
            text = emoji.get("description") or f"stub description of :{name}:"
            message = {"role": "assistant", "content": text}
        elif record["kind"] == "topic":
            message = {"role": "assistant", "content": "stub topic"}
        else:
            user = [m for m in messages if m["role"] == "user"]
            prompt = content_text(user[-1]["content"]) if user else ""
            trigger = trigger_line(prompt)
            record["trigger"] = trigger
            tool_results = [content_text(m.get("content")) for m in messages if m["role"] == "tool"]
            if messages[-1]["role"] == "tool":
                entry = next((e for e in w.scripts["reply"] if e.get("pending")), {})
                queue = entry.get("queue") or []
                record["tool_results"] = tool_results
                if queue:
                    # A scripted sequence: the next call may use IDs from this result.
                    tool = fill_args(queue.pop(0), tool_results[-1])
                    message = tool_call_message(n, tool)
                    record["tool_call"] = tool
                else:
                    # Last turn of a scripted tool call: answer from its result.
                    entry.pop("pending", None)
                    ids = re.findall(r'"id":\s*"(\d+)"', tool_results[-1])
                    text = entry.get("text", "stub: found {ids}").format(
                        trigger=trigger, ids=" ".join(ids), result=tool_results[-1][:500]
                    )
                    message = {"role": "assistant", "content": text}
            else:
                entry = self.take("reply", trigger)
                tools = (entry or {}).get("tools") or (
                    [entry["tool"]] if entry and entry.get("tool") else []
                )
                if tools:
                    entry["pending"] = True
                    entry["queue"] = list(tools[1:])
                    message = tool_call_message(n, tools[0])
                    record["tool_call"] = tools[0]
                elif entry and entry.get("web"):
                    message, usage = self.server_tools(body, entry, trigger)
                    record["web"] = usage["server_tool_use_details"]
                elif entry:
                    message = {
                        "role": "assistant",
                        "content": entry["text"].format(trigger=trigger, ids="", result=""),
                    }
                else:
                    message = {"role": "assistant", "content": f"stub: {trigger}"}
        record["answer"] = message
        w.llm.append(record)
        return {
            "id": f"gen-sim-{n}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": body.get("model", "sim/chat"),
            "provider": "Sim",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "tool_calls" if message.get("tool_calls") else "stop",
                    "logprobs": None,
                    "message": {"refusal": None, **message},
                }
            ],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "total_tokens": 15,
                "cost": 0.0001,
                **usage,
            },
        }

    def server_tools(self, body: dict, entry: dict, trigger: str) -> tuple[dict, dict]:
        """OpenRouter's server tools: it searches and fetches itself, then reports
        counts in usage and search sources as `url_citation` annotations. A scripted
        `web: [{search: q, source: url} | {fetch: url}]` only runs if it was offered."""
        offered = {t.get("type") for t in body.get("tools", [])}
        searches = fetches = 0
        sources = []
        for call in entry["web"]:
            if "search" in call and "openrouter:web_search" in offered:
                searches += 1
                sources.append(call.get("source") or "https://example.com/search")
            elif "fetch" in call and "openrouter:web_fetch" in offered:
                fetches += 1
        annotations = [
            {
                "type": "url_citation",
                "url_citation": {"url": url, "title": url, "start_index": 0, "end_index": 0},
            }
            for url in sources
        ]
        text = entry.get("text", "stub web: {trigger}")
        message = {
            "role": "assistant",
            "content": text.format(trigger=trigger, ids="", result=" ".join(sources)),
            "annotations": annotations,
        }
        used = searches + fetches
        details = {
            "web_search_requests": searches,
            "tool_calls_requested": used,
            "tool_calls_executed": used,
        }
        return message, {"server_tool_use_details": details}

    def image(self, body: dict) -> web.Response:
        """OpenRouter's Images API: draws STUB_PNG, or rejects prompts containing
        "refuse" with a 400 as a moderation block would."""
        w = self.world
        prompt = body["prompt"]
        drew = not re.search(r"\brefuse\b", prompt, re.IGNORECASE)
        w.llm.append({"kind": "image", "request": body, "prompt": prompt, "drew": drew})
        if not drew:
            error = {"code": 400, "message": "Request blocked by the provider's safety system"}
            return web.json_response({"error": error}, status=400)
        image = {"b64_json": base64.b64encode(STUB_PNG).decode(), "media_type": "image/png"}
        usage = {"prompt_tokens": 10, "completion_tokens": 1056, "total_tokens": 1066, "cost": 0.04}
        return web.json_response({"created": int(time.time()), "data": [image], "usage": usage})

    # --- control API -------------------------------------------------------

    async def control(self, request: web.Request) -> web.StreamResponse:
        w = self.world
        path = request.match_info["path"].rstrip("/")
        body = await request.json() if request.can_read_body else {}
        route = f"{request.method} {path}"
        if route == "GET state":
            return web.json_response(self.state())
        if route == "POST reset":
            fixture = Path(body.get("fixture") or w.fixture_path)
            self.world = World(fixture)
            self.disconnect()
            return web.json_response({"ok": True})
        if route == "POST drop-gateway":
            self.disconnect()
            return web.json_response({"ok": True})
        if route == "POST script":
            for kind in ("jev", "reply"):
                w.scripts[kind].extend(body.get(kind, []))
            return web.json_response({"ok": True})
        if route == "POST messages":
            at_ms = time.time() * 1000 - float(body.get("age_s", 0)) * 1000
            message = w.create_message(
                body["as"], body["in"], body["text"], at_ms=at_ms, reply_to=body.get("reply_to")
            )
            await self.dispatch("MESSAGE_CREATE", message)
            return web.json_response(message)
        if m := re.fullmatch(r"(PATCH|DELETE) messages/(\d+)", route):
            message = w.messages[m[2]]
            if m[1] == "DELETE":
                w.deleted.add(m[2])
                await self.dispatch(
                    "MESSAGE_DELETE",
                    {"id": m[2], "channel_id": message["channel_id"], "guild_id": w.guild["id"]},
                )
                return web.json_response({"ok": True})
            message["content"] = body["text"]
            message["edited_timestamp"] = iso(time.time() * 1000)
            await self.dispatch("MESSAGE_UPDATE", message)
            return web.json_response(message)
        if route == "POST emojis":
            # A server admin adds an emoji (no Gateway event: Kiki resyncs by command).
            return web.json_response(w.add_emoji(body["name"], body))
        if m := re.fullmatch(r"DELETE emojis/(\w+)", route):
            w.emojis = {k: e for k, e in w.emojis.items() if e["name"] != m[1]}
            return web.json_response({"ok": True})
        if route == "POST interactions":
            return web.json_response(await self.interaction(body))
        raise web.HTTPNotFound(text=f"no control route {route}")

    def disconnect(self) -> None:
        """Closes every Gateway socket without waiting for a peer that may be gone."""
        self.identified.clear()
        for ws in list(self.sockets):
            self.sockets.discard(ws)
            task = asyncio.ensure_future(asyncio.wait_for(ws.close(code=4000), 1))
            task.add_done_callback(lambda t: t.exception())

    async def interaction(self, body: dict) -> dict:
        w = self.world
        m = w.member(body["as"])
        c = w.channel(body["in"])
        id = w.snowflake(time.time() * 1000)
        token = f"sim-interaction-token-{id}"
        options = [
            {"name": k, "type": 3, "value": v} for k, v in (body.get("options") or {}).items()
        ]
        payload = {
            "id": id,
            "application_id": w.application_id,
            "type": 2,
            "token": token,
            "version": 1,
            "guild_id": w.guild["id"],
            "channel_id": c["id"],
            "member": {**w.member_payload(m), "user": m["user"]},
            "data": {
                "id": "900000000000009000",
                "name": "kiki",
                "type": 1,
                "options": [{"name": body["command"], "type": 1, "options": options}],
                "resolved": {"users": {m["user"]["id"]: m["user"]}},
            },
        }
        w.interactions.append({"interaction_id": id, "kind": "sent", "token": token})
        await self.dispatch("INTERACTION_CREATE", payload)
        return {"id": id, "token": token}

    def state(self) -> dict:
        w = self.world
        return {
            "guild": w.guild,
            "bot": w.bot,
            "members": {k: m["user"] for k, m in w.members.items()},
            "channels": {k: c["id"] for k, c in w.channels.items()},
            "messages": [{**m, "deleted": id in w.deleted} for id, m in w.messages.items()],
            "reactions": w.reactions,
            "interactions": w.interactions,
            "calls": w.calls,
            "unexpected": w.unexpected,
            "llm": w.llm,
            "unused_scripts": [
                {"kind": kind, **{k: v for k, v in e.items() if k != "used"}}
                for kind, entries in w.scripts.items()
                for e in entries
                if not e.get("used")
            ],
            "identifies": w.identifies,
            "guild_creates": w.guild_creates,
            "connected": len(self.identified),
        }

    def app(self) -> web.Application:
        app = web.Application(client_max_size=16 * 1024 * 1024)
        app.router.add_get("/gateway", self.gateway)
        app.router.add_get("/gateway/", self.gateway)
        app.router.add_route("*", "/api/v10/{path:.*}", self.rest)
        app.router.add_route("*", "/openrouter/{path:.*}", self.openrouter)
        app.router.add_route("*", "/cdn/{path:.*}", self.cdn)
        app.router.add_route("*", "/_sim/{path:.*}", self.control)
        return app


def tool_call_message(n: int, tool: dict) -> dict:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": f"call_{n}",
                "type": "function",
                "function": {"name": tool["name"], "arguments": json.dumps(tool.get("args", {}))},
            }
        ],
    }


def fill_args(tool: dict, previous: str) -> dict:
    """`{entry_id}` is the first memory entry ID (e12) in the previous tool result;
    `{entry_ids:N}` the first N, as a list."""
    entries = list(dict.fromkeys(re.findall(r"\be[0-9]+\b", previous)))

    def fill(value):
        if value == "{entry_id}":
            return entries[0] if entries else value
        if isinstance(value, str) and (m := re.fullmatch(r"\{entry_ids:(\d+)\}", value)):
            return entries[: int(m[1])]
        return value

    return {**tool, "args": {k: fill(v) for k, v in (tool.get("args") or {}).items()}}


def content_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(p.get("text", "") for p in content if isinstance(p, dict))
    return "" if content is None else str(content)


def sniff(data: bytes) -> str:
    return "image/png" if data.startswith(b"\x89PNG") else "application/octet-stream"


def has_image(messages: list[dict]) -> bool:
    return any(
        isinstance(part, dict) and part.get("type") == "image_url"
        for m in messages
        if isinstance(m.get("content"), list)
        for part in m["content"]
    )


def png(seed: int) -> bytes:
    """A valid 8×8 single-color PNG, colored by `seed`."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(kind + data)
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)

    color = bytes([seed % 251, seed // 251 % 251, 200])
    rows = b"".join(b"\x00" + color * 8 for _ in range(8))
    header = struct.pack(">IIBBBBB", 8, 8, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


def trigger_line(prompt: str) -> str:
    """The newest chat line in Kiki's prompt (`[u1 Alex] hi` → `hi`)."""
    lines = [line for line in prompt.splitlines() if line.startswith("[")]
    if not lines:
        return prompt.strip().splitlines()[-1] if prompt.strip() else ""
    return re.sub(r"^\[[^\]]*\]\s*", "", lines[-1])


def embeddings(body: dict) -> dict:
    texts = body["input"] if isinstance(body["input"], list) else [body["input"]]
    data = []
    for i, text in enumerate(texts):
        vector = [0.0] * EMBEDDING_DIMENSIONS
        vector[-1] = 0.05  # never all zero
        for word in re.findall(r"[a-z0-9']+", text.lower()):
            digest = hashlib.sha256(word.encode()).digest()
            vector[int.from_bytes(digest[:4]) % (EMBEDDING_DIMENSIONS - 1)] += 1.0
        norm = math.sqrt(sum(x * x for x in vector))
        data.append({"object": "embedding", "index": i, "embedding": [x / norm for x in vector]})
    return {
        "object": "list",
        "data": data,
        "model": body.get("model", "sim/embedding"),
        "usage": {"prompt_tokens": len(texts), "total_tokens": len(texts), "cost": 0.000001},
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--fixture", type=Path, required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    web.run_app(Fakecord(args.fixture, args.port).app(), host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
