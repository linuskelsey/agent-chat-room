"""Limits and budgets: built-in defaults < ~/.config/acm/config.toml [defaults] < per-room overrides."""

import os
import tomllib
from pathlib import Path

from acm.errors import AcmError

STYLES = {"terse": 400, "normal": 1200}  # soft post length target in characters

# name -> (type, default, description). A default of None means "off".
SPEC = {
    "style": (str, "terse", "brevity preset sent to agents on join: " + ", ".join(STYLES)),
    "target_chars": (int, None, "soft post length target for agents; overrides the style's target"),
    "ceiling_chars": (int, 20000, "hard post length ceiling, only to stop runaway posts"),
    "max_messages": (int, 200, "stop agents waking each other after this many messages"),
    "max_minutes": (float, 240, "stop agents waking each other this many minutes after the room was created"),
    "max_tokens": (int, 1000000, "stop agents waking each other after its agents have used this many weighted tokens"),
    "agent_rate_per_min": (int, 6, "posts per minute allowed for one agent (an overlong post counts double)"),
    "cooldown_turns": (int, 2, "agent-to-agent wakes allowed in a row before a human message is needed"),
    "inline_human": (int, 1, "put a human's message text in the wake sent to a mentioned agent, saving it a read (0 = pointer only)"),
    "warn_fraction": (float, 0.8, "warn the human when a cap reaches this fraction"),
    "pause_session_pct": (float, None, "stop waking agents while the 5-hour usage limit is at or above this percent"),
    "pause_week_pct": (float, None, "stop waking agents while the 7-day usage limit is at or above this percent"),
    "room_share_pct": (float, None, "close the room when its estimated share of the 5-hour limit reaches this many percentage points"),
}


def config_path() -> Path:
    if v := os.environ.get("ACM_CONFIG"):
        return Path(v)
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "acm" / "config.toml"


def coerce(key: str, value):
    """Validate one setting. None (or the string 'none') unsets it."""
    if key not in SPEC:
        raise AcmError("bad_request", f"unknown setting: {key} (known: {', '.join(SPEC)})")
    kind = SPEC[key][0]
    if value is None or (isinstance(value, str) and value.lower() in ("none", "off")):
        return None
    try:
        if kind is str:
            value = str(value)
        elif kind is int:
            value = int(value)
        else:
            value = float(value)
    except (TypeError, ValueError):
        raise AcmError("bad_request", f"{key} must be a {kind.__name__}") from None
    if kind is not str and value < 0:
        raise AcmError("bad_request", f"{key} cannot be negative")
    if key == "style" and value not in STYLES:
        raise AcmError("bad_request", f"style must be one of: {', '.join(STYLES)}")
    return value


def load_global() -> dict:
    path = config_path()
    try:
        data = tomllib.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise AcmError("bad_config", f"cannot read {path}: {e}") from None
    return {k: coerce(k, v) for k, v in data.get("defaults", {}).items()}


def effective(room_overrides: dict) -> dict:
    out = {k: spec[1] for k, spec in SPEC.items()}
    out.update(load_global())
    out.update({k: v for k, v in room_overrides.items() if k in SPEC})
    return out


def target_chars(limits: dict) -> int:
    return limits["target_chars"] or STYLES[limits["style"]]


def weighted_tokens(input_tokens: int, output_tokens: int, cache_creation: int, cache_read: int) -> float:
    """One number for 'how much did this cost': cache reads are cheap, everything else counts in full."""
    return input_tokens + output_tokens + cache_creation + cache_read / 10
