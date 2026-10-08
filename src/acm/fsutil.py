"""File handling that does not trust the places it writes to."""

import os
import stat
import tempfile
from pathlib import Path

from acm.errors import AcmError


def ensure_private_dir(path: Path) -> Path:
    """Create `path` for acm's own use and make sure it really is ours: a real directory (not a symlink),
    owned by this user, with no access for anyone else. A predictable path in a shared directory such as
    /tmp could otherwise have been created, or pointed elsewhere, by another user first.
    """
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise AcmError("insecure_path", f"{path} is not a plain directory (is it a symlink?); refusing to use it")
    if info.st_uid != os.getuid():
        raise AcmError("insecure_path", f"{path} belongs to another user; refusing to use it")
    if info.st_mode & 0o077:
        os.chmod(path, 0o700)
    return path


def atomic_write(path: Path, text: str, mode: int = 0o600) -> None:
    """Write `text` to `path` in one step, through a private temporary file in the same directory.

    A symlink at `path` is followed (so a dotfile manager's link keeps working), an existing file keeps its
    permissions, and a new one gets `mode`.
    """
    real = Path(os.path.realpath(path))
    real.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = stat.S_IMODE(os.stat(real).st_mode)
    except FileNotFoundError:
        pass
    fd, tmp = tempfile.mkstemp(dir=real.parent, prefix=f".{real.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            os.fchmod(f.fileno(), mode)
            f.write(text)
        os.replace(tmp, real)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def unclaimed_path(path: Path, marker: str) -> Path:
    """`path` if nothing is there or it is a file acm wrote earlier (its first line is `marker`); otherwise the
    first of path-1, path-2, ... that is free. acm never overwrites a file it did not write.
    """
    def ours(p: Path) -> bool:
        try:
            with open(p, "rb") as f:
                return f.readline().decode(errors="replace").strip() == marker
        except OSError:
            return False

    if not os.path.lexists(path) or ours(path):
        return path
    for n in range(1, 1000):
        candidate = path.with_name(f"{path.stem}-{n}{path.suffix}")
        if not os.path.lexists(candidate) or ours(candidate):
            return candidate
    raise AcmError("exists", f"no free file name next to {path}")
