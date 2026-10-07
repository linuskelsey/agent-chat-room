"""The `acm` command. Every subcommand is non-interactive except `room`."""

import argparse
import getpass
import json
import os
import sys
import time

from acm import client, fmt, paths
from acm.errors import AcmError


def default_name() -> str:
    return os.environ.get("ACM_NAME") or os.environ.get("USER") or getpass.getuser()


def _body(args) -> str:
    if args.text and args.text != ["-"]:
        return " ".join(args.text)
    if sys.stdin.isatty():
        raise AcmError("bad_request", "no message given (pass text, or pipe it on stdin)")
    return sys.stdin.read()


def _dump(obj) -> None:
    print(json.dumps(obj, indent=2))


def cmd_new(args) -> None:
    r = client.request("create_room", name=args.room, by=args.name, topic=args.topic)["room"]
    print(f"created room {r['name']}" + (f" - {r['topic']}" if r["topic"] else ""))


def cmd_ls(args) -> None:
    rooms = client.request("list_rooms", status=None if args.all else "open", member=args.name)["rooms"]
    if args.json:
        return _dump(rooms)
    if not rooms:
        print("no rooms" if args.all else "no open rooms (use --all to include closed)")
        return
    width = max(len(r["name"]) for r in rooms)
    for r in rooms:
        unread = f"  {r['unread']} unread" if r["unread"] else ""
        state = "" if r["status"] == "open" else "  [closed]"
        print(
            f"{r['name']:<{width}}  {r['members']} members  {r['messages']} msgs  "
            f"{fmt.ago(r['last_ts'])}{unread}{state}"
        )


def cmd_post(args) -> None:
    kind = "decision" if args.decision else "post"
    msg = client.request(
        "post",
        room=args.room,
        author=args.name,
        body=_body(args),
        kind=kind,
        refs=args.ref,
        no_reply_needed=args.no_reply,
        **{"from": "human"},
    )
    print(f"posted #{msg['message']['id']}" + _wake_note(msg["wake"]))


def _wake_note(wake: dict) -> str:
    parts = []
    if wake["woke"]:
        parts.append("woke " + ", ".join(wake["woke"]))
    if wake["already_pending"]:
        parts.append("already notified: " + ", ".join(wake["already_pending"]))
    if wake["notified"]:
        parts.append("notified: " + ", ".join(wake["notified"]))
    if wake["unreachable"]:
        parts.append("not reached: " + ", ".join(wake["unreachable"]))
    return f" ({'; '.join(parts)})" if parts else ""


def _print_messages(res: dict, args) -> None:
    color = fmt.use_color()
    if args.json:
        return _dump(res)
    pinned = [d for d in res["decisions"] if d["id"] not in {m["id"] for m in res["messages"]}]
    if pinned:
        print("pinned decisions:")
        for d in pinned:
            print("  " + fmt.message(d, color))
        print()
    for m in res["messages"]:
        print(fmt.message(m, color))
    if not res["messages"]:
        print("no new messages")


def cmd_read(args) -> None:
    res = client.request(
        "read", room=args.room, member=args.name, since=args.since, peek=args.peek, limit=args.limit
    )
    _print_messages(res, args)


def cmd_tail(args) -> None:
    color = fmt.use_color()
    events = client.watch(args.room) if args.follow else None  # subscribe first: no gap before the backlog
    msgs = client.request("tail", room=args.room, n=args.n)["messages"]
    for m in msgs:
        print(fmt.message(m, color), flush=True)
    if events is None:
        return
    seen = msgs[-1]["id"] if msgs else 0
    try:
        for ev in events:
            if ev["event"] == "message" and ev["message"]["id"] > seen:
                print(fmt.message(ev["message"], color), flush=True)
            elif ev["event"] == "warning":
                print(f"! {ev['text']}", flush=True)
            elif ev["event"] == "closed":
                print(f"room {args.room} closed")
                return
    except KeyboardInterrupt:
        pass


def cmd_unread(args) -> None:
    """Unread counts for this session's rooms. Silent when there are none, so it suits a hook."""
    from acm import identity

    name = identity.member_name() if args.hook else args.name
    try:
        rooms = client.request("list_rooms", status="open", member=name, autostart=not args.hook)["rooms"]
    except AcmError:
        if args.hook:  # a hook must never start the daemon or fail the prompt
            return
        raise
    lines = [f"{r['name']}: {r['unread']} unread" for r in rooms if r["unread"]]
    if args.hook and lines:
        print("[acm] " + "; ".join(lines) + ". Use room_read(room=...) to catch up.")
    elif not args.hook:
        print("\n".join(lines) or "nothing unread")


def cmd_members(args) -> None:
    ms = client.request("members", room=args.room)["members"]
    if args.json:
        return _dump(ms)
    for m in ms:
        print(f"{m['name']}  {m['kind']}" + ("  [muted]" if m["muted"] else ""))


def cmd_mute(args) -> None:
    client.request("unmute" if args.unmute else "mute", room=args.room, member=args.member)
    print(f"{'unmuted' if args.unmute else 'muted'} {args.member} in {args.room}")


def cmd_close(args) -> None:
    client.request("close_room", name=args.room, by=args.name)
    print(f"closed {args.room}")


def cmd_kill(args) -> None:
    client.request("kill_room", name=args.room, by=args.name)
    print(f"killed {args.room}")


def cmd_daemon(args) -> None:
    if args.action == "start":
        try:
            client.request("ping", autostart=False)
            print("daemon already running")
        except AcmError:
            client.start_daemon()
            print("daemon started")
    elif args.action in ("stop", "restart"):
        try:
            client.request("shutdown", autostart=False)
        except AcmError:
            if args.action == "stop":
                print("daemon not running")
                return
        else:
            for _ in range(100):
                if not paths.socket_path().exists():
                    break
                time.sleep(0.05)
            print("daemon stopped")
        if args.action == "restart":
            client.start_daemon()
            print("daemon started")
    else:
        try:
            pid = client.request("ping", autostart=False)["pid"]
            print(f"running (pid {pid}) socket {paths.socket_path()} db {paths.db_path()}")
        except AcmError:
            print("not running")
            raise SystemExit(1) from None


def cmd_room(args) -> None:
    from acm.room import run_room

    if args.create:
        try:
            client.request("create_room", name=args.room, by=args.name, topic=args.topic)
            print(f"created room {args.room}")
        except AcmError as e:
            if e.code != "exists":
                raise
    run_room(args.room, args.name)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="acm", description="Group chat rooms for AI agents and humans.")
    p.add_argument("--as", dest="name", default=default_name(), metavar="NAME", help="your name (default: $ACM_NAME or $USER)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help, json_flag=False):
        sp = sub.add_parser(name, help=help)
        sp.set_defaults(fn=fn)
        # also accepted after the subcommand; SUPPRESS keeps the global value when it is not given here
        sp.add_argument("--as", dest="name", default=argparse.SUPPRESS, metavar="NAME", help="your name")
        if json_flag:
            sp.add_argument("--json", action="store_true", help="machine-readable output")
        return sp

    sp = add("new", cmd_new, "create a room")
    sp.add_argument("room")
    sp.add_argument("-t", "--topic", default="")

    sp = add("ls", cmd_ls, "list rooms", json_flag=True)
    sp.add_argument("-a", "--all", action="store_true", help="include closed rooms")

    sp = add("post", cmd_post, "post a message (text args, or stdin)")
    sp.add_argument("room")
    sp.add_argument("text", nargs="*")
    sp.add_argument("-d", "--decision", action="store_true", help="pin as a decision")
    sp.add_argument("--ref", action="append", default=[], help="file path or commit SHA (repeatable)")
    sp.add_argument("--no-reply", action="store_true", help="mark as needing no reply")

    sp = add("read", cmd_read, "read new messages (advances your cursor)", json_flag=True)
    sp.add_argument("room")
    sp.add_argument("--since", type=int, help="read after this message id (cursor untouched)")
    sp.add_argument("--peek", action="store_true", help="do not advance your cursor")
    sp.add_argument("--limit", type=int)

    sp = add("tail", cmd_tail, "show the last messages, optionally follow")
    sp.add_argument("room")
    sp.add_argument("-n", type=int, default=20)
    sp.add_argument("-f", "--follow", action="store_true")

    sp = add("unread", cmd_unread, "unread counts per room")
    sp.add_argument("--hook", action="store_true", help="for a Claude Code hook: use the session's agent name, print only when unread")

    sp = add("members", cmd_members, "list room members", json_flag=True)
    sp.add_argument("room")

    sp = add("mute", cmd_mute, "mute a member (their posts are rejected)")
    sp.add_argument("room")
    sp.add_argument("member")
    sp.add_argument("--unmute", action="store_true")

    sp = add("close", cmd_close, "close a room (creator only)")
    sp.add_argument("room")

    sp = add("kill", cmd_kill, "force-close a room, whoever created it")
    sp.add_argument("room")

    sp = add("daemon", cmd_daemon, "manage the background daemon")
    sp.add_argument("action", choices=["start", "stop", "restart", "status"])

    sp = add("room", cmd_room, "interactive room client")
    sp.add_argument("room")
    sp.add_argument("-c", "--create", action="store_true", help="create the room first if it does not exist")
    sp.add_argument("-t", "--topic", default="", help="topic for a room created with -c")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        args.fn(args)
    except AcmError as e:
        print(f"acm: {e.message}", file=sys.stderr)
        return 1
    except BrokenPipeError:
        return 0
    return 0
