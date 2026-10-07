"""The `acm` command. Every subcommand is non-interactive except `room`."""

import argparse
import getpass
import json
import os
import sys
import time

from acm import client, config, fmt, paths, usagelimits
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


def _names(values: list[str]) -> list[str]:
    return [n for v in values for n in v.split(",") if n]


def _invite(room: str, by: str, names: list[str]) -> None:
    if not names:
        return
    res = client.request("invite", room=room, by=by, names=names)
    if res["added"]:
        print("added and notified: " + ", ".join(res["added"]))
    if res["already"]:
        print("already in the room: " + ", ".join(res["already"]))
    if res["unreachable"]:
        print("not reached: " + ", ".join(res["unreachable"]), file=sys.stderr)


def _settings(pairs: list[str]) -> dict:
    out = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or not key:
            raise AcmError("bad_request", f"expected KEY=VALUE, got {pair!r}")
        out[key.strip()] = value.strip()
    return out


def cmd_new(args) -> None:
    r = client.request("create_room", name=args.room, by=args.name, topic=args.topic)["room"]
    print(f"created room {r['name']}" + (f" - {r['topic']}" if r["topic"] else ""))
    if args.limit:
        client.request("set_limits", room=args.room, updates=_settings(args.limit))
    _invite(r["name"], args.name, _names(args.add))


def cmd_budget(args) -> None:
    if args.settings:
        client.request("set_limits", room=args.room, updates=_settings(args.settings))
    b = client.request("budget", room=args.room)
    if args.json:
        return _dump(b)
    lim, used, usage = b["limits"], b["used"], b["usage"]

    def line(label, value, cap, extra=""):
        cap_text = f"/ {cap:g}" if cap else "/ no cap"
        print(f"  {label:<9} {value:>8.0f} {cap_text}{extra}")

    print(f"room {args.room}")
    line("messages", used["max_messages"], lim["max_messages"])
    line("minutes", used["max_minutes"], lim["max_minutes"])
    line("tokens", used["max_tokens"], lim["max_tokens"], f"  (agents {usage['wake']}, posts {usage['post']})")
    print(f"  style {lim['style']} (target {config.target_chars(lim)} chars), rate {lim['agent_rate_per_min']}/min, cooldown {lim['cooldown_turns']} turns")
    for key in ("pause_session_pct", "pause_week_pct", "room_share_pct"):
        if lim[key] is not None:
            print(f"  {key} {lim[key]:g}")
    if lim["cooldown_turns"]:
        print(f"  agent-to-agent wakes since the last human message: {b['agent_turns']} / {lim['cooldown_turns']}")
    if b["paused"]:
        print(f"  PAUSED: {b['paused']}")
    if usage["by_agent"]:
        print("  tokens by member: " + ", ".join(f"{k} {v}" for k, v in usage["by_agent"].items()))
    strikes = [f"{m['name']} {m['strikes']}" for m in b["members"] if m["strikes"]]
    if strikes:
        print("  overlong-post strikes: " + ", ".join(strikes))
    for cap in b["exceeded"]:
        print(f"  CAP REACHED: {cap} (agents no longer wake each other; a human message still wakes them)")
    for cap in b["warn"]:
        print(f"  nearing cap: {cap}")


_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def _duration(text: str) -> float:
    if len(text) < 2 or text[-1] not in _UNITS or not text[:-1].isdigit():
        raise AcmError("bad_request", f"bad duration {text!r}: use a number and s, m, h or d (30m, 2h), or 'off'")
    return int(text[:-1]) * _UNITS[text[-1]]


def cmd_snooze(args) -> None:
    if args.duration == "off":
        client.request("unsnooze", name=args.agent)
        print(f"{args.agent} will be woken again")
        return
    until = client.request("snooze", name=args.agent, seconds=_duration(args.duration))["until"]
    print(f"{args.agent} will not be woken by rooms until {time.strftime('%H:%M', time.localtime(until))}")


def cmd_limits(args) -> None:
    now = usagelimits.current()
    if not now:
        print(f"no usage-limit data in {usagelimits.limits_file()}; chain `acm limits-tap` into your status line")
        return
    for key, label in (("five_hour", "5-hour"), ("seven_day", "7-day")):
        if key in now:
            w = now[key]
            print(f"{label}: {w['used_percentage']:.0f}% used, resets {time.strftime('%a %H:%M', time.localtime(w['resets_at']))}")


def cmd_limits_tap(args) -> None:
    """Read a status-line JSON from stdin and keep its rate limits for the daemon. Silent, never fails."""
    try:
        usagelimits.write_from_statusline(sys.stdin.read())
    except Exception:
        pass


def cmd_invite(args) -> None:
    _invite(args.room, args.name, _names(args.agents))


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
    for notice in msg["notices"]:
        print(notice)


def _wake_note(wake: dict) -> str:
    parts = []
    if wake.get("passive"):
        parts.append("no reply needed, nobody woken")
    if wake.get("suppressed"):
        parts.append("nobody woken: " + wake["suppressed"])
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
        print(f"{m['name']}  {m['kind']}" + ("  [muted]" if m["muted"] else "") + (f"  strikes {m['strikes']}" if m["strikes"] else "") + ("  (invited, not joined yet)" if not m["joined"] else "")
              + (f"  (snoozed until {time.strftime('%H:%M', time.localtime(m['snoozed_until']))})" if m["snoozed_until"] else ""))


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
    def running() -> bool:
        try:
            client.request("ping", autostart=False)
            return True
        except AcmError as e:
            if e.code != "daemon_unavailable":
                raise
            return False

    if args.action == "start":
        if running():
            print("daemon already running")
        else:
            client.start_daemon()
            print("daemon started")
    elif args.action in ("stop", "restart"):
        if running():
            client.request("shutdown", autostart=False)
            for _ in range(100):
                if not paths.socket_path().exists():
                    break
                time.sleep(0.05)
            print("daemon stopped")
        elif args.action == "stop":
            print("daemon not running")
            return
        if args.action == "restart":
            client.start_daemon()
            print("daemon started")
    else:
        if not running():
            print("not running")
            raise SystemExit(1)
        pid = client.request("ping", autostart=False)["pid"]
        print(f"running (pid {pid}) socket {paths.socket_path()} db {paths.db_path()}")


def cmd_room(args) -> None:
    from acm.room import run_room

    if args.create:
        try:
            client.request("create_room", name=args.room, by=args.name, topic=args.topic)
            print(f"created room {args.room}")
            if args.limit:
                client.request("set_limits", room=args.room, updates=_settings(args.limit))
        except AcmError as e:
            if e.code != "exists":
                raise
    _invite(args.room, args.name, _names(args.add))
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
    sp.add_argument("--add", action="append", default=[], metavar="AGENT", help="add an agent by session name (repeatable or comma separated)")
    sp.add_argument("-L", "--limit", action="append", default=[], metavar="KEY=VALUE", help="set a room limit, e.g. max_messages=100 (repeatable)")

    sp = add("budget", cmd_budget, "show a room's limits and usage, or change limits with KEY=VALUE", json_flag=True)
    sp.add_argument("room")
    sp.add_argument("settings", nargs="*", metavar="KEY=VALUE", help="a number, 'none' to switch off, or 'default' to remove the override")

    sp = add("snooze", cmd_snooze, "hold room wakes for an agent in every room (messages still collect)")
    sp.add_argument("agent")
    sp.add_argument("duration", nargs="?", default="1h", help="30m, 2h, 1d, or off (default 1h)")

    sp = add("limits", cmd_limits, "show the account usage limits the daemon can see")
    sp = add("limits-tap", cmd_limits_tap, "status-line helper: read status-line JSON on stdin and keep its rate limits")

    sp = add("invite", cmd_invite, "add agents to an open room and tell them to join")
    sp.add_argument("room")
    sp.add_argument("agents", nargs="+", metavar="AGENT")

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
    sp.add_argument("-L", "--limit", action="append", default=[], metavar="KEY=VALUE", help="limits for a room created with -c, e.g. max_messages=100")
    sp.add_argument("--add", action="append", default=[], metavar="AGENT", help="add an agent by session name (repeatable or comma separated)")
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
