"""Hook acm's usage-limit tap into Claude Code's status line with one reversible command.

Claude Code only shows an account's 5-hour and 7-day usage to a status-line command, and allows one such
command. `install` puts `acm limits-tap` in front of whatever the user already has: both receive the same
input, and the original command's output is unchanged. The original setting is saved so `uninstall`
restores it exactly.
"""

import json
import shlex
import shutil
import sys
from pathlib import Path

from acm import config, identity
from acm.errors import AcmError

BACKUP_SUFFIX = ".acm-backup"


def settings_path() -> Path:
    return identity.claude_dir() / "settings.json"


def state_path() -> Path:
    return config.config_path().parent / "statusline-original.json"


def _acm_command() -> str:
    exe = shutil.which("acm")
    return shlex.quote(exe) if exe else f"{shlex.quote(sys.executable)} -m acm"


def _load() -> dict:
    path = settings_path()
    try:
        data = json.loads(path.read_text()) if path.exists() else {}
    except (OSError, ValueError) as e:
        raise AcmError("bad_settings", f"cannot read {path}: {e}") from None
    if not isinstance(data, dict):
        raise AcmError("bad_settings", f"{path} is not a JSON object")
    return data


def _save(data: dict) -> None:
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".acm-tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.replace(path)


def wrapped_command(original: str | None) -> str:
    """A status-line command that taps the input for acm, then behaves exactly like `original`."""
    acm = _acm_command()
    if not original:
        return f"{acm} limits-tap --show"
    script = f'in=$(cat); printf "%s" "$in" | {acm} limits-tap; printf "%s" "$in" | ( {original} )'
    return f"sh -c {shlex.quote(script)}"


def installed() -> bool:
    command = (_load().get("statusLine") or {}).get("command", "")
    return "limits-tap" in command


def status() -> str:
    if installed():
        return f"installed in {settings_path()}"
    return f"not installed ({settings_path()})"


def plan() -> tuple[dict | None, dict]:
    """The current statusLine setting and the one install would write."""
    data = _load()
    current = data.get("statusLine")
    if current is not None and (not isinstance(current, dict) or current.get("type", "command") != "command"):
        raise AcmError("unsupported", "your statusLine is not a command, so acm will not wrap it")
    if installed():
        raise AcmError("exists", "already installed (run `acm statusline uninstall` to undo)")
    original = (current or {}).get("command")
    new = {**(current or {"type": "command"}), "command": wrapped_command(original)}
    return current, new


def install() -> Path:
    current, new = plan()
    data = _load()
    path = settings_path()
    backup = path.with_name(path.name + BACKUP_SUFFIX)
    if path.exists() and not backup.exists():
        shutil.copy2(path, backup)
    state_path().parent.mkdir(parents=True, exist_ok=True)
    state_path().write_text(json.dumps({"original": current, "installed": new["command"]}, indent=2) + "\n")
    data["statusLine"] = new
    _save(data)
    return backup


def uninstall(force: bool = False) -> str:
    data = _load()
    try:
        saved = json.loads(state_path().read_text())
    except (OSError, ValueError):
        raise AcmError("not_found", "acm has no saved status line to restore (was it installed with this command?)") from None
    now = (data.get("statusLine") or {}).get("command")
    if now != saved["installed"] and not force:
        raise AcmError("changed", "your statusLine was changed after acm installed it; use --force to restore the saved one anyway")
    if saved["original"] is None:
        data.pop("statusLine", None)
    else:
        data["statusLine"] = saved["original"]
    _save(data)
    state_path().unlink(missing_ok=True)
    return "restored your original status line" if saved["original"] else "removed the status line acm added"
