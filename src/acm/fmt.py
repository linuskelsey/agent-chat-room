"""Plain-text rendering shared by the CLI and the interactive client."""

import hashlib
import os
import sys
import time

from acm import textsafe

_COLORS = (31, 32, 33, 34, 35, 36, 91, 92, 93, 94, 95, 96)


def use_color(stream=None) -> bool:
    stream = stream or sys.stdout
    return stream.isatty() and "NO_COLOR" not in os.environ


def _paint(text: str, code: int | str, color: bool) -> str:
    return f"\033[{code}m{text}\033[0m" if color else text


def clock(ts: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts))


def message(m: dict, color: bool = False) -> str:
    stamp = _paint(clock(m["ts"]), 2, color)
    if m["kind"] == "system":
        return f"{stamp} {_paint('* ' + m['body'], 2, color)}"
    who = m["author"] + (" (human)" if m["from"] == "human" and m["kind"] != "human_approve" else "")
    hue = _COLORS[int(hashlib.md5(m["author"].encode()).hexdigest(), 16) % len(_COLORS)]
    who = _paint(who, hue, color)
    mark = {"decision": "★ DECISION ", "human_approve": "✔ APPROVED "}.get(m["kind"], "")
    body = textsafe.clean(m["body"]).replace("\n", "\n         ")
    return f"{stamp} {who}: {_paint(mark, 1, color)}{body}"


def ago(ts: float | None) -> str:
    if ts is None:
        return "-"
    d = max(0, time.time() - ts)
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if d >= n:
            return f"{int(d // n)}{unit} ago"
    return "just now"
