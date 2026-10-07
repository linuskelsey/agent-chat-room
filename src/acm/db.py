"""SQLite store: rooms, members and an append-only message log."""

import json
import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

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
            Path(path).parent.mkdir(parents=True, exist_ok=True)
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
                       AND g.id > m.cursor AND g.author != :member) AS unread
              FROM rooms r
             WHERE (:status IS NULL OR r.status = :status)
             ORDER BY r.id
            """,
            {"status": status, "member": member},
        ).fetchall()
        out = []
        for row in rows:
            room = self.get_room(row["name"])
            room.update(members=row["members"], messages=row["messages"], last_ts=row["last_ts"], unread=row["unread"])
            out.append(room)
        return out

    def close_room(self, name: str, by: str, force: bool = False) -> dict:
        """Close a room. Only the creator may close it unless `force` (the human kill switch)."""
        self._check_member_name(by)
        with self._tx():
            room = self._open_room(name)
            if not force and room["created_by"] != by:
                raise AcmError("forbidden", f"only the creator ({room['created_by']}) can close {name}")
            now = time.time()
            self.conn.execute(
                "UPDATE rooms SET status='closed', closed_at=?, closed_by=? WHERE id=?", (now, by, room["id"])
            )
            how = "killed" if force and room["created_by"] != by else "closed"
            self._insert(room["id"], "system", "system", "system", f"room {how} by {by}", now=now)
        return self.get_room(name)

    # -- members -------------------------------------------------------

    def _ensure_member(self, room: sqlite3.Row, name: str, kind: str) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM members WHERE room_id = ? AND name = ?", (room["id"], name)
        ).fetchone()
        if row is None:
            self.conn.execute(
                "INSERT INTO members (room_id, name, kind, joined_at) VALUES (?, ?, ?, ?)",
                (room["id"], name, kind, time.time()),
            )
            row = self.conn.execute(
                "SELECT * FROM members WHERE room_id = ? AND name = ?", (room["id"], name)
            ).fetchone()
        return row

    def join(self, room: str, member: str, kind: str = "human") -> dict:
        self._check_member_name(member)
        if kind not in MEMBER_KINDS:
            raise AcmError("bad_request", f"invalid member kind: {kind}")
        with self._tx():
            self._ensure_member(self._open_room(room), member, kind)
        return self.members(room)[member]

    def leave(self, room: str, member: str) -> None:
        with self._tx():
            r = self._room(room)
            self.conn.execute("DELETE FROM members WHERE room_id = ? AND name = ?", (r["id"], member))

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
        rows = self.conn.execute("SELECT * FROM members WHERE room_id = ? ORDER BY joined_at, name", (r["id"],))
        return {
            m["name"]: {
                "name": m["name"],
                "kind": m["kind"],
                "joined_at": m["joined_at"],
                "cursor": m["cursor"],
                "muted": bool(m["muted"]),
            }
            for m in rows
        }

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
            " WHERE r.name = ? AND g.id > ? AND g.author != ?",
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
        if refs is not None and not (isinstance(refs, list) and all(isinstance(x, str) for x in refs)):
            raise AcmError("bad_request", "refs must be a list of strings")
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
        }

    def tail(self, room: str, n: int) -> list[dict]:
        """The last `n` messages, oldest first. Never touches cursors."""
        r = self._room(room)
        rows = self.conn.execute(
            "SELECT * FROM (SELECT * FROM messages WHERE room_id = ? ORDER BY id DESC LIMIT ?) ORDER BY id",
            (r["id"], n),
        ).fetchall()
        return [_message(x, room) for x in rows]
