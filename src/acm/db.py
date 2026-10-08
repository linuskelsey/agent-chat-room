"""SQLite store: rooms, members and an append-only message log."""

import json
import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from acm import config, fsutil, textsafe
from acm.errors import AcmError

ROOM_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
MEMBER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
MENTION_RE = re.compile(r"(?<![\w@])@([A-Za-z0-9][A-Za-z0-9._-]*)")

MEMBER_KINDS = ("agent", "human")
MESSAGE_KINDS = ("post", "system", "decision", "human_approve")

# Each entry migrates the schema from version i to i + 1 (tracked in PRAGMA user_version).
MIGRATIONS = [
    """
    CREATE TABLE rooms (
        id         INTEGER PRIMARY KEY,
        name       TEXT NOT NULL UNIQUE,
        topic      TEXT NOT NULL DEFAULT '',
        status     TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
        created_by TEXT NOT NULL,
        created_at REAL NOT NULL,
        closed_at  REAL,
        closed_by  TEXT
    );
    CREATE TABLE members (
        room_id   INTEGER NOT NULL REFERENCES rooms(id),
        name      TEXT NOT NULL,
        kind      TEXT NOT NULL CHECK (kind IN ('agent', 'human')),
        joined_at REAL NOT NULL,
        cursor    INTEGER NOT NULL DEFAULT 0,
        muted     INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (room_id, name)
    );
    CREATE TABLE messages (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        room_id         INTEGER NOT NULL REFERENCES rooms(id),
        author          TEXT NOT NULL,
        kind            TEXT NOT NULL CHECK (kind IN ('post', 'system', 'decision', 'human_approve')),
        from_kind       TEXT NOT NULL CHECK (from_kind IN ('agent', 'human', 'system')),
        mentions        TEXT NOT NULL DEFAULT '[]',
        body            TEXT NOT NULL,
        refs            TEXT NOT NULL DEFAULT '[]',
        no_reply_needed INTEGER NOT NULL DEFAULT 0,
        ts              REAL NOT NULL
    );
    CREATE INDEX messages_room_id ON messages(room_id, id);
    """,
    """
    CREATE TABLE agents (
        name       TEXT PRIMARY KEY,
        pid        INTEGER NOT NULL,
        inbox      TEXT NOT NULL,
        updated_at REAL NOT NULL
    );
    """,
    """
    ALTER TABLE rooms ADD COLUMN limits TEXT NOT NULL DEFAULT '{}';
    ALTER TABLE members ADD COLUMN strikes INTEGER NOT NULL DEFAULT 0;
    CREATE TABLE usage (
        id             INTEGER PRIMARY KEY,
        room_id        INTEGER NOT NULL REFERENCES rooms(id),
        agent          TEXT NOT NULL,
        ts             REAL NOT NULL,
        kind           TEXT NOT NULL CHECK (kind IN ('wake', 'post')),
        input          INTEGER NOT NULL DEFAULT 0,
        output         INTEGER NOT NULL DEFAULT 0,
        cache_creation INTEGER NOT NULL DEFAULT 0,
        cache_read     INTEGER NOT NULL DEFAULT 0,
        weighted       REAL NOT NULL
    );
    CREATE INDEX usage_room ON usage(room_id);
    """,
    """
    ALTER TABLE members ADD COLUMN seen_at REAL;
    UPDATE members SET seen_at = joined_at;
    """,
    """
    CREATE TABLE snooze (
        name  TEXT PRIMARY KEY,
        until REAL NOT NULL
    );
    """,
    """
    ALTER TABLE rooms ADD COLUMN project_dir TEXT;
    """,
]


def parse_mentions(body: str) -> list[str]:
    seen: list[str] = []
    for m in MENTION_RE.finditer(body):
        name = m.group(1).rstrip("._-")
        if name and name not in seen:
            seen.append(name)
    return seen


def _message(row: sqlite3.Row, room: str) -> dict:
    return {
        "id": row["id"],
        "room": room,
        "author": row["author"],
        "kind": row["kind"],
        "from": row["from_kind"],
        "mentions": json.loads(row["mentions"]),
        "body": row["body"],
        "refs": json.loads(row["refs"]),
        "no_reply_needed": bool(row["no_reply_needed"]),
        "ts": row["ts"],
    }


class Store:
    def __init__(self, path: str | Path):
        if str(path) != ":memory:":
            fsutil.ensure_private_dir(Path(path).parent)
        self.on_system = None  # called with (room name, message) for each system line, so the daemon can push it live
        self.conn = sqlite3.connect(str(path), isolation_level=None, timeout=10)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    def close(self) -> None:
        self.conn.close()

    def _migrate(self) -> None:
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        for i in range(version, len(MIGRATIONS)):
            self.conn.executescript(f"BEGIN;{MIGRATIONS[i]}PRAGMA user_version={i + 1};COMMIT;")

    @contextmanager
    def _tx(self):
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        self.conn.execute("COMMIT")

    # -- rooms ---------------------------------------------------------

    def _room(self, name: str) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM rooms WHERE name = ?", (name,)).fetchone()
        if row is None:
            raise AcmError("not_found", f"no such room: {name}")
        return row

    def _open_room(self, name: str) -> sqlite3.Row:
        room = self._room(name)
        if room["status"] != "open":
            raise AcmError("room_closed", f"room is closed: {name}")
        return room

    @staticmethod
    def _check_member_name(name: str) -> None:
        if not isinstance(name, str) or not MEMBER_RE.match(name):
            raise AcmError("bad_name", f"invalid member name: {name!r}")

    def create_room(self, name: str, creator: str, topic: str = "") -> dict:
        if not isinstance(name, str) or not ROOM_RE.match(name):
            raise AcmError("bad_name", "room name must be lowercase letters, digits, '.', '_' or '-' (max 64)")
        self._check_member_name(creator)
        topic = textsafe.one_line(topic) if isinstance(topic, str) else ""
        now = time.time()
        with self._tx():
            if self.conn.execute("SELECT 1 FROM rooms WHERE name = ?", (name,)).fetchone():
                raise AcmError("exists", f"room already exists: {name}")
            cur = self.conn.execute(
                "INSERT INTO rooms (name, topic, created_by, created_at) VALUES (?, ?, ?, ?)",
                (name, topic, creator, now),
            )
            self.conn.execute(
                "INSERT INTO members (room_id, name, kind, joined_at) VALUES (?, ?, 'human', ?)",
                (cur.lastrowid, creator, now),
            )
        return self.get_room(name)

    def get_room(self, name: str) -> dict:
        r = self._room(name)
        return {
            "name": r["name"],
            "topic": r["topic"],
            "status": r["status"],
            "created_by": r["created_by"],
            "created_at": r["created_at"],
            "closed_at": r["closed_at"],
            "closed_by": r["closed_by"],
            "project_dir": r["project_dir"],
        }

    def list_rooms(self, status: str | None = None, member: str | None = None) -> list[dict]:
        rows = self.conn.execute(
            """
            SELECT r.name,
                   (SELECT COUNT(*) FROM members m WHERE m.room_id = r.id) AS members,
                   (SELECT COUNT(*) FROM messages g WHERE g.room_id = r.id) AS messages,
                   (SELECT MAX(ts) FROM messages g WHERE g.room_id = r.id) AS last_ts,
                   (SELECT COUNT(*) FROM messages g, members m
                     WHERE g.room_id = r.id AND m.room_id = r.id AND m.name = :member
                       AND g.id > m.cursor AND g.author != :member AND g.kind != 'system') AS unread,
                   EXISTS(SELECT 1 FROM members m WHERE m.room_id = r.id AND m.name = :member) AS is_member
              FROM rooms r
             WHERE (:status IS NULL OR r.status = :status)
             ORDER BY r.id
            """,
            {"status": status, "member": member},
        ).fetchall()
        out = []
        for row in rows:
            room = self.get_room(row["name"])
            room.update(
                members=row["members"], messages=row["messages"], last_ts=row["last_ts"], unread=row["unread"],
                member=bool(row["is_member"]),
            )
            out.append(room)
        return out

    def close_room(
        self, name: str, by: str, force: bool = False, reason: str | None = None, summary: str | None = None
    ) -> dict:
        """Close a room. Only the creator may close it unless `force` (the human kill switch).

        `summary` is written into the room after the closing line, as part of the same final system message.
        """
        self._check_member_name(by)
        with self._tx():
            room = self._open_room(name)
            if not force and room["created_by"] != by:
                raise AcmError("forbidden", f"only the creator ({room['created_by']}) can close {name}")
            now = time.time()
            self.conn.execute(
                "UPDATE rooms SET status='closed', closed_at=?, closed_by=? WHERE id=?", (now, by, room["id"])
            )
            how = "killed" if force and reason is None and room["created_by"] != by else "closed"
            self._insert(
                room["id"], "system", "system", "system",
                f"room {how} by {by}" + (f": {reason}" if reason else "") + (f"\n\n{summary}" if summary else ""),
                now=now,
            )
        return self.get_room(name)

    def delete_room(self, name: str) -> dict:
        """Remove a room with its members, messages and usage. Nothing about it is kept."""
        with self._tx():
            room = self._room(name)
            counts = {
                table: self.conn.execute(f"DELETE FROM {table} WHERE room_id = ?", (room["id"],)).rowcount
                for table in ("messages", "members", "usage")
            }
            self.conn.execute("DELETE FROM rooms WHERE id = ?", (room["id"],))
        return counts

    # -- members -------------------------------------------------------

    def _ensure_member(self, room: sqlite3.Row, name: str, kind: str, pending: bool = False) -> sqlite3.Row:
        """The member's row, creating it. An agent that actually shows up gets a "joined" line in the room.

        `pending` adds the member without that line (an invited agent has not joined until it acts).
        """
        row = self.conn.execute(
            "SELECT * FROM members WHERE room_id = ? AND name = ?", (room["id"], name)
        ).fetchone()
        now = time.time()
        if row is None:
            self.conn.execute(
                "INSERT INTO members (room_id, name, kind, joined_at, seen_at) VALUES (?, ?, ?, ?, ?)",
                (room["id"], name, kind, now, None if pending else now),
            )
            if kind == "agent" and not pending:
                self._insert(room["id"], "system", "system", "system", f"{name} joined", now=now)
        elif row["seen_at"] is None and not pending:
            self.conn.execute(
                "UPDATE members SET seen_at = ? WHERE room_id = ? AND name = ?", (now, room["id"], name)
            )
            if row["kind"] == "agent":
                self._insert(room["id"], "system", "system", "system", f"{name} joined", now=now)
        return self.conn.execute(
            "SELECT * FROM members WHERE room_id = ? AND name = ?", (room["id"], name)
        ).fetchone()

    def join(self, room: str, member: str, kind: str = "human", pending: bool = False) -> dict:
        self._check_member_name(member)
        if kind not in MEMBER_KINDS:
            raise AcmError("bad_request", f"invalid member kind: {kind}")
        with self._tx():
            self._ensure_member(self._open_room(room), member, kind, pending)
        return self.members(room)[member]

    def leave(self, room: str, member: str) -> None:
        with self._tx():
            r = self._room(room)
            row = self.conn.execute(
                "SELECT kind FROM members WHERE room_id = ? AND name = ?", (r["id"], member)
            ).fetchone()
            self.conn.execute("DELETE FROM members WHERE room_id = ? AND name = ?", (r["id"], member))
            if row is not None and row["kind"] == "agent" and r["status"] == "open":
                self._insert(r["id"], "system", "system", "system", f"{member} left")

    def set_muted(self, room: str, member: str, muted: bool) -> None:
        with self._tx():
            r = self._open_room(room)
            cur = self.conn.execute(
                "UPDATE members SET muted = ? WHERE room_id = ? AND name = ?", (int(muted), r["id"], member)
            )
            if cur.rowcount == 0:
                raise AcmError("not_found", f"{member} is not a member of {room}")

    def members(self, room: str) -> dict[str, dict]:
        r = self._room(room)
        snoozed = self.snoozed()
        rows = self.conn.execute("SELECT * FROM members WHERE room_id = ? ORDER BY joined_at, name", (r["id"],))
        return {
            m["name"]: {
                "name": m["name"],
                "kind": m["kind"],
                "joined_at": m["joined_at"],
                "cursor": m["cursor"],
                "muted": bool(m["muted"]),
                "strikes": m["strikes"],
                "joined": m["seen_at"] is not None,
                "snoozed_until": snoozed.get(m["name"]),
                "wake_cost": self.wake_cost(m["name"]) if m["kind"] == "agent" else None,
            }
            for m in rows
        }

    def set_project_dir(self, room: str, path: str | None) -> dict:
        """Link a room to a project directory (None unlinks). Used to resolve refs in summaries."""
        with self._tx():
            r = self._room(room)
            self.conn.execute("UPDATE rooms SET project_dir = ? WHERE id = ?", (path, r["id"]))
        return self.get_room(room)

    def search(self, query: str, room: str | None = None, author: str | None = None,
               status: str | None = None, limit: int = 50, include_system: bool = False) -> list[dict]:
        """Messages containing `query` (case-insensitive), newest first, across rooms."""
        if not isinstance(query, str) or not query.strip():
            raise AcmError("bad_request", "search needs some text")
        escaped = query.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        rows = self.conn.execute(
            """
            SELECT g.*, r.name AS room_name, r.status AS room_status
              FROM messages g JOIN rooms r ON r.id = g.room_id
             WHERE g.body LIKE :q ESCAPE '\\'
               AND (:room IS NULL OR r.name = :room)
               AND (:author IS NULL OR g.author = :author)
               AND (:status IS NULL OR r.status = :status)
               AND (:system = 1 OR g.kind != 'system')
             ORDER BY g.id DESC LIMIT :limit
            """,
            {"q": f"%{escaped}%", "room": room, "author": author, "status": status,
             "system": int(include_system), "limit": max(1, min(int(limit), 500))},
        ).fetchall()
        return [{**_message(x, x["room_name"]), "room_status": x["room_status"]} for x in rows]

    # -- limits, strikes and usage ----------------------------------------

    def room_overrides(self, room: str) -> dict:
        return json.loads(self._room(room)["limits"])

    def limits(self, room: str) -> dict:
        """The effective limits of a room: defaults, then the config file, then the room's own settings."""
        return config.effective(self.room_overrides(room))

    def set_limits(self, room: str, updates: dict) -> dict:
        """Change a room's own settings. A value of None switches a setting off, "default" removes the override."""
        with self._tx():
            r = self._room(room)
            own = json.loads(r["limits"])
            for key, value in updates.items():
                if isinstance(value, str) and value.lower() == "default":
                    own.pop(key, None)
                    config.coerce(key, None)  # still validates the key
                else:
                    own[key] = config.coerce(key, value)
            self.conn.execute("UPDATE rooms SET limits = ? WHERE id = ?", (json.dumps(own), r["id"]))
        return self.limits(room)

    def add_strike(self, room: str, member: str) -> int:
        with self._tx():
            r = self._room(room)
            self.conn.execute(
                "UPDATE members SET strikes = strikes + 1 WHERE room_id = ? AND name = ?", (r["id"], member)
            )
            return self.conn.execute(
                "SELECT strikes FROM members WHERE room_id = ? AND name = ?", (r["id"], member)
            ).fetchone()[0]

    def add_usage(self, room: str, agent: str, kind: str, input: int = 0, output: int = 0,
                  cache_creation: int = 0, cache_read: int = 0) -> float:
        weighted = config.weighted_tokens(input, output, cache_creation, cache_read)
        r = self._room(room)
        self.conn.execute(
            "INSERT INTO usage (room_id, agent, ts, kind, input, output, cache_creation, cache_read, weighted)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (r["id"], agent, time.time(), kind, input, output, cache_creation, cache_read, weighted),
        )
        return weighted

    def usage(self, room: str) -> dict:
        r = self._room(room)
        by_agent = {
            row["agent"]: round(row["w"])
            for row in self.conn.execute(
                "SELECT agent, SUM(weighted) AS w FROM usage WHERE room_id = ? GROUP BY agent ORDER BY w DESC",
                (r["id"],),
            )
        }
        tot = self.conn.execute(
            "SELECT COALESCE(SUM(weighted),0), COALESCE(SUM(CASE WHEN kind='wake' THEN weighted END),0),"
            " COALESCE(SUM(CASE WHEN kind='post' THEN weighted END),0) FROM usage WHERE room_id = ?",
            (r["id"],),
        ).fetchone()
        return {"weighted": round(tot[0]), "wake": round(tot[1]), "post": round(tot[2]), "by_agent": by_agent}

    COLD_CACHE_CREATION = 20000  # a wake that wrote this much cache found the agent's context expired

    def wake_cost(self, agent: str) -> dict:
        """What waking this agent has cost lately (weighted tokens): the usual warm wake and the cold one.

        Based on its last 30 wakes in any room; falls back to typical figures when there is no history.
        """
        rows = self.conn.execute(
            "SELECT weighted, cache_creation FROM usage WHERE agent = ? AND kind = 'wake' ORDER BY id DESC LIMIT 30",
            (agent,),
        ).fetchall()
        warm = [r["weighted"] for r in rows if r["cache_creation"] <= self.COLD_CACHE_CREATION]
        cold = [r["weighted"] for r in rows if r["cache_creation"] > self.COLD_CACHE_CREATION]
        return {
            "warm": sum(warm) / len(warm) if warm else 25000.0,
            "cold": sum(cold) / len(cold) if cold else 90000.0,
            "n": len(rows),
        }

    def usage_since(self, room: str, since: float) -> float:
        r = self._room(room)
        return self.conn.execute(
            "SELECT COALESCE(SUM(weighted), 0) FROM usage WHERE room_id = ? AND kind = 'wake' AND ts >= ?",
            (r["id"], since),
        ).fetchone()[0]

    def budget_status(self, room: str) -> dict:
        """How much of each cap a room has used. `exceeded` and `warn` list the caps that need action."""
        r = self._room(room)
        limits = self.limits(room)
        messages = self.conn.execute(
            "SELECT COUNT(*) FROM messages WHERE room_id = ? AND kind != 'system'", (r["id"],)
        ).fetchone()[0]
        used = {
            "max_messages": messages,
            "max_minutes": (time.time() - r["created_at"]) / 60,
            "max_tokens": self.usage(room)["weighted"],
        }
        exceeded, warn = [], []
        for key, value in used.items():
            cap = limits[key]
            if not cap:
                continue
            if value >= cap:
                exceeded.append(key)
            elif value >= cap * limits["warn_fraction"]:
                warn.append(key)
        return {"used": used, "limits": limits, "exceeded": exceeded, "warn": warn}

    # -- agent sessions ------------------------------------------------

    def register_agent(self, name: str, pid: int, inbox: str) -> None:
        """Remember which Claude Code session (and inbox socket) currently answers to `name`."""
        self._check_member_name(name)
        if not isinstance(pid, int) or not isinstance(inbox, str) or not inbox:
            raise AcmError("bad_request", "register needs an integer pid and an inbox path")
        self.conn.execute(
            "INSERT INTO agents (name, pid, inbox, updated_at) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(name) DO UPDATE SET pid=excluded.pid, inbox=excluded.inbox, updated_at=excluded.updated_at",
            (name, pid, inbox, time.time()),
        )

    def snooze(self, name: str, seconds: float) -> float:
        """Hold wakes for an agent (in every room) until the returned time."""
        self._check_member_name(name)
        if seconds <= 0:
            raise AcmError("bad_request", "snooze duration must be positive")
        until = time.time() + seconds
        self.conn.execute(
            "INSERT INTO snooze (name, until) VALUES (?, ?) ON CONFLICT(name) DO UPDATE SET until = excluded.until",
            (name, until),
        )
        return until

    def unsnooze(self, name: str) -> None:
        self.conn.execute("DELETE FROM snooze WHERE name = ?", (name,))

    def snoozed(self) -> dict[str, float]:
        """Agents whose wakes are currently held, with the time each snooze ends."""
        now = time.time()
        self.conn.execute("DELETE FROM snooze WHERE until <= ?", (now,))
        return {r["name"]: r["until"] for r in self.conn.execute("SELECT name, until FROM snooze")}

    def agents_of(self, pid: int) -> list[dict]:
        """Every name registered to this session pid (more than one after a rename)."""
        return [dict(r) for r in self.conn.execute("SELECT name, pid, inbox FROM agents WHERE pid = ?", (pid,))]

    def get_agent(self, name: str) -> dict | None:
        row = self.conn.execute("SELECT name, pid, inbox FROM agents WHERE name = ?", (name,)).fetchone()
        return dict(row) if row else None

    def cursor_of(self, room: str, member: str) -> int:
        row = self.conn.execute(
            "SELECT m.cursor FROM members m JOIN rooms r ON r.id = m.room_id WHERE r.name = ? AND m.name = ?",
            (room, member),
        ).fetchone()
        return row["cursor"] if row else 0

    def unread_count(self, room: str, member: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) FROM messages g JOIN rooms r ON r.id = g.room_id"
            " WHERE r.name = ? AND g.id > ? AND g.author != ? AND g.kind != 'system'",
            (room, self.cursor_of(room, member), member),
        ).fetchone()
        return row[0]

    # -- messages ------------------------------------------------------

    def _insert(
        self,
        room_id: int,
        author: str,
        kind: str,
        from_kind: str,
        body: str,
        refs: list | None = None,
        no_reply_needed: bool = False,
        now: float | None = None,
    ) -> int:
        cur = self._insert_row(
            room_id, author, kind, from_kind, body, refs, no_reply_needed, now
        )
        if from_kind == "system" and self.on_system is not None:
            row = self.conn.execute("SELECT * FROM messages WHERE id = ?", (cur,)).fetchone()
            name = self.conn.execute("SELECT name FROM rooms WHERE id = ?", (room_id,)).fetchone()["name"]
            self.on_system(name, _message(row, name))
        return cur

    def _insert_row(self, room_id, author, kind, from_kind, body, refs, no_reply_needed, now) -> int:
        cur = self.conn.execute(
            "INSERT INTO messages (room_id, author, kind, from_kind, mentions, body, refs, no_reply_needed, ts)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                room_id,
                author,
                kind,
                from_kind,
                json.dumps(parse_mentions(body) if from_kind != "system" else []),
                body,
                json.dumps(refs or []),
                int(no_reply_needed),
                now if now is not None else time.time(),
            ),
        )
        return cur.lastrowid

    def post_system(self, room: str, body: str) -> None:
        with self._tx():
            r = self._open_room(room)
            self._insert(r["id"], "system", "system", "system", body)

    def post(
        self,
        room: str,
        author: str,
        body: str,
        kind: str = "post",
        from_kind: str = "human",
        refs: list | None = None,
        no_reply_needed: bool = False,
    ) -> dict:
        """Append a message. The author joins the room if not already a member."""
        self._check_member_name(author)
        if kind not in ("post", "decision", "human_approve"):
            raise AcmError("bad_request", f"cannot post a message of kind {kind!r}")
        if from_kind not in MEMBER_KINDS:
            raise AcmError("bad_request", f"invalid sender kind: {from_kind}")
        if kind == "human_approve" and from_kind != "human":
            raise AcmError("forbidden", "only humans can post approvals")
        if not isinstance(body, str) or not body.strip():
            raise AcmError("bad_request", "message body is empty")
        body = textsafe.clean(body)
        if refs is not None and not (isinstance(refs, list) and all(isinstance(x, str) for x in refs)):
            raise AcmError("bad_request", "refs must be a list of strings")
        refs = [textsafe.one_line(x) for x in refs] if refs else refs
        with self._tx():
            r = self._open_room(room)
            member = self._ensure_member(r, author, from_kind)
            if member["muted"]:
                raise AcmError("muted", f"{author} is muted in {room}")
            mid = self._insert(r["id"], author, kind, from_kind, body.strip(), refs, no_reply_needed)
            row = self.conn.execute("SELECT * FROM messages WHERE id = ?", (mid,)).fetchone()
        return _message(row, room)

    def read(
        self,
        room: str,
        member: str,
        since: int | None = None,
        peek: bool = False,
        limit: int | None = None,
        exclude_own: bool = False,
        decisions: str = "all",
        kind: str = "human",
    ) -> dict:
        """Messages after `since`, or after the member's cursor when `since` is None.

        Reading from the cursor advances it unless `peek`. An explicit `since` never touches the cursor.
        `exclude_own` drops the member's own posts (the cursor still moves past them).
        `decisions="all"` always returns every pinned decision; `"auto"` returns them only to a fresh
        reader (cursor 0) or when the window contains a new one, so polling agents do not re-read them.
        """
        self._check_member_name(member)
        if decisions not in ("all", "auto"):
            raise AcmError("bad_request", f"invalid decisions mode: {decisions}")
        with self._tx():
            r = self._room(room)
            advance = since is None and not peek
            if since is None:
                if r["status"] == "open":
                    start = self._ensure_member(r, member, kind)["cursor"]
                else:
                    m = self.conn.execute(
                        "SELECT cursor FROM members WHERE room_id = ? AND name = ?", (r["id"], member)
                    ).fetchone()
                    start = m["cursor"] if m else 0
            else:
                start = since
            sql = "SELECT * FROM messages WHERE room_id = ? AND id > ?"
            args: list = [r["id"], start]
            if exclude_own:
                sql += " AND author != ?"
                args.append(member)
            sql += " ORDER BY id"
            if limit is not None:
                sql += " LIMIT ?"
                args.append(limit)
            rows = self.conn.execute(sql, args).fetchall()
            messages = [_message(x, room) for x in rows]
            if limit is not None and len(rows) == limit:
                cursor = rows[-1]["id"]  # more may follow
            else:
                top = self.conn.execute("SELECT MAX(id) FROM messages WHERE room_id = ?", (r["id"],)).fetchone()[0]
                cursor = max(start, top or 0)
            if advance and cursor != start:
                self.conn.execute(
                    "UPDATE members SET cursor = ? WHERE room_id = ? AND name = ?", (cursor, r["id"], member)
                )
            pinned = [
                _message(x, room)
                for x in self.conn.execute(
                    "SELECT * FROM messages WHERE room_id = ? AND kind = 'decision' ORDER BY id", (r["id"],)
                )
            ]
            if decisions == "auto" and start != 0 and not any(d["id"] > start for d in pinned):
                pinned = []
        return {"messages": messages, "decisions": pinned, "cursor": cursor}

    def catch_up(self, room: str, member: str, keep: int = 10, kind: str = "agent") -> dict:
        """Join a room and skip to the present: the last `keep` messages and all pinned decisions.

        The member's cursor moves to the newest message, so a later read returns only what is new.
        """
        self._check_member_name(member)
        if kind not in MEMBER_KINDS:
            raise AcmError("bad_request", f"invalid member kind: {kind}")
        with self._tx():
            r = self._open_room(room)
            self._ensure_member(r, member, kind)
            rows = self.conn.execute(
                "SELECT * FROM (SELECT * FROM messages WHERE room_id = ? ORDER BY id DESC LIMIT ?) ORDER BY id",
                (r["id"], keep),
            ).fetchall()
            top = self.conn.execute("SELECT MAX(id) FROM messages WHERE room_id = ?", (r["id"],)).fetchone()[0] or 0
            self.conn.execute(
                "UPDATE members SET cursor = MAX(cursor, ?) WHERE room_id = ? AND name = ?", (top, r["id"], member)
            )
            pinned = self.conn.execute(
                "SELECT * FROM messages WHERE room_id = ? AND kind = 'decision' ORDER BY id", (r["id"],)
            ).fetchall()
        return {
            "room": self.get_room(room),
            "members": list(self.members(room).values()),
            "messages": [_message(x, room) for x in rows],
            "decisions": [_message(x, room) for x in pinned],
            "cursor": top,
            "limits": self.limits(room),
        }

    def last_message_id(self, room: str) -> int:
        """The id of the newest message that is not a join, leave or close line (0 if there is none)."""
        r = self._room(room)
        row = self.conn.execute(
            "SELECT MAX(id) FROM messages WHERE room_id = ? AND kind != 'system'", (r["id"],)
        ).fetchone()
        return row[0] or 0

    def tail(self, room: str, n: int) -> list[dict]:
        """The last `n` messages, oldest first. Never touches cursors."""
        r = self._room(room)
        rows = self.conn.execute(
            "SELECT * FROM (SELECT * FROM messages WHERE room_id = ? ORDER BY id DESC LIMIT ?) ORDER BY id",
            (r["id"], n),
        ).fetchall()
        return [_message(x, room) for x in rows]
