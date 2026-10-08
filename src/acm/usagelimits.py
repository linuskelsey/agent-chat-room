"""Account usage limits (5-hour session, 7-day week) and a room's estimated share of them.

The figures come from Claude Code's documented status-line input (`rate_limits.five_hour` and
`rate_limits.seven_day`, each with `used_percentage` and `resets_at`). `acm limits-tap`, chained
into a status-line command, writes them to a small file that the daemon reads.
"""

import json
import os
import time
from datetime import datetime
from pathlib import Path

from acm import accounting, config, fsutil, identity, paths

WINDOWS = {"five_hour": 5 * 3600, "seven_day": 7 * 86400}
SHARE_CACHE_SECS = 60


def limits_file() -> Path:
    return Path(os.environ.get("ACM_LIMITS_FILE") or paths.runtime_dir() / "limits.json")


def write_from_statusline(raw: str) -> bool:
    """Store the rate limits found in one status-line JSON input. Returns True when something was written."""
    try:
        data = json.loads(raw)
        limits = data["rate_limits"]
    except (ValueError, KeyError, TypeError):
        return False
    out = {"updated_at": time.time()}
    for key in WINDOWS:
        w = limits.get(key) if isinstance(limits, dict) else None
        if isinstance(w, dict) and isinstance(w.get("used_percentage"), (int, float)) and isinstance(w.get("resets_at"), (int, float)):
            out[key] = {"used_percentage": float(w["used_percentage"]), "resets_at": float(w["resets_at"])}
    if len(out) == 1:
        return False
    fsutil.atomic_write(limits_file(), json.dumps(out))
    return True


def _omarchy_cache_path() -> Path | None:
    """Omarchy's agent-usage widget keeps the account limits in a cache file. Empty setting turns this source off."""
    override = os.environ.get("ACM_OMARCHY_USAGE_CACHE")
    if override is not None:
        return Path(override) if override else None
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "omarchy" / "agent-usage" / "claude-limits.json"


def _from_status_line() -> dict:
    try:
        data = json.loads(limits_file().read_text())
    except (OSError, ValueError):
        return {}
    stamp = data.get("updated_at") or 0
    return {
        k: {**v, "as_of": stamp, "source": "status line"}
        for k, v in data.items() if k in WINDOWS and isinstance(v, dict)
    }


def _from_omarchy() -> dict:
    """Read the Omarchy widget's cache if it exists. Its percentages are fractions (0.35 means 35%)."""
    path = _omarchy_cache_path()
    if path is None:
        return {}
    try:
        data = json.loads(path.read_text())
        stamp = float(data["fetchedAtMs"]) / 1000
        entries = data["limits"]
    except (OSError, ValueError, KeyError, TypeError):
        return {}
    out = {}
    for e in entries if isinstance(entries, list) else []:
        label = str(e.get("label", "")) if isinstance(e, dict) else ""
        key = "five_hour" if "5-hour" in label else "seven_day" if "7-day" in label else None
        try:
            resets = datetime.fromisoformat(e["resetsAt"]).timestamp()
            used = float(e["percent"]) * 100
        except (KeyError, ValueError, TypeError):
            continue
        if key and key not in out:
            out[key] = {"used_percentage": used, "resets_at": resets, "as_of": stamp, "source": "Omarchy usage cache"}
    return out


def current() -> dict:
    """The usage-limit windows that are still open, each from whichever source reported it most recently.

    Sources: the status-line tap (`acm limits-tap`) and, when present, Omarchy's usage cache.
    """
    now = time.time()
    best: dict = {}
    for source in (_from_status_line(), _from_omarchy()):
        for key, w in source.items():
            if not isinstance(w.get("resets_at"), (int, float)) or w["resets_at"] <= now:
                continue
            if key not in best or w["as_of"] > best[key]["as_of"]:
                best[key] = w
    return best


def _until(epoch: float) -> str:
    return time.strftime("%H:%M", time.localtime(epoch))


def valve_reason(limits: dict) -> str | None:
    """Why the account is too close to its usage limit for agents to be woken, or None."""
    now = current()
    for key, cap_key, label in (("five_hour", "pause_session_pct", "5-hour"), ("seven_day", "pause_week_pct", "7-day")):
        cap, w = limits.get(cap_key), now.get(key)
        if cap is not None and w and w["used_percentage"] >= cap:
            return f"{label} usage limit is at {w['used_percentage']:.0f}% (pause at {cap:g}%), resumes at {_until(w['resets_at'])}"
    return None


_share_cache: dict = {}


def _turns_since(since: float) -> dict:
    """Every assistant turn of every Claude Code session after `since` (epoch), keyed by (message id, request id).

    Transcripts can be large, so each is read one line at a time. A streamed turn is written as several
    lines that repeat one usage block; the one with the most output tokens is kept.
    """
    turns: dict = {}
    cutoff = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(since))
    for path in (identity.claude_dir() / "projects").glob("*/*.jsonl"):
        try:
            if path.stat().st_mtime < since:
                continue
            handle = open(path, "rb")
        except OSError:
            continue
        with handle:
            for line in handle:
                if b'"usage"' not in line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                message = entry.get("message") if isinstance(entry, dict) else None
                usage = message.get("usage") if isinstance(message, dict) else None
                if entry.get("type") != "assistant" or not isinstance(usage, dict) or str(entry.get("timestamp", "")) < cutoff:
                    continue
                key = (message.get("id"), entry.get("requestId"))
                if key not in turns or usage.get("output_tokens", 0) >= turns[key].get("output_tokens", 0):
                    turns[key] = usage
    return turns


def _all_sessions_weighted(since: float) -> float:
    """Weighted tokens of every Claude Code session's turns since `since` (epoch), from the transcripts."""
    return sum(
        config.weighted_tokens(
            int(u.get("input_tokens") or 0), int(u.get("output_tokens") or 0),
            int(u.get("cache_creation_input_tokens") or 0), int(u.get("cache_read_input_tokens") or 0),
        )
        for u in _turns_since(since).values()
    )


def room_share_pct(room_weighted_in_window: float) -> float | None:
    """Estimated percentage points of the 5-hour limit this room used.

    An estimate, not a measurement: the account's used percentage is split in proportion to
    weighted tokens (this room's agents over all sessions' turns in the window).
    """
    w = current().get("five_hour")
    if not w:
        return None
    start = w["resets_at"] - WINDOWS["five_hour"]
    cached = _share_cache.get("all")
    if cached is None or cached[0] != start or time.time() - cached[1] > SHARE_CACHE_SECS:
        cached = (start, time.time(), _all_sessions_weighted(start))
        _share_cache["all"] = cached
    total = max(cached[2], room_weighted_in_window)
    if total <= 0:
        return 0.0
    return w["used_percentage"] * room_weighted_in_window / total


def window_start() -> float | None:
    w = current().get("five_hour")
    return w["resets_at"] - WINDOWS["five_hour"] if w else None


def window_end() -> float | None:
    w = current().get("five_hour")
    return w["resets_at"] if w else None
