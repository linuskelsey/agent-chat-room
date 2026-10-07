"""Client for the acm daemon socket, including daemon auto-start."""

import json
import socket
import subprocess
import sys
import time
from collections.abc import Iterator

from acm import paths
from acm.errors import AcmError

START_TIMEOUT = 5.0


def _connect() -> socket.socket:
    s = socket.socket(socket.AF_UNIX)
    try:
        s.connect(str(paths.socket_path()))
    except OSError:
        s.close()
        raise
    return s


def start_daemon() -> None:
    """Spawn the daemon detached from this process and wait until it accepts connections."""
    paths.data_dir().mkdir(parents=True, exist_ok=True)
    with open(paths.log_path(), "ab") as log:
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


def watch(room: str) -> Iterator[dict]:
    """Subscribe to a room and return a generator of its events.

    The subscription is live as soon as this returns, so callers can fetch history afterwards
    without missing messages that arrive in between (dedupe by message id). Close the generator
    to disconnect.
    """
    s = connect()
    try:
        s.sendall(json.dumps({"op": "watch", "room": room}).encode() + b"\n")
        f = s.makefile("rb")
        _raise_if_error(json.loads(f.readline() or b'{"ok":false}'))
    except BaseException:
        s.close()
        raise

    def events() -> Iterator[dict]:
        try:
            while line := f.readline():
                yield json.loads(line)
        finally:
            f.close()
            s.close()

    return events()
