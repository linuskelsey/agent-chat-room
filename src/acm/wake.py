"""Waking agents and notifying humans when a message addresses them.

A wake is a short pointer ("go read the room"), never the message itself: the cursor-based
`room_read` stays the only way content reaches an agent, so a wake that is held or dropped
loses nothing, and text posted by one agent is not injected into another agent's prompt.
"""

import asyncio
import json
import os
import time
from pathlib import Path

from acm import identity
from acm.db import Store

CONNECT_TIMEOUT = 2.0
CONFIRM_SECS = float(os.environ.get("ACM_WAKE_CONFIRM_SECS", "30"))


def wake_text(room: str, author: str, everyone: bool, unread: int) -> str:
    who = f"{author} addressed everyone" if everyone else f"{author} mentioned you"
    return f"[acm room {room}] {who} ({unread} unread). Call room_read(room=\"{room}\")."


def plan(store: Store, msg: dict) -> dict:
    """Who a message addresses: agents to wake, humans to notify, and mentions that reach nobody."""
    author = msg["author"]
    members = store.members(msg["room"])
    agents: list[str] = []
    humans: list[str] = []
    skipped: list[str] = []
    everyone = "all" in msg["mentions"]
    names = [m for m in msg["mentions"] if m != "all"]
    if everyone:
        names += [n for n, m in members.items() if m["kind"] == "agent"]
    for name in dict.fromkeys(names):
        if name == author:
            continue
        member = members.get(name)
        if name == "human":
            humans += [n for n, m in members.items() if m["kind"] == "human" and n != author]
        elif member is None:
            skipped.append(f"{name} (not in room)")
        elif member["muted"]:
            skipped.append(f"{name} (muted)")
        elif member["kind"] == "human":
            humans.append(name)
        else:
            agents.append(name)
    return {"agents": agents, "humans": list(dict.fromkeys(humans)), "skipped": skipped, "everyone": everyone}


def session_state(store: Store, name: str) -> dict | None:
    """The live session behind an agent name, or None.

    The registered inbox is only trusted when the session's own record (written by Claude Code)
    names the same socket, so a registration cannot point the daemon at an arbitrary socket.
    """
    agent = store.get_agent(name)
    if agent is None:
        return None
    try:
        info = json.loads((identity.claude_dir() / "sessions" / f"{agent['pid']}.json").read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(info, dict) or info.get("pid") != agent["pid"]:
        return None
    if info.get("messagingSocketPath") != agent["inbox"]:
        return None
    return {"pid": agent["pid"], "inbox": agent["inbox"], "status": info.get("status"),
            "status_at": info.get("statusUpdatedAt") or 0}


async def deliver(inbox: str, text: str) -> None:
    """Write one user message to a session's inbox socket (format found in Phase 0, docs/delivery.md)."""
    payload = {"type": "user", "message": {"role": "user", "content": text}}
    _, writer = await asyncio.wait_for(asyncio.open_unix_connection(inbox), CONNECT_TIMEOUT)
    try:
        writer.write(json.dumps(payload).encode() + b"\n")
        await asyncio.wait_for(writer.drain(), CONNECT_TIMEOUT)
    finally:
        writer.close()


def _clean(text: str, limit: int) -> str:
    text = "".join(c if c.isprintable() else " " for c in text)
    return text[:limit].replace("&", "&amp;").replace("<", "&lt;")


async def notify_desktop(title: str, body: str) -> bool:
    """Best-effort desktop notification. Never raises; False when it could not be sent."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "notify-send", "--app-name=acm", "--", _clean(title, 80), _clean(body, 200),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), 5)
        return proc.returncode == 0
    except (OSError, asyncio.TimeoutError):
        return False


def confirmed(store: Store, room: str, name: str, sent_at: float, before_cursor: int) -> bool:
    """Did the woken session react? It went busy/idle after the wake, or it read the room."""
    state = session_state(store, name)
    if state is not None and state["status_at"] / 1000 >= sent_at:
        return True
    return store.cursor_of(room, name) > before_cursor


def now() -> float:
    return time.time()
