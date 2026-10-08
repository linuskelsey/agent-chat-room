"""The acm daemon: owns the store and serves newline-delimited JSON over a unix socket.

Request:  {"op": "<name>", ...args}
Response: {"ok": true, ...result} or {"ok": false, "error": {"code": ..., "message": ...}}
`watch` turns the connection into a stream of {"event": ...} lines until the client disconnects.
"""

import asyncio
import fcntl
import json
import os
import signal
import socket
import struct
import sys
import time
import traceback
from collections import deque
from pathlib import Path

from acm import accounting, config, fsutil, identity, paths, summary, usagelimits, wake
from acm.db import Store, parse_mentions
from acm.errors import AcmError

LINE_LIMIT = 8 * 1024 * 1024
WATCH_QUEUE_MAX = 1000  # events a watcher may fall behind by before it is disconnected
PENDING_TTL = 120.0  # a woken agent that has not read yet is not woken again for this long
TICK_SECS = float(os.environ.get("ACM_TICK_SECS", "30"))
ACCOUNT_POLL_SECS = float(os.environ.get("ACM_ACCOUNT_POLL_SECS", "2"))
ATTENTION_SECS = float(os.environ.get("ACM_ATTENTION_SECS", "3"))  # blocked this long before the human is told
ATTENTION_POLL_SECS = float(os.environ.get("ACM_ATTENTION_POLL_SECS", "1"))
QUIET_SECS = float(os.environ.get("ACM_QUIET_SECS", "4"))  # how long after an unanswered post before the room is judged quiet
QUIET_POLL_SECS = float(os.environ.get("ACM_QUIET_POLL_SECS", "2"))
QUIET_MAX_SECS = 180.0
ACCOUNT_MAX_SECS = 900.0  # stop waiting for a woken session to go idle after this long
CACHE_TTL_SECS = 300  # an agent idle for longer than this has probably lost its prompt cache


# Room administration is for humans. Three checks tell an agent's process from a human's; each stops a
# different way of getting around the one before. None stops a process that works hard to look human
# (everything runs as the same user), so they guard against accidents and honest mistakes.
HUMAN_ONLY_OPS = {
    "create_room", "close_room", "kill_room", "delete_room", "mute", "unmute", "set_limits", "invite", "snooze", "unsnooze", "link",
    "shutdown",
}
# Set in the environment of anything a Claude Code session starts, and inherited by processes it detaches.
SESSION_ENV_MARKERS = {
    "CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_PID",
}
# Admin actions need an interactive terminal. Tests (and nothing else) switch this off for the daemon they start.
REQUIRE_TTY = os.environ.get("ACM_ALLOW_NO_TTY") != "1"


def peer_credentials(writer: asyncio.StreamWriter) -> tuple[int, int] | None:
    """(pid, uid) of the process on the other end of the connection, as the kernel reports them."""
    sock = writer.get_extra_info("socket")
    try:
        pid, uid, _ = struct.unpack("3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
        return pid, uid
    except (OSError, AttributeError, struct.error):
        return None


def peer_pid(writer: asyncio.StreamWriter) -> int | None:
    creds = peer_credentials(writer)
    return creds[0] if creds else None


def describe_peer(pid: int | None) -> dict:
    """Facts about the process on the other end of a connection."""
    info = {"in_session": False, "marked": False, "tty": False, "session": None}
    if pid is None:
        return info
    info["session"] = identity.find_session(pid, max_depth=32)
    info["in_session"] = info["session"] is not None
    try:
        keys = {e.split(b"=", 1)[0].decode(errors="replace") for e in Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")}
        info["marked"] = bool(keys & SESSION_ENV_MARKERS)
    except OSError:
        pass
    try:
        info["tty"] = int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[4]) != 0
    except (OSError, ValueError, IndexError):
        pass
    return info


def guard(req: dict, peer: dict) -> None:
    """Refuse human-only requests from anything that looks like an agent, or that has no terminal."""
    op = req.get("op")
    if not (op in HUMAN_ONLY_OPS or (op == "post" and req.get("from", "human") != "agent")):
        return
    if peer["in_session"] or peer["marked"]:
        raise AcmError(
            "human_only",
            "refused: this is running inside a Claude Code session, and this action is for humans. "
            "Run it in a normal terminal or the acm room client. Agents use the room_* tools.",
        )
    if REQUIRE_TTY and not peer["tty"]:
        raise AcmError(
            "human_only",
            "refused: this action needs an interactive terminal, and none was found. Run it from a terminal.",
        )


# Which request field names the member a request acts as.
ACTS_AS = {"register": "name", "post": "author", "join": "member", "leave": "member", "catch_up": "member", "read": "member"}


# Operations that look at one room, and the request field that names it.
ROOM_FIELD = {"get_room": "name", "tail": "room", "read": "room", "members": "room", "summary": "room",
              "export": "room", "budget": "room", "wake_preview": "room", "watch": "room"}


def session_name(session: dict) -> str:
    """The name an agent in this Claude session goes by: its session name, or agent-<pid> if it has none."""
    return identity.sanitize(str(session.get("name") or "")) or f"agent-{session['pid']}"


def _alive(pid: int) -> bool:
    return Path(f"/proc/{pid}").exists()


def _need(req: dict, key: str):
    if key not in req:
        raise AcmError("bad_request", f"missing field: {key}")
    return req[key]


class Daemon:
    def __init__(self, store: Store):
        self.store = store
        self.watchers: dict[str, set[asyncio.Queue]] = {}
        self.global_watchers: set[asyncio.Queue] = set()  # every room's events, for clients that show all rooms
        self.watch_writers: dict[asyncio.Queue, asyncio.StreamWriter] = {}
        self.global_names: dict[asyncio.Queue, set[str] | None] = {}  # who each all-rooms watcher is, for what it may see
        self.writers: set[asyncio.StreamWriter] = set()
        self.pending: dict[tuple[str, str], float] = {}  # (room, agent) -> when the outstanding wake was sent
        self.tasks: set[asyncio.Task] = set()
        self.stop = asyncio.Event()
        self.rate: dict[tuple[str, str], deque] = {}  # (room, agent) -> recent (time, cost) of its posts
        self.agent_turns: dict[str, int] = {}  # room -> agent-to-agent wakes since the last human message
        self.warned: set[tuple[str, str]] = set()  # (room, cap) already warned about
        self.spans: dict[str, dict] = {}  # agent -> the token-accounting span currently open for it
        self.blocked: dict[str, set[str]] = {}  # agent -> rooms where the human has been told it is waiting on them
        store.on_system = lambda room, msg: self._publish(room, {"event": "message", "message": msg})

    def _publish(self, room: str, event: dict) -> None:
        event = {**event, "room_name": room}
        recipients = [*self.watchers.get(room, ()), *(
            q for q in self.global_watchers if self.can_see(room, self.global_names.get(q))
        )]
        for q in recipients:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:  # it has stopped reading: cut it off rather than keep its backlog in memory
                writer = self.watch_writers.get(q)
                if writer is not None:
                    writer.transport.abort()

    def _spawn(self, coro) -> None:
        task = asyncio.ensure_future(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def invite(self, req: dict) -> dict:
        """Add agents to a room by session name and send each a pointer telling it to join."""
        room, by, names = _need(req, "room"), _need(req, "by"), _need(req, "names")
        if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
            raise AcmError("bad_request", "names must be a list of strings")
        info = self.store.get_room(room)
        if info["status"] != "open":
            raise AcmError("room_closed", f"room is closed: {room}")
        out: dict = {"added": [], "already": [], "unreachable": []}
        for name in dict.fromkeys(names):
            if name in self.store.members(room):
                out["already"].append(name)
                continue
            sess, why = wake.find_live_session(name)
            if sess is None:
                out["unreachable"].append(f"{name} ({why})")
                continue
            self.store.register_agent(name, sess["pid"], sess["messagingSocketPath"])
            self.store.join(room, name, "agent", pending=True)  # not "joined" until it acts
            state = wake.session_state(self.store, name)
            sent_at = wake.now()
            try:
                await wake.deliver(sess["messagingSocketPath"], wake.invite_text(room, by, info["topic"]))
            except (OSError, asyncio.TimeoutError):
                out["unreachable"].append(f"{name} (added, but its inbox is not reachable)")
                continue
            self._track(room, name, state, sent_at)
            out["added"].append(name)
        if out["added"]:
            self.store.post_system(room, f"{by} added {', '.join(out['added'])}")
        return out

    def _track(self, room: str, name: str, state: dict | None, sent_at: float) -> None:
        """Bookkeeping after a wake was delivered: block repeat wakes, check it landed, count its tokens.

        `sent_at` is taken before delivery: a fast session can react before delivery even returns.
        """
        self.pending[(room, name)] = time.monotonic()
        self._spawn(self._confirm(room, name, sent_at, self.store.cursor_of(room, name)))
        if state is not None:
            self._spawn(self._attend(room, name, sent_at))
        if state is None:
            return
        path = accounting.find_transcript(state.get("session_id"))
        offset = accounting.size(path)
        previous = self.spans.get(name)
        if previous is not None:
            previous["end"] = offset  # an agent is one stream of work: the earlier wake's span stops where this starts
        span = {"room": room, "name": name, "sent_at": sent_at, "offset": offset, "path": path, "end": None}
        self.spans[name] = span
        self._spawn(self._account(span))

    def check_identity(self, req: dict, peer: dict) -> None:
        """An agent speaks only as itself.

        A caller inside a Claude session may act only under its session's own name (or a name it held
        earlier, before a rename), may register only its own session, and cannot take a name that a live
        session already holds. A caller that carries a session's environment but cannot be tied to a
        session may not act as an agent at all. Callers outside any session are not checked here.
        """
        if not (peer["in_session"] or peer["marked"]):
            return
        op = req.get("op")
        field = ACTS_AS.get(op)
        if field is None or field not in req:
            return
        if op == "read" and (req.get("peek") or req.get("since") is not None):
            return  # looking without moving anyone's read position is not acting as them
        session, claimed = peer["session"], req[field]
        if session is None:
            raise AcmError("identity", "this process looks like an agent's but cannot be tied to a session, so it may not act as one")
        own = session_name(session)
        earlier = {a["name"] for a in self.store.agents_of(session["pid"])}
        if claimed != own and claimed not in earlier:
            raise AcmError("identity", f"this session is {own}; it cannot act as {claimed}")
        if op == "register":
            if req.get("pid") != session["pid"] or req.get("inbox") != session.get("messagingSocketPath"):
                raise AcmError("identity", "a session can only register itself")
            held = self.store.get_agent(claimed)
            if held and held["pid"] != session["pid"] and _alive(held["pid"]):
                raise AcmError("identity", f"{claimed} is already held by another live session")

    def agent_names(self, peer: dict) -> set[str] | None:
        """The names an agent caller may act as, or None for a caller that is not an agent (humans, harnesses).

        An agent is whatever runs under a Claude session or carries one's environment. A marked process that
        cannot be tied to a session has no names at all.
        """
        if not (peer["in_session"] or peer["marked"]):
            return None
        session = peer["session"]
        if session is None:
            return set()
        return {session_name(session)} | {a["name"] for a in self.store.agents_of(session["pid"])}

    def can_see(self, room: str, names: set[str] | None) -> bool:
        """Whether an agent known by `names` may see `room`: always for humans, and for agents when the room is
        open to anyone or one of the names is a member."""
        if names is None:
            return True
        try:
            if self.store.limits(room)["join_policy"] == "anyone":
                return True
            members = self.store.members(room)
        except AcmError:
            return False
        return any(n in members for n in names)

    def check_invited(self, room: str, req: dict) -> None:
        """Joining, posting or catching up: an agent that was not added is told how to get added."""
        names = req.get("_names")
        if names is not None and not self.can_see(room, names):
            who = sorted(names)[0] if names else "this agent"
            raise AcmError("not_invited", f"{room} only takes agents a human has added; ask them to add {who}")

    def check_visible(self, req: dict) -> None:
        """Reading about a room an agent cannot see gets the same answer as a room that does not exist."""
        names = req.get("_names")
        field = ROOM_FIELD.get(req.get("op"))
        if names is None or field is None or field not in req:
            return
        if not self.can_see(req[field], names):
            raise AcmError("not_found", f"no such room: {req[field]}")

    async def dispatch_async(self, req: dict) -> dict:
        op = req.get("op")
        if op == "invite":
            return await self.invite(req)
        if op == "post":
            return await self.post(req)
        if op == "budget":
            return await self.budget(_need(req, "room"))
        if op == "wake_preview":
            return await self.preview(req)
        return self.dispatch(req)

    async def pause_reason(self, room: str, lim: dict) -> str | None:
        """Why agents must not be woken or post right now (account usage limits), or None."""
        if reason := usagelimits.valve_reason(lim):
            return reason
        cap = lim["room_share_pct"]
        start = usagelimits.window_start()
        if cap is None or start is None:
            return None
        used = self.store.usage_since(room, start)
        share = await asyncio.get_running_loop().run_in_executor(None, usagelimits.room_share_pct, used)
        if share is not None and share >= cap:
            end = usagelimits.window_end()
            return (
                f"this room used an estimated {share:.1f}% of the 5-hour usage limit (cap {cap:g}%), "
                f"resumes at {time.strftime('%H:%M', time.localtime(end))}"
            )
        return None

    def _rate_check(self, room: str, author: str, lim: dict) -> None:
        cap = lim["agent_rate_per_min"]
        if not cap:
            return
        recent = self.rate.setdefault((room, author), deque())
        now = time.monotonic()
        while recent and now - recent[0][0] > 60:
            recent.popleft()
        if sum(cost for _, cost in recent) >= cap:
            wait = int(60 - (now - recent[0][0])) + 1
            raise AcmError(
                "rate_limited", f"too many posts: the limit is {cap} per minute (an overlong post counts double), wait {wait}s"
            )

    async def post(self, req: dict) -> dict:
        room, author, body = _need(req, "room"), _need(req, "author"), _need(req, "body")
        from_kind, s = req.get("from", "human"), self.store
        lim = s.limits(room)
        is_agent = from_kind == "agent"
        if is_agent:
            self.check_invited(room, req)
        if is_agent:
            if lim["ceiling_chars"] and isinstance(body, str) and len(body) > lim["ceiling_chars"]:
                raise AcmError("too_long", f"post is {len(body)} characters, above the limit of {lim['ceiling_chars']}")
            if reason := await self.pause_reason(room, lim):
                raise AcmError("paused", f"agents cannot post right now: {reason}")
            self._rate_check(room, author, lim)
        msg = s.post(
            room, author, body,
            kind=req.get("kind", "post"), from_kind=from_kind, refs=req.get("refs"),
            no_reply_needed=bool(req.get("no_reply_needed", False)),
        )
        notices: list[str] = []
        if is_agent:
            self.pending.pop((room, author), None)  # it is active in the room, so a new mention may wake it again
            target, overlong = config.target_chars(lim), False
            if target and len(msg["body"]) > target:
                overlong = True
                strikes = s.add_strike(room, author)
                notices.append(
                    f"over the {target}-character target ({len(msg['body'])}): be shorter next time (strike {strikes})"
                )
            self.rate.setdefault((room, author), deque()).append((time.monotonic(), 2 if overlong else 1))
        else:
            self.agent_turns[room] = 0  # a human message restarts the agent-to-agent allowance
        s.add_usage(room, author, "post", output=-(-len(msg["body"]) // 4))
        self._publish(room, {"event": "message", "message": msg})
        wake_result = await self.notify(msg, lim, is_agent)
        if is_agent and wake_result["woke"]:
            self.agent_turns[room] = self.agent_turns.get(room, 0) + 1
        if (is_agent and lim["notify_when_quiet"] and not wake_result["woke"] and not wake_result["already_pending"]
                and not wake_result["notified"]):
            self._spawn(self._quiet(room, msg["id"], wake_result["suppressed"]))  # nothing else is going to happen: say so
        if wake_result["suppressed"]:
            notices.append("nobody was woken: " + wake_result["suppressed"])
        elif is_agent and not msg["mentions"] and not msg["no_reply_needed"] and self._others(room, author):
            notices.append(
                "nobody was addressed, so nobody was woken. To hand this on, write @their-name in the post "
                "(a name followed by a colon does not count). If nothing is needed from anyone, mark it no_reply_needed."
            )
        self.enforce(room)
        return {"message": msg, "wake": wake_result, "notices": notices}

    async def _targets(self, msg: dict, lim: dict, from_agent: bool) -> tuple[dict, list, dict]:
        """Decide who a message would wake, without waking anyone. Returns (report, [(name, state)], plan)."""
        room = msg["room"]
        out: dict = {
            "woke": [], "already_pending": [], "unreachable": [], "notified": [], "suppressed": None, "passive": None,
        }
        p = {"agents": [], "humans": [], "skipped": [], "everyone": False}
        if msg["no_reply_needed"]:
            out["passive"] = "no_reply_needed"
            return out, [], p
        p = wake.plan(self.store, msg)
        out["unreachable"] = list(p["skipped"])
        agents = p["agents"]
        turns, cooldown = self.agent_turns.get(room, 0), lim["cooldown_turns"]
        if agents and (reason := await self.pause_reason(room, lim)):
            out["suppressed"], agents = reason, []
        elif agents and from_agent and (capped := self.store.budget_status(room)["exceeded"]):
            out["suppressed"] = f"{capped[0]} cap reached: waiting for a human (raise the cap, or ask for a wrap-up)"
            agents = []
        elif agents and from_agent and cooldown and turns >= cooldown:
            out["suppressed"] = f"cooldown: {turns} agent-to-agent wakes in a row, a human message restarts it"
            agents = []
        targets = []
        snoozed = self.store.snoozed()
        for name in agents:
            if name in snoozed:
                until = time.strftime("%H:%M", time.localtime(snoozed[name]))
                out["unreachable"].append(f"{name} (snoozed until {until})")
                continue
            if time.monotonic() - self.pending.get((room, name), -PENDING_TTL) < PENDING_TTL:
                out["already_pending"].append(name)
                continue
            state = wake.session_state(self.store, name)
            if state is None:
                out["unreachable"].append(f"{name} (no live session)")
                continue
            targets.append((name, state))
        return out, targets, p

    async def notify(self, msg: dict, lim: dict, from_agent: bool) -> dict:
        """Wake the agents a message addresses and notify the humans. Reports what happened to each."""
        room, author = msg["room"], msg["author"]
        out, targets, p = await self._targets(msg, lim, from_agent)
        sends = []
        for name, state in targets:
            inline = msg["body"] if lim["inline_human"] and msg["from"] == "human" else None
            text = wake.wake_text(room, author, p["everyone"], self.store.unread_count(room, name), inline)
            sends.append((name, state, text))
        sent_at = wake.now()
        results = await asyncio.gather(*(wake.deliver(st["inbox"], text) for _, st, text in sends), return_exceptions=True)
        for (name, state, _), result in zip(sends, results):
            if isinstance(result, BaseException):
                out["unreachable"].append(f"{name} (inbox not reachable)")
                continue
            out["woke"].append(name)
            self._track(room, name, state, sent_at)
        for human in p["humans"]:
            out["notified"].append(human)
            self._spawn(wake.notify_desktop(f"acm: {room}", f"{author}: {msg['body']}"))
        return out

    def _others(self, room: str, author: str) -> bool:
        """Whether the room has another agent that could have been addressed."""
        return any(m["kind"] == "agent" and n != author for n, m in self.store.members(room).items())

    def _estimate(self, name: str, state: dict) -> dict:
        """What waking this agent now should cost: its usual warm figure, or its cold one if its cache has likely expired."""
        cost = self.store.wake_cost(name)
        idle_secs = time.time() - state["status_at"] / 1000 if state["status"] == "idle" else 0
        cold = idle_secs > CACHE_TTL_SECS
        return {"name": name, "tokens": int(cost["cold"] if cold else cost["warm"]), "cold": cold, "history": cost["n"]}

    async def preview(self, req: dict) -> dict:
        """Who a message would wake and about what that would cost, without posting it."""
        room, author, body = _need(req, "room"), _need(req, "author"), _need(req, "body")
        if not isinstance(body, str):
            raise AcmError("bad_request", "body must be text")
        lim = self.store.limits(room)
        from_kind = req.get("from", "human")
        msg = {
            "room": room, "author": author, "body": body, "from": from_kind,
            "no_reply_needed": bool(req.get("no_reply_needed", False)), "mentions": parse_mentions(body),
        }
        out, targets, p = await self._targets(msg, lim, from_kind == "agent")
        wakes = [self._estimate(n, st) for n, st in targets]
        return {
            "wakes": wakes, "total": sum(w["tokens"] for w in wakes), "notified": p["humans"],
            "suppressed": out["suppressed"], "passive": out["passive"], "already_pending": out["already_pending"],
            "unreachable": out["unreachable"], "confirm_over": lim["confirm_wake_tokens"],
        }

    async def budget(self, room: str) -> dict:
        s = self.store
        status = s.budget_status(room)
        return {
            **status,
            "usage": s.usage(room),
            "members": list(s.members(room).values()),
            "paused": await self.pause_reason(room, status["limits"]),
            "agent_turns": self.agent_turns.get(room, 0),
            "snoozed": self.store.snoozed(),
        }

    async def _attend(self, room: str, name: str, sent_at: float) -> None:
        """Tell the human when a woken agent is stuck waiting for them in its own window.

        An approval prompt or a question in an agent's own Claude Code window stops it dead, and nothing in
        the room shows that. The session record's status says `waiting` while that is so.
        """
        deadline = time.monotonic() + ACCOUNT_MAX_SECS
        waiting_since: float | None = None
        told = False
        try:
            while time.monotonic() < deadline:
                await asyncio.sleep(ATTENTION_POLL_SECS)
                state = wake.session_state(self.store, name)
                if state is None:
                    break
                if state["status"] == "waiting":
                    waiting_since = waiting_since or time.monotonic()
                    if not told and time.monotonic() - waiting_since >= ATTENTION_SECS:
                        told = True
                        self._blocked(room, name, True)
                    continue
                waiting_since = None
                if told:
                    told = False
                    self._blocked(room, name, False)
                if state["status"] == "idle" and state["status_at"] / 1000 >= sent_at:
                    break
        finally:
            if told:
                self._blocked(room, name, False)

    def _anyone_working(self, room: str) -> bool:
        for name, member in self.store.members(room).items():
            if member["kind"] == "agent":
                state = wake.session_state(self.store, name)
                if state is not None and state["status"] in ("busy", "waiting"):
                    return True
        return False

    async def _quiet(self, room: str, post_id: int, why: str | None) -> None:
        """After an agent's post that woke nobody: once every agent has stopped and nobody has spoken since,
        tell the human it is their turn. Only runs when the room's notify_when_quiet is on."""
        await asyncio.sleep(QUIET_SECS)
        deadline = time.monotonic() + QUIET_MAX_SECS
        while time.monotonic() < deadline:
            try:
                if self.store.get_room(room)["status"] != "open" or self.store.last_message_id(room) != post_id:
                    return  # someone has spoken since, or the room is over: it is not quiet
            except AcmError:
                return
            if not self._anyone_working(room):
                break
            await asyncio.sleep(QUIET_POLL_SECS)
        else:
            return
        text = f"{room} is quiet: nobody is working and nobody was woken, so it is your turn" + (f" ({why})" if why else "")
        self._publish(room, {"event": "quiet", "text": text})
        await wake.notify_desktop(f"acm: {room}", text)

    def _blocked(self, room: str, name: str, waiting: bool) -> None:
        text = f"{name} is waiting for you in its own Claude Code window (an approval or a question)"
        first = not self.blocked.get(name)
        rooms = self.blocked.setdefault(name, set())
        (rooms.add if waiting else rooms.discard)(room)
        self._publish(room, {"event": "attention", "agent": name, "waiting": waiting, "text": text if waiting else f"{name} is no longer waiting"})
        if waiting and first:  # one notification per agent, however many rooms it is waiting in
            self._spawn(wake.notify_desktop(f"acm: {room}", text))

    async def _account(self, span: dict) -> None:
        """Count the tokens a woken session spends, from the wake until it is idle again or woken for another room."""
        room, name = span["room"], span["name"]
        deadline = time.monotonic() + ACCOUNT_MAX_SECS
        while time.monotonic() < deadline and span["end"] is None:
            await asyncio.sleep(ACCOUNT_POLL_SECS)
            state = wake.session_state(self.store, name)
            if state is None or (state["status"] == "idle" and state["status_at"] / 1000 >= span["sent_at"]):
                break
        if self.spans.get(name) is span:
            del self.spans[name]
        used = accounting.usage_since(span["path"], span["offset"], span["end"])
        if any(used.values()):
            try:
                self.store.add_usage(
                    room, name, "wake", used["input"], used["output"], used["cache_creation"], used["cache_read"]
                )
            except AcmError:
                return
            self.enforce(room)

    def enforce(self, room: str) -> None:
        """Warn when a cap is close, and tell the human when one is reached.

        A room that has reached a cap is not closed: agents stop waking each other, a human can still
        wake them (for example to ask for a wrap-up), and raising the cap resumes normal operation.
        """
        try:
            if self.store.get_room(room)["status"] != "open":
                return
            status = self.store.budget_status(room)
        except AcmError:
            return
        reached = set(status["exceeded"])
        for key in [k for k in self.warned if k[0] == room and k[1].startswith("reached:")]:
            if key[1][len("reached:"):] not in reached:
                self.warned.discard(key)  # the cap was raised: alert again if it is reached again
        for cap in status["warn"]:
            if (room, cap) not in self.warned:
                self.warned.add((room, cap))
                text = f"{cap}: {status['used'][cap]:.0f} of {status['limits'][cap]} used"
                self._publish(room, {"event": "warning", "room": room, "text": text})
                self._spawn(wake.notify_desktop(f"acm: {room} nearing a cap", text))
        for cap in sorted(reached):
            key = (room, "reached:" + cap)
            if key in self.warned:
                continue
            self.warned.add(key)
            text = (
                f"{cap} reached ({status['used'][cap]:.0f} of {status['limits'][cap]}). Agents no longer wake each "
                f"other. Raise it with `acm budget {room} {cap}=...`, ask them to wrap up, or close the room."
            )
            print(f"[{room}] {text}", file=sys.stderr, flush=True)
            self._publish(room, {"event": "warning", "room": room, "text": text})
            self._spawn(wake.notify_desktop(f"acm: {room} is at its cap", text))

    async def ticker(self) -> None:
        while True:
            await asyncio.sleep(TICK_SECS)
            for r in self.store.list_rooms(status="open"):
                self.enforce(r["name"])

    async def _confirm(self, room: str, name: str, sent_at: float, cursor_before: int) -> None:
        await asyncio.sleep(wake.CONFIRM_SECS)
        if wake.confirmed(self.store, room, name, sent_at, cursor_before):
            return
        text = (
            f"{name} did not react to the wake within {int(wake.CONFIRM_SECS)}s. It may be holding messages: "
            "set crossSessionInbound to accept in that session."
        )
        print(f"[{room}] {text}", file=sys.stderr, flush=True)
        self._publish(room, {"event": "warning", "room": room, "text": text})
        await wake.notify_desktop(f"acm: {room}", text)

    @staticmethod
    def _project_dir(path) -> str | None:
        if path in (None, "", "none"):
            return None
        resolved = Path(str(path)).expanduser().resolve()
        if not resolved.is_dir():
            raise AcmError("bad_request", f"not a directory: {resolved}")
        return str(resolved)

    def _create(self, req: dict) -> dict:
        s, name = self.store, _need(req, "name")
        source = req.get("continue_from")
        carried = None
        pdir = self._project_dir(req.get("dir"))
        if source:  # check the source before creating anything
            src_info = s.get_room(source)
            pdir = pdir or src_info["project_dir"]
            carried = summary.render(summary.with_ref_notes(summary.build(s, source)))
        room = s.create_room(name, _need(req, "by"), req.get("topic", ""))
        if pdir:
            room = s.set_project_dir(name, pdir)
        if carried:
            s.post_system(name, f"continued from {source}\n\n{carried}")
        self._publish(name, {"event": "room_created", "room": room})
        return {"room": room}

    def _export_path(self, room: str) -> Path:
        directory = self.store.limits(room)["export_dir"]
        return (Path(directory).expanduser() if directory else paths.data_dir() / "rooms") / f"{room}.md"

    def _close(self, op: str, req: dict) -> dict:
        """Close a room: write its summary into the log, save it to a file and tell the human the outcome."""
        s, name, by = self.store, _need(req, "name"), _need(req, "by")
        info = s.get_room(name)
        if info["status"] != "open":
            raise AcmError("room_closed", f"room is closed: {name}")
        if op != "kill_room" and info["created_by"] != by:
            raise AcmError("forbidden", f"only the creator ({info['created_by']}) can close {name}")
        data = summary.with_ref_notes(summary.build(s, name, closing_by=by))
        data["ended"] = time.time()
        room = s.close_room(name, by, force=op == "kill_room", summary=summary.render(data))
        self._publish(name, {"event": "closed", "room": room})
        exported = None
        try:
            path = fsutil.unclaimed_path(self._export_path(name), marker=f"<!-- acm-room: {name} -->")
            fsutil.atomic_write(path, f"<!-- acm-room: {name} -->\n" + summary.render(data, title=True) + "\n\n" + summary.transcript(s, name) + "\n")
            exported = str(path)
        except OSError as e:
            print(f"[{name}] could not save the summary: {e}", file=sys.stderr, flush=True)
        final = summary.final_decision(data)
        self._spawn(wake.notify_desktop(
            f"acm: {name} closed", f"Final decision: {final}" if final else "No decisions were pinned."
        ))
        for key in [k for k in self.pending if k[0] == name]:
            del self.pending[key]
        self.agent_turns.pop(name, None)
        return {"room": room, "summary": summary.render(data), "exported": exported, "final_decision": final}

    def _delete(self, req: dict) -> dict:
        """Delete a room and everything kept about it, including the summary file acm wrote when it closed."""
        s, name = self.store, _need(req, "name")
        info = s.get_room(name)
        if info["status"] == "open" and not req.get("force"):
            raise AcmError("room_open", f"{name} is still open: close it first, or delete it anyway with force")
        export = self._export_path(name)  # worked out while the room's own settings still exist
        self._publish(name, {"event": "deleted"})  # told before the room stops existing, so its watchers can see it
        counts = s.delete_room(name)
        removed = None
        try:
            if export.is_file() and export.read_text(errors="replace").startswith(f"<!-- acm-room: {name} -->"):
                export.unlink()  # only a file acm itself wrote for this room
                removed = str(export)
        except OSError as e:
            print(f"[{name}] could not remove {export}: {e}", file=sys.stderr, flush=True)
        for key in [k for k in self.pending if k[0] == name]:
            del self.pending[key]
        for key in [k for k in self.rate if k[0] == name]:
            del self.rate[key]
        self.agent_turns.pop(name, None)
        return {"deleted": name, **counts, "export_removed": removed}

    @staticmethod
    def _kind(req: dict, default: str = "human") -> str:
        """The member kind to create. A caller inside a Claude session can only ever be an agent."""
        return "agent" if req.get("_agentish") else req.get("kind", default)

    def dispatch(self, req: dict) -> dict:
        s = self.store
        op = _need(req, "op")
        if op == "ping":
            return {"pid": os.getpid()}
        if op == "shutdown":
            self.stop.set()
            return {}
        if op == "create_room":
            return self._create(req)
        if op == "get_room":
            return {"room": s.get_room(_need(req, "name"))}
        if op == "list_rooms":
            rooms = s.list_rooms(req.get("status"), req.get("member"))
            return {"rooms": [r for r in rooms if self.can_see(r["name"], req.get("_names"))]}
        if op in ("close_room", "kill_room"):
            return self._close(op, req)
        if op == "delete_room":
            return self._delete(req)
        if op == "summary":
            data = summary.with_ref_notes(summary.build(s, _need(req, "room")))
            return {"summary": summary.render(data), "final_decision": summary.final_decision(data)}
        if op == "export":
            room = _need(req, "room")
            data = summary.with_ref_notes(summary.build(s, room))
            text = summary.render(data, title=True)
            if not req.get("summary_only"):
                text += "\n\n" + summary.transcript(s, room)
            return {"text": text + "\n"}
        if op == "search":
            found = s.search(
                _need(req, "query"), req.get("room"), req.get("author"), req.get("status"),
                int(req.get("limit", 50)), bool(req.get("include_system", False)),
            )
            return {"matches": [m for m in found if self.can_see(m["room"], req.get("_names"))]}
        if op == "link":
            room = s.set_project_dir(_need(req, "room"), self._project_dir(req.get("dir")))
            self._publish(room["name"], {"event": "room_updated", "what": "link"})
            return {"room": room}
        if op == "join":
            self.check_invited(_need(req, "room"), req)
            return {"member": s.join(_need(req, "room"), _need(req, "member"), self._kind(req))}
        if op == "leave":
            s.leave(_need(req, "room"), _need(req, "member"))
            return {}
        if op in ("mute", "unmute"):
            s.set_muted(_need(req, "room"), _need(req, "member"), op == "mute")
            self._publish(req["room"], {"event": "room_updated", "what": op})
            return {}
        if op == "members":
            return {"members": list(s.members(_need(req, "room")).values())}
        if op == "snooze":
            return {"until": s.snooze(_need(req, "name"), float(_need(req, "seconds")))}
        if op == "unsnooze":
            s.unsnooze(_need(req, "name"))
            return {}
        if op == "set_limits":
            limits = s.set_limits(_need(req, "room"), _need(req, "updates"))
            self._publish(req["room"], {"event": "room_updated", "what": "limits"})
            return {"limits": limits}
        if op == "register":
            s.register_agent(_need(req, "name"), _need(req, "pid"), _need(req, "inbox"))
            return {}
        if op == "read":
            if req.get("since") is None and not req.get("peek"):
                self.pending.pop((req.get("room"), req.get("member")), None)  # they are catching up now
            return s.read(
                _need(req, "room"),
                _need(req, "member"),
                since=req.get("since"),
                peek=bool(req.get("peek", False)),
                limit=req.get("limit"),
                exclude_own=bool(req.get("exclude_own", False)),
                decisions=req.get("decisions", "all"),
                kind=self._kind(req),
            )
        if op == "catch_up":
            self.check_invited(_need(req, "room"), req)
            self.pending.pop((req.get("room"), req.get("member")), None)
            return s.catch_up(
                _need(req, "room"), _need(req, "member"), int(req.get("keep", 10)), req.get("kind", "agent")
            )
        if op == "tail":
            return {"messages": s.tail(_need(req, "room"), int(req.get("n", 20)))}
        raise AcmError("bad_request", f"unknown op: {op}")

    async def _send(self, writer: asyncio.StreamWriter, obj: dict) -> None:
        writer.write(json.dumps(obj, separators=(",", ":")).encode() + b"\n")
        await writer.drain()

    async def _watch(self, req: dict, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        everywhere = req.get("op") == "watch_all"
        room = "*" if everywhere else self.store.get_room(_need(req, "room"))["name"]
        q: asyncio.Queue = asyncio.Queue(WATCH_QUEUE_MAX)
        self.watch_writers[q] = writer
        if everywhere:
            self.global_names[q] = req.get("_names")
        (self.global_watchers if everywhere else self.watchers.setdefault(room, set())).add(q)
        eof = asyncio.ensure_future(reader.read(1))
        try:
            await self._send(writer, {"ok": True, "watching": room})
            while True:
                get = asyncio.ensure_future(q.get())
                done, _ = await asyncio.wait({get, eof}, return_when=asyncio.FIRST_COMPLETED)
                if eof in done:
                    get.cancel()
                    return
                await self._send(writer, get.result())
        except ConnectionError:
            return  # the watcher went away
        finally:
            eof.cancel()
            self.watch_writers.pop(q, None)
            self.global_names.pop(q, None)
            (self.global_watchers if everywhere else self.watchers[room]).discard(q)

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        creds = peer_credentials(writer)
        if creds is None or creds[1] != os.getuid():  # the socket is owner-only already; this checks it anyway
            writer.close()
            return
        self.writers.add(writer)
        peer = describe_peer(creds[0])
        try:
            while True:
                try:
                    line = await reader.readline()
                except (asyncio.LimitOverrunError, ValueError):
                    await self._send(writer, {"ok": False, "error": {"code": "too_large", "message": "request too large"}})
                    return
                if not line:
                    return
                try:
                    req = json.loads(line)
                    if not isinstance(req, dict):
                        raise AcmError("bad_request", "request must be a JSON object")
                    guard(req, peer)
                    self.check_identity(req, peer)
                    names = self.agent_names(peer)
                    req.pop("_names", None)
                    req.pop("_agentish", None)
                    if names is not None:  # a caller cannot claim to be something else by sending these itself
                        req["_names"], req["_agentish"] = names, True
                    self.check_visible(req)
                    if req.get("op") in ("watch", "watch_all"):
                        await self._watch(req, reader, writer)
                        return
                    resp = {"ok": True, **await self.dispatch_async(req)}
                except AcmError as e:
                    resp = {"ok": False, "error": {"code": e.code, "message": e.message}}
                except json.JSONDecodeError:
                    resp = {"ok": False, "error": {"code": "bad_request", "message": "invalid JSON"}}
                except Exception:
                    traceback.print_exc()
                    resp = {"ok": False, "error": {"code": "internal", "message": "internal error, see daemon log"}}
                await self._send(writer, resp)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            self.writers.discard(writer)
            writer.close()


def _socket_alive(path: Path) -> bool:
    s = socket.socket(socket.AF_UNIX)
    try:
        s.connect(str(path))
        return True
    except OSError:
        return False
    finally:
        s.close()


async def serve(store: Store, sock: Path) -> None:
    fsutil.ensure_private_dir(sock.parent)
    # A held lock means a live daemon; it also keeps two racing starters from unlinking each other's socket.
    lock = open(sock.with_suffix(".lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("acm daemon already running") from None
    if sock.exists():
        if _socket_alive(sock):
            raise SystemExit("acm daemon already running")
        sock.unlink()
    daemon = Daemon(store)
    old_umask = os.umask(0o177)
    try:
        server = await asyncio.start_unix_server(daemon.handle, path=str(sock), limit=LINE_LIMIT)
    finally:
        os.umask(old_umask)
    os.chmod(sock, 0o600)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, daemon.stop.set)
    ticker = asyncio.ensure_future(daemon.ticker())
    async with server:
        await daemon.stop.wait()
        ticker.cancel()
        for w in list(daemon.writers):  # open watchers would otherwise keep the server from closing
            w.close()
    sock.unlink(missing_ok=True)


def main() -> int:
    os.umask(0o077)  # everything the daemon creates (database, exports) is private to this user
    store = Store(paths.db_path())
    try:
        asyncio.run(serve(store, paths.socket_path()))
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
