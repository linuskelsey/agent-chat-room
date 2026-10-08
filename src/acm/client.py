"""Client for the acm daemon socket, including daemon auto-start."""

import json
import os
import socket
import struct
import subprocess
import sys
import time

from acm import fsutil, paths
from acm.errors import AcmError

START_TIMEOUT = 5.0


class InsecureSocket(AcmError):
    """Something other than this user's daemon is listening on the socket path."""


def _connect() -> socket.socket:
    s = socket.socket(socket.AF_UNIX)
    try:
        s.connect(str(paths.socket_path()))
        _pid, uid, _gid = struct.unpack("3i", s.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
        if uid != os.getuid():  # not our daemon: send it nothing
            raise InsecureSocket("insecure_socket", f"the socket {paths.socket_path()} is owned by another user; refusing to use it")
    except BaseException:
        s.close()
        raise
    return s


def start_daemon() -> None:
    """Spawn the daemon detached from this process and wait until it accepts connections."""
    fsutil.ensure_private_dir(paths.runtime_dir())  # fail clearly now, not after the daemon has failed to start
    fsutil.ensure_private_dir(paths.data_dir())
    with os.fdopen(os.open(paths.log_path(), os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600), "ab") as log:
        subprocess.Popen(
            [sys.executable, "-m", "acm.daemon"],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
    deadline = time.monotonic() + START_TIMEOUT
    while time.monotonic() < deadline:
        try:
            _connect().close()
            return
        except OSError:
            time.sleep(0.05)
    raise AcmError("daemon_unavailable", f"daemon did not start, see {paths.log_path()}")


def connect(autostart: bool = True) -> socket.socket:
    try:
        return _connect()
    except OSError:
        if not autostart:
            raise AcmError("daemon_unavailable", "acm daemon is not running") from None
    start_daemon()
    try:
        return _connect()
    except OSError as e:
        raise AcmError("daemon_unavailable", f"cannot reach daemon: {e}") from None


def _raise_if_error(resp: dict) -> dict:
    if not resp.get("ok"):
        err = resp.get("error", {})
        if str(err.get("message", "")).startswith("unknown op:"):
            raise AcmError(
                "daemon_outdated", "the running daemon is older than this client, run: acm daemon restart"
            )
        raise AcmError(err.get("code", "internal"), err.get("message", "unknown error"))
    return resp


def request(op: str, autostart: bool = True, **args) -> dict:
    """Send one request on a fresh connection and return the result fields."""
    with connect(autostart) as s:
        s.sendall(json.dumps({"op": op, **args}).encode() + b"\n")
        line = s.makefile("rb").readline()
    if not line:
        raise AcmError("daemon_unavailable", "daemon closed the connection")
    return _raise_if_error(json.loads(line))


class Watch:
    """A live event stream for one room. Iterate it for events; `close()` ends it from any thread."""

    def __init__(self, sock: socket.socket, stream):
        self._sock = sock
        self._stream = stream
        self._reading = False

    def __iter__(self):
        return self

    def __next__(self) -> dict:
        self._reading = True
        try:
            line = self._stream.readline()
        except TimeoutError:
            raise  # only if the caller set a socket timeout; that is not the end of the stream
        except (OSError, ValueError):
            line = b""
        finally:
            self._reading = False
        if not line:
            self._finish()
            raise StopIteration
        return json.loads(line)

    def close(self) -> None:
        try:
            self._sock.shutdown(socket.SHUT_RDWR)  # unblocks a reader in another thread
        except OSError:
            pass
        if not self._reading:  # otherwise the reader releases the socket when it sees the end of the stream
            self._finish()

    def _finish(self) -> None:
        for closer in (self._stream.close, self._sock.close):
            try:
                closer()
            except OSError:
                pass


def watch(room: str | None = None) -> Watch:
    """Subscribe to one room's events, or to every room's when `room` is None. Closed rooms and new rooms
    appear in the all-rooms stream. The subscription is live as soon as this returns, so callers can fetch
    history afterwards without missing messages that arrive in between (dedupe by message id)."""
    s = connect()
    try:
        request = {"op": "watch", "room": room} if room else {"op": "watch_all"}
        s.sendall(json.dumps(request).encode() + b"\n")
        f = s.makefile("rb")
        _raise_if_error(json.loads(f.readline() or b'{"ok":false}'))
    except BaseException:
        s.close()
        raise
    return Watch(s, f)
