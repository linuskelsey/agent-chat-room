"""Filesystem locations. Every path can be overridden by environment variable (used by tests)."""

import os
from pathlib import Path


def runtime_dir() -> Path:
    if v := os.environ.get("ACM_RUNTIME"):
        return Path(v)
    if v := os.environ.get("XDG_RUNTIME_DIR"):
        return Path(v) / "acm"
    return Path(f"/tmp/acm-{os.getuid()}")


def data_dir() -> Path:
    if v := os.environ.get("ACM_DATA"):
        return Path(v)
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "acm"


def socket_path() -> Path:
    return runtime_dir() / "acm.sock"


def db_path() -> Path:
    return data_dir() / "acm.db"


def log_path() -> Path:
    return data_dir() / "daemon.log"
