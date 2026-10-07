"""Who is this agent? Resolved from the Claude Code session that spawned this process."""

import json
import os
import re
from pathlib import Path


def claude_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def _parent(pid: int) -> int:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        return int(stat.rsplit(")", 1)[1].split()[1])
    except (OSError, ValueError, IndexError):
        return 0


def find_session(start_pid: int | None = None, max_depth: int = 8) -> dict | None:
    """The session record of the nearest ancestor process that is a Claude Code session."""
    pid = start_pid or os.getpid()
    for _ in range(max_depth):
        pid = _parent(pid)
        if pid <= 1:
            return None
        try:
            info = json.loads((claude_dir() / "sessions" / f"{pid}.json").read_text())
        except (OSError, ValueError):
            continue
        if isinstance(info, dict) and info.get("pid") == pid:
            return info
    return None


def sanitize(name: str) -> str:
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-._")[:64]
    return name


def member_name(info: dict | None = None) -> str:
    """$ACM_NAME if set, else the session name, else agent-<pid>."""
    if env := sanitize(os.environ.get("ACM_NAME", "")):
        return env
    info = info if info is not None else find_session()
    if info:
        if name := sanitize(str(info.get("name") or "")):
            return name
        return f"agent-{info['pid']}"
    return f"agent-{os.getppid()}"
