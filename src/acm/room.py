"""Interactive room client: type to post, `/` for commands, `!` for a local shell command."""

import shutil
import subprocess
import sys
import threading

from acm import client, fmt, textsafe
from acm.errors import AcmError

try:
    import readline
except ImportError:  # pragma: no cover - not available on every platform
    readline = None

PROMPT = "> "
HELP = """\
  text            post to the room
  /add NAME...    add agents (by session name) and tell them to join
  /fyi TEXT       post without waking anyone (no reply needed), even with @mentions
  /wrapup AGENT   ask that one agent to pin a summary decision (never everyone)
  /decision TEXT  post and pin a decision
  /members        list members
  /mute NAME      mute a member (/unmute NAME to undo)
  /close          close the room (creator only)
  /help           this help
  /quit           leave the client (the room stays open)
  !COMMAND        run a shell command locally, nothing is posted"""


class Printer:
    """Print from the watcher thread without clobbering a half-typed input line."""

    def __init__(self):
        self.lock = threading.Lock()
        self.color = fmt.use_color()
        self.stopped = False

    def stop(self) -> None:
        """Wait for any print in flight, then make every later print a no-op."""
        with self.lock:
            self.stopped = True

    def out(self, text: str) -> None:
        with self.lock:
            if self.stopped:
                return
            buf = readline.get_line_buffer() if readline else ""
            sys.stdout.write(f"\r\033[K{text}\n{PROMPT}{buf}")
            sys.stdout.flush()


def _watch_thread(events, room: str, printer: Printer, seen: list, done: threading.Event) -> None:
    try:
        for ev in events:
            if ev["event"] == "message" and ev["message"]["id"] > seen[0]:
                printer.out(fmt.message(ev["message"], printer.color))
            elif ev["event"] in ("warning", "attention", "quiet"):
                printer.out(f"! {ev['text']}")
            elif ev["event"] == "closed":
                printer.out(f"* room {room} was closed, press enter to exit")
                done.set()
                return
    except (AcmError, OSError):
        printer.out("* lost connection to the daemon, press enter to exit")
        done.set()


def run_room(room: str, name: str) -> None:
    info = client.request("get_room", name=room)["room"]
    printer = Printer()
    if info["status"] != "open":  # read-only: show what was said, then leave
        print(f"room {room} is closed (read-only), last messages:")
        for m in client.request("tail", room=room, n=20)["messages"]:
            print(fmt.message(m, printer.color))
        return
    events = client.watch(room)  # subscribe before reading history so nothing falls in the gap
    history = client.request("tail", room=room, n=20)["messages"]
    client.request("read", room=room, member=name)  # joins and marks everything so far as read
    topic = f" - {textsafe.one_line(info['topic'])}" if info["topic"] else ""
    print(f"room {room}{topic}  (you are {name}, /help for commands)")
    for m in history:
        print(fmt.message(m, printer.color))
    seen = [history[-1]["id"] if history else 0]

    done = threading.Event()
    thread = threading.Thread(target=_watch_thread, args=(events, room, printer, seen, done), daemon=True)
    thread.start()
    try:
        _input_loop(room, name, printer, done)
    finally:  # stop the watcher before the interpreter exits, or it can die mid-print
        printer.stop()
        events.close()
        thread.join(timeout=2)


def _input_loop(room: str, name: str, printer: Printer, done: threading.Event) -> None:
    while not done.is_set():
        try:
            raw = input(PROMPT)
        except (EOFError, KeyboardInterrupt):
            print()
            return
        line = raw.strip()
        if done.is_set() or not line:
            continue
        fyi = False
        if not line.startswith(("!", "/")):
            try:
                answer = _confirm_cost(room, name, line)
            except AcmError as e:
                printer.out(f"! {e.message}")
                continue
            if answer == "cancel":
                print("not sent")
                continue
            fyi = answer == "fyi"
            if answer == "sent-as-is":
                _erase_typed(raw)  # the room echoes the message back with a timestamp, don't show it twice
        try:
            if line.startswith("!"):
                subprocess.run(line[1:], shell=True)
            elif line.startswith("/"):
                if _command(line, room, name):
                    return
            else:
                res = client.request(
                    "post", room=room, author=name, body=line, no_reply_needed=fyi, **{"from": "human"}
                )
                for who in res["wake"]["unreachable"]:
                    printer.out(f"! not reached: {who}")
        except AcmError as e:
            printer.out(f"! {e.message}")


def _confirm_cost(room: str, name: str, text: str) -> str:
    """Ask before sending a message that would wake agents costing a lot.

    Returns "sent-as-is" (no question was needed), "yes", "fyi" (send without waking anyone) or "cancel".
    """
    pre = client.request("wake_preview", room=room, author=name, body=text, **{"from": "human"})
    over = pre["confirm_over"]
    if not over or pre["passive"] or pre["total"] < over:
        return "sent-as-is"
    who = ", ".join(f"{w['name']} {w['tokens'] // 1000}k" + (" cold" if w["cold"] else "") for w in pre["wakes"])
    reply = input(f"this wakes {len(pre['wakes'])} agents, about {pre['total']:,} tokens ({who}). send? [y/N/f=as fyi] ")
    return {"y": "yes", "yes": "yes", "f": "fyi", "fyi": "fyi"}.get(reply.strip().lower(), "cancel")


def _erase_typed(raw: str) -> None:
    """Remove the line(s) just typed from the terminal. A no-op when output is not a terminal."""
    if not sys.stdout.isatty():
        return
    cols = shutil.get_terminal_size().columns
    rows = max(1, -(-(len(PROMPT) + len(raw)) // cols))
    sys.stdout.write(f"\033[{rows}A\r\033[J")
    sys.stdout.flush()


def _command(line: str, room: str, name: str) -> bool:
    """Run a slash command. Returns True when the client should exit."""
    cmd, _, rest = line[1:].partition(" ")
    rest = rest.strip()
    if cmd in ("quit", "q", "exit"):
        return True
    if cmd == "help":
        print(HELP)
    elif cmd == "members":
        for m in client.request("members", room=room)["members"]:
            print(f"  {m['name']}  {m['kind']}" + ("  [muted]" if m["muted"] else ""))
    elif cmd in ("mute", "unmute"):
        if not rest:
            raise AcmError("bad_request", f"usage: /{cmd} NAME")
        client.request(cmd, room=room, member=rest)
    elif cmd == "add":
        if not rest:
            raise AcmError("bad_request", "usage: /add NAME...")
        res = client.request("invite", room=room, by=name, names=rest.replace(",", " ").split())
        for label, key in (("added and notified", "added"), ("already in the room", "already"), ("not reached", "unreachable")):
            if res[key]:
                print(f"  {label}: {', '.join(res[key])}")
    elif cmd == "wrapup":
        if not rest or " " in rest.strip():
            raise AcmError("bad_request", "usage: /wrapup AGENT (exactly one agent)")
        from acm import summary

        text = summary.wrapup_request(rest.strip())
        if _confirm_cost(room, name, text) == "cancel":
            print("not sent")
        else:
            res = client.request("post", room=room, author=name, body=text, **{"from": "human"})
            for who in res["wake"]["unreachable"]:
                print(f"  not reached: {who}")
    elif cmd == "fyi":
        if not rest:
            raise AcmError("bad_request", "usage: /fyi TEXT")
        client.request("post", room=room, author=name, body=rest, no_reply_needed=True, **{"from": "human"})
    elif cmd == "decision":
        if not rest:
            raise AcmError("bad_request", "usage: /decision TEXT")
        client.request("post", room=room, author=name, body=rest, kind="decision", **{"from": "human"})
    elif cmd == "close":
        if input(f"close {room}? this cannot be undone [y/N] ").strip().lower() not in ("y", "yes"):
            print("not closed")
            return False
        res = client.request("close_room", name=room, by=name)
        print(res["summary"])
        if res["exported"]:
            print(f"\nsaved to {res['exported']}")
        return True
    else:
        raise AcmError("bad_request", f"unknown command: /{cmd} (try /help)")
    return False
