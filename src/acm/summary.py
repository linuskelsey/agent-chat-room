"""A closed room's summary, assembled from what is in the log. No model call, so it costs nothing."""

import os
import re
import subprocess
import threading
import time
from pathlib import Path

from acm import textsafe
from acm.db import Store

COMMIT_RE = re.compile(r"^[0-9a-f]{7,40}$")
SNIPPET = 160
SUBJECT_BYTES = 400


def _snip(text: str, limit: int = SNIPPET) -> str:
    text = " ".join(textsafe.clean(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _git_subject(project_dir: str, sha: str) -> str | None:
    """The subject line of a commit in the linked project. Reads at most a few hundred bytes of git's output."""
    command = [
        # a linked repository is someone else's configuration: never let it prompt, lock or run a helper program
        "git", "-C", project_dir, "--no-optional-locks", "-c", "core.fsmonitor=false", "log", "-1", "--format=%s", sha, "--",
    ]
    try:
        proc = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except OSError:
        return None
    timer = threading.Timer(5.0, proc.kill)
    timer.start()
    try:
        first = proc.stdout.read(SUBJECT_BYTES)  # bounded: it never holds more than this, whatever git prints
    finally:
        timer.cancel()
        proc.kill()
        proc.wait()
        proc.stdout.close()
    subject = first.decode(errors="replace").split("\n", 1)[0].strip()
    return textsafe.one_line(subject) or None


def _ref_note(ref: str, project_dir: str | None) -> str:
    if not project_dir:
        return ""
    if COMMIT_RE.match(ref):
        subject = _git_subject(project_dir, ref)
        return f" {subject}" if subject else " (not found in the project repo)"
    if not ref.startswith("/"):  # relative to the linked project
        base = Path(project_dir).resolve()
        target = (base / ref).resolve()
        if base in target.parents or target == base:
            return "" if target.exists() else " (not found in the project)"
    return ""


def build(store: Store, room: str, closing_by: str | None = None) -> dict:
    info = store.get_room(room)
    messages = store.read(room, "acm-summary", since=0)["messages"]  # explicit since: no cursor is touched
    members = store.members(room)
    usage = store.usage(room)

    decisions = [m for m in messages if m["kind"] == "decision"]
    posts = [m for m in messages if m["kind"] in ("post", "decision", "human_approve")]

    open_items = []
    for m in posts:
        if m["no_reply_needed"]:
            continue
        for name in m["mentions"]:
            if name in ("all", "human") or name == m["author"] or name not in members:  # prose like "@mentions" is not a person
                continue
            if not any(x["id"] > m["id"] and x["author"] == name for x in posts):
                open_items.append({"who": name, "by": m["author"], "id": m["id"], "text": _snip(m["body"])})

    refs: dict[str, str] = {}
    for m in posts:
        for ref in m["refs"]:
            refs.setdefault(ref, m["author"])

    end = info["closed_at"] or time.time()
    return {
        "room": info["name"],
        "topic": info["topic"],
        "status": info["status"],
        "closed_by": closing_by or info["closed_by"],
        "started": info["created_at"],
        "ended": end,
        "project_dir": info["project_dir"],
        "members": [(n, m["kind"]) for n, m in members.items()],
        "messages": len(posts),
        "tokens": usage["weighted"],
        "decisions": [{"author": d["author"], "text": d["body"], "id": d["id"]} for d in decisions],
        "open_items": open_items,
        "refs": [(r, who) for r, who in refs.items()],
        "ending": [{"author": m["author"], "text": _snip(m["body"])} for m in posts[-3:]],
    }


def final_decision(data: dict) -> str | None:
    return data["decisions"][-1]["text"] if data["decisions"] else None


def render(data: dict, title: bool = False) -> str:
    out = []
    if title:
        out += [f"# Room: {data['room']}", ""]
    minutes = max(1, round((data["ended"] - data["started"]) / 60))
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(data["started"]))
    head = f"Topic: {textsafe.one_line(data['topic'])}\n" if data["topic"] else ""
    people = ", ".join(f"{n} ({k})" for n, k in data["members"])
    out += [
        f"{head}Started {when}, {minutes} min, {data['messages']} messages, about {data['tokens']:,} weighted tokens.",
        f"Members: {people}.",
    ]
    if data["project_dir"]:
        out.append(f"Project: {data['project_dir']}")
    out += ["", "Decisions:"]
    if data["decisions"]:
        out += [f"{i}. ({d['author']}) {d['text']}" for i, d in enumerate(data["decisions"], 1)]
    else:
        out.append("- none pinned (pin them with /decision, or room_pin_decision)")
    if data["open_items"]:
        out += ["", "Open items:"]
        out += [f"- @{o['who']} was asked by {o['by']} (#{o['id']}): {o['text']}" for o in data["open_items"]]
    if data["refs"]:
        out += ["", "Files and commits:"]
        for ref, who in data["refs"]:
            out.append(f"- {ref} ({who}){data.get('ref_notes', {}).get(ref, '')}")
    if data["ending"]:
        out += ["", "How it ended:"]
        out += [f"- {e['author']}: {e['text']}" for e in data["ending"]]
    return "\n".join(out)


MAX_REFS_CHECKED = 25  # the daemon waits on these, so how many refs it looks up (and for how long) is capped
REF_CHECK_SECONDS = 6.0


def with_ref_notes(data: dict) -> dict:
    """Add what the linked project can say about each ref (commit subjects, missing files)."""
    notes, deadline = {}, time.monotonic() + REF_CHECK_SECONDS
    for ref, _ in data["refs"][:MAX_REFS_CHECKED]:
        if time.monotonic() > deadline:
            break
        notes[ref] = _ref_note(ref, data["project_dir"])
    return {**data, "ref_notes": {k: v for k, v in notes.items() if v}}


def transcript(store: Store, room: str) -> str:
    out = ["## Transcript", ""]
    for m in store.read(room, "acm-summary", since=0)["messages"]:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(m["ts"]))
        if m["kind"] == "system":
            out.append(f"- {stamp} *{m['body'].splitlines()[0]}*")
            continue
        mark = {"decision": " **DECISION**", "human_approve": " **APPROVED**"}.get(m["kind"], "")
        who = m["author"] + (" (human)" if m["from"] == "human" else "")
        body = textsafe.clean(m["body"]).replace("\n", "\n  ")
        refs = f" [refs: {', '.join(m['refs'])}]" if m["refs"] else ""
        out.append(f"- {stamp} **{who}**{mark}: {body}{refs}")
    return "\n".join(out)


def wrapup_request(agent: str) -> str:
    """The message that asks one agent to leave a summary behind. It names a single agent, never everyone."""
    return (
        f"@{agent} please pin one decision (room_pin_decision) that summarises this discussion for whoever "
        "continues it: what was decided, what is still open, and which files or commits matter. Keep it short."
    )

