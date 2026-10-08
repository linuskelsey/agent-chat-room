"""What the terminal client knows and does, with no terminal code in it.

Everything goes through the daemon's public operations and event stream, so the client can only do what
the CLI can do. The view (tui.py) draws this state and turns keys into calls on it.
"""

from dataclasses import dataclass, field

from acm import client, summary
from acm.errors import AcmError

PAGE = 200  # messages loaded per room, and added by each "load older"


@dataclass
class Room:
    name: str
    topic: str = ""
    status: str = "open"
    created_by: str = ""
    created_at: float = 0.0
    closed_at: float | None = None
    last_ts: float | None = None
    unread: int = 0
    member_count: int = 0
    joined: bool = False  # whether I am a member; only members have unread counts and a read position
    messages: list = field(default_factory=list)
    decisions: list = field(default_factory=list)
    members: dict | None = None  # name -> member record, once the room has been opened
    loaded: bool = False
    exhausted: bool = False  # everything older has been loaded
    draft: str = ""

    @property
    def activity(self) -> float:
        return self.last_ts or self.closed_at or self.created_at

    @property
    def open(self) -> bool:
        return self.status == "open"


class Model:
    def __init__(self, me: str, request=client.request):
        self.me = me
        self.request = request
        self.rooms: dict[str, Room] = {}
        self.selected: str | None = None
        self.notice = ""  # a line for the status bar: warnings, results of actions
        self.confirm_over = 100000

    # -- rooms ---------------------------------------------------------

    def load_rooms(self) -> None:
        """(Re)read the list of rooms. Messages already loaded are kept."""
        listed = self.request("list_rooms", status=None, member=self.me)["rooms"]
        for info in listed:
            room = self.rooms.setdefault(info["name"], Room(info["name"]))
            room.topic, room.status, room.created_by = info["topic"], info["status"], info["created_by"]
            room.created_at, room.closed_at, room.last_ts = info["created_at"], info["closed_at"], info["last_ts"]
            room.unread, room.member_count, room.joined = info["unread"], info["members"], info["member"]
        names = {info["name"] for info in listed}
        for gone in [n for n in self.rooms if n not in names]:
            del self.rooms[gone]
        if self.selected not in self.rooms:
            self.selected = None

    def open_rooms(self) -> list[Room]:
        return sorted((r for r in self.rooms.values() if r.open), key=lambda r: r.activity, reverse=True)

    def closed_rooms(self) -> list[Room]:
        return sorted((r for r in self.rooms.values() if not r.open), key=lambda r: r.activity, reverse=True)

    def order(self) -> list[Room]:
        """The conversation list: open rooms by latest activity, then closed rooms."""
        return self.open_rooms() + self.closed_rooms()

    @property
    def current(self) -> Room | None:
        return self.rooms.get(self.selected) if self.selected else None

    def total_unread(self) -> int:
        return sum(r.unread for r in self.rooms.values())

    def select(self, name: str) -> None:
        room = self.rooms[name]
        self.selected = name
        if not room.loaded:
            self._load(room)
        self.mark_read(room)

    def move(self, step: int) -> None:
        """Select the next (+1) or previous (-1) conversation in the list."""
        rooms = self.order()
        if not rooms:
            return
        names = [r.name for r in rooms]
        at = names.index(self.selected) if self.selected in names else (-1 if step > 0 else 0)
        self.select(names[max(0, min(len(names) - 1, at + step))])

    def _load(self, room: Room) -> None:
        room.messages = self.request("tail", room=room.name, n=PAGE)["messages"]
        room.exhausted = len(room.messages) < PAGE
        # reading from far beyond the end returns no messages but always returns the pinned decisions
        room.decisions = self.request("read", room=room.name, member=self.me, since=10**12)["decisions"]
        self.refresh_members(room)
        room.loaded = True

    def refresh_members(self, room: Room | None = None) -> None:
        room = room or self.current
        if room:
            room.members = {m["name"]: m for m in self.request("members", room=room.name)["members"]}

    def mark_read(self, room: Room) -> None:
        """Move my read position to the end. Only members have one, so looking at a room never joins it."""
        if room.joined and room.unread:
            self.request("read", room=room.name, member=self.me)
        room.unread = 0

    def load_older(self) -> bool:
        """Load an earlier page of the selected room. Returns False when there is nothing older."""
        room = self.current
        if room is None or room.exhausted:
            return False
        want = len(room.messages) + PAGE
        older = self.request("tail", room=room.name, n=want)["messages"]
        room.exhausted = len(older) < want
        gained = len(older) > len(room.messages)
        room.messages = older
        return gained

    # -- events --------------------------------------------------------

    def apply(self, ev: dict) -> bool:
        """Fold one event from the daemon's stream into the state. Returns True if anything on screen changed."""
        kind, name = ev.get("event"), ev.get("room_name")
        if kind == "room_created":
            info = ev["room"]
            self.rooms.setdefault(info["name"], Room(info["name"])).created_at = info["created_at"]
            self.load_rooms()
            return True
        if name not in self.rooms:
            if kind in ("message", "closed", "room_updated"):
                self.load_rooms()  # a room we have not seen: pick it up
                return name in self.rooms
            return False
        room = self.rooms[name]
        if kind == "message":
            m = ev["message"]
            room.last_ts = m["ts"]
            if room.loaded and not any(x["id"] == m["id"] for x in room.messages):
                room.messages.append(m)
                if m["kind"] == "decision":
                    room.decisions.append(m)
            if m["kind"] != "system" and m["author"] != self.me:
                if name == self.selected:  # I am looking at it: it is read as it arrives
                    room.unread = 1 if room.joined else 0
                    self.mark_read(room)
                elif room.joined:
                    room.unread += 1
            return True
        if kind == "closed":
            info = ev["room"]
            room.status, room.closed_at = "closed", info.get("closed_at")
            return True
        if kind == "room_updated":
            if name == self.selected:
                self.refresh_members(room)
            return True
        if kind == "warning":
            self.notice = f"{name}: {ev['text']}"
            return True
        return False

    # -- actions -------------------------------------------------------

    def preview(self, text: str, fyi: bool = False) -> dict:
        room = self._need_open()
        pre = self.request("wake_preview", room=room.name, author=self.me, body=text, no_reply_needed=fyi, **{"from": "human"})
        self.confirm_over = pre["confirm_over"]
        return pre

    def needs_confirm(self, pre: dict) -> bool:
        return bool(pre["confirm_over"]) and not pre["passive"] and pre["total"] >= pre["confirm_over"]

    def post(self, text: str, fyi: bool = False, decision: bool = False) -> str:
        room = self._need_open()
        res = self.request(
            "post", room=room.name, author=self.me, body=text, kind="decision" if decision else "post",
            no_reply_needed=fyi, **{"from": "human"},
        )
        room.joined = True  # posting joins a room
        self.refresh_members(room)
        w = res["wake"]
        parts = []
        if w["woke"]:
            parts.append("woke " + ", ".join(w["woke"]))
        if w["passive"]:
            parts.append("nobody woken (fyi)")
        if w["suppressed"]:
            parts.append("nobody woken: " + w["suppressed"])
        if w["unreachable"]:
            parts.append("not reached: " + ", ".join(w["unreachable"]))
        self.notice = "; ".join(parts)
        return self.notice

    def close_room(self) -> dict:
        room = self._need_open()
        res = self.request("close_room", name=room.name, by=self.me)
        room.status = "closed"
        self.notice = f"closed {room.name}" + (f", saved to {res['exported']}" if res.get("exported") else "")
        return res

    def set_muted(self, member: str, muted: bool) -> None:
        room = self._need_open()
        self.request("mute" if muted else "unmute", room=room.name, member=member)
        self.refresh_members(room)
        self.notice = f"{'muted' if muted else 'unmuted'} {member}"

    def invite(self, names: list[str]) -> str:
        room = self._need_open()
        res = self.request("invite", room=room.name, by=self.me, names=names)
        parts = []
        for label, key in (("added", "added"), ("already in", "already"), ("not reached", "unreachable")):
            if res[key]:
                parts.append(f"{label}: {', '.join(res[key])}")
        self.refresh_members(room)
        self.notice = "; ".join(parts) or "nobody to add"
        return self.notice

    def wrapup_text(self, agent: str) -> str:
        return summary.wrapup_request(agent)

    def summary_text(self) -> str:
        room = self.current
        if room is None:
            raise AcmError("bad_request", "no conversation selected")
        return self.request("summary", room=room.name)["summary"]

    def _need_open(self) -> Room:
        room = self.current
        if room is None:
            raise AcmError("bad_request", "no conversation selected")
        if not room.open:
            raise AcmError("room_closed", f"{room.name} is closed (read-only)")
        return room
