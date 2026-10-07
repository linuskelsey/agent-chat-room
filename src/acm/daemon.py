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
import sys
import traceback
from pathlib import Path

from acm import paths
from acm.db import Store
from acm.errors import AcmError

LINE_LIMIT = 8 * 1024 * 1024


def _need(req: dict, key: str):
    if key not in req:
        raise AcmError("bad_request", f"missing field: {key}")
    return req[key]


class Daemon:
    def __init__(self, store: Store):
        self.store = store
        self.watchers: dict[str, set[asyncio.Queue]] = {}
        self.stop = asyncio.Event()

    def _publish(self, room: str, event: dict) -> None:
        for q in self.watchers.get(room, ()):
            q.put_nowait(event)

    def dispatch(self, req: dict) -> dict:
        s = self.store
        op = _need(req, "op")
        if op == "ping":
            return {"pid": os.getpid()}
        if op == "shutdown":
            self.stop.set()
            return {}
        if op == "create_room":
            return {"room": s.create_room(_need(req, "name"), _need(req, "by"), req.get("topic", ""))}
        if op == "get_room":
            return {"room": s.get_room(_need(req, "name"))}
        if op == "list_rooms":
            return {"rooms": s.list_rooms(req.get("status"), req.get("member"))}
        if op in ("close_room", "kill_room"):
            room = s.close_room(_need(req, "name"), _need(req, "by"), force=op == "kill_room")
            self._publish(room["name"], {"event": "closed", "room": room})
            return {"room": room}
        if op == "join":
            return {"member": s.join(_need(req, "room"), _need(req, "member"), req.get("kind", "human"))}
        if op == "leave":
            s.leave(_need(req, "room"), _need(req, "member"))
            return {}
        if op in ("mute", "unmute"):
            s.set_muted(_need(req, "room"), _need(req, "member"), op == "mute")
            return {}
        if op == "members":
            return {"members": list(s.members(_need(req, "room")).values())}
        if op == "post":
            msg = s.post(
                _need(req, "room"),
                _need(req, "author"),
                _need(req, "body"),
                kind=req.get("kind", "post"),
                from_kind=req.get("from", "human"),
                refs=req.get("refs"),
                no_reply_needed=bool(req.get("no_reply_needed", False)),
            )
            self._publish(msg["room"], {"event": "message", "message": msg})
            return {"message": msg}
        if op == "read":
            return s.read(
                _need(req, "room"),
                _need(req, "member"),
                since=req.get("since"),
                peek=bool(req.get("peek", False)),
                limit=req.get("limit"),
            )
        if op == "tail":
            return {"messages": s.tail(_need(req, "room"), int(req.get("n", 20)))}
        raise AcmError("bad_request", f"unknown op: {op}")

    async def _send(self, writer: asyncio.StreamWriter, obj: dict) -> None:
        writer.write(json.dumps(obj, separators=(",", ":")).encode() + b"\n")
        await writer.drain()

    async def _watch(self, req: dict, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        room = self.store.get_room(_need(req, "room"))["name"]
        q: asyncio.Queue = asyncio.Queue()
        self.watchers.setdefault(room, set()).add(q)
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
        finally:
            eof.cancel()
            self.watchers[room].discard(q)

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while line := await reader.readline():
                try:
                    req = json.loads(line)
                    if not isinstance(req, dict):
                        raise AcmError("bad_request", "request must be a JSON object")
                    if req.get("op") == "watch":
                        await self._watch(req, reader, writer)
                        return
                    resp = {"ok": True, **self.dispatch(req)}
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
    sock.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(sock.parent, 0o700)
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
    async with server:
        await daemon.stop.wait()
    sock.unlink(missing_ok=True)


def main() -> int:
    store = Store(paths.db_path())
    try:
        asyncio.run(serve(store, paths.socket_path()))
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
