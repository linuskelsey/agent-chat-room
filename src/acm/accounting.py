"""Real token accounting: read what a woken session spent from its own transcript."""

import json
from pathlib import Path

from acm import identity


def find_transcript(session_id: str) -> Path | None:
    if not session_id:
        return None
    for path in (identity.claude_dir() / "projects").glob(f"*/{session_id}.jsonl"):
        return path
    return None


def size(path: Path | None) -> int:
    try:
        return path.stat().st_size if path else 0
    except OSError:
        return 0


def usage_since(path: Path | None, offset: int, end: int | None = None) -> dict:
    """Token usage of assistant turns written between byte `offset` and `end` (default: the end of the file).

    A streamed turn is written as several lines that repeat the same usage block, so lines are
    deduplicated by (message id, request id) keeping the largest output count. Read a line at a time.
    """
    totals = {"input": 0, "output": 0, "cache_creation": 0, "cache_read": 0}
    if path is None:
        return totals
    turns: dict[tuple, dict] = {}
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            while end is None or f.tell() < end:
                line = f.readline()
                if not line:
                    break
                if end is not None and f.tell() > end:
                    line = line[: len(line) - (f.tell() - end)]  # a line that runs past the end is cut there
                if b'"usage"' not in line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue  # a partial line
                message = entry.get("message") if isinstance(entry, dict) else None
                usage = message.get("usage") if isinstance(message, dict) else None
                if entry.get("type") != "assistant" or not isinstance(usage, dict):
                    continue
                key = (message.get("id"), entry.get("requestId"))
                old = turns.get(key)
                if old is None or usage.get("output_tokens", 0) >= old.get("output_tokens", 0):
                    turns[key] = usage
    except OSError:
        return totals
    for usage in turns.values():
        totals["input"] += int(usage.get("input_tokens") or 0)
        totals["output"] += int(usage.get("output_tokens") or 0)
        totals["cache_creation"] += int(usage.get("cache_creation_input_tokens") or 0)
        totals["cache_read"] += int(usage.get("cache_read_input_tokens") or 0)
    return totals
