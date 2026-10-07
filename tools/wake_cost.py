#!/usr/bin/env python3
"""Measure what waking live agents costs, with and without the human's text in the wake.

Run it yourself in a normal terminal (it creates a room, adds the agents and posts as you, which
agents are not allowed to do). It uses one throwaway room and flips `inline_human` between posts,
so every post hits agents that are already in the room:

    python3 tools/wake_cost.py sys-3b hub-e9 --rounds 2

Each post wakes every named agent, so a run costs real tokens: roughly (1 + 2 * rounds) posts times the
number of agents, each around 10k to 90k weighted tokens depending on how big the agent's context is.
"""

import argparse
import json
import subprocess
import sys
import time


def acm(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(["acm", *args], capture_output=True, text=True)
    if check and r.returncode:
        sys.exit(f"acm {' '.join(args)} failed: {r.stderr.strip() or r.stdout.strip()}")
    return r


def spent(room: str) -> dict[str, int]:
    return json.loads(acm("budget", room, "--json").stdout)["usage"]["by_agent"]


def wait_for(room: str, agents: list[str], before: dict[str, int], timeout: float) -> dict[str, int] | None:
    """Wait until every agent's accounted tokens have gone up, then let any late accounting settle."""
    end = time.time() + timeout
    while time.time() < end:
        now = spent(room)
        if all(now.get(a, 0) > before.get(a, 0) for a in agents):
            time.sleep(6)
            return spent(room)
        time.sleep(2)
    return None


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("agents", nargs="+", help="session names of live agents with the acm tools")
    p.add_argument("--rounds", type=int, default=2, help="pointer/inline pairs after one warm-up post (default 2)")
    p.add_argument("--question", default="what is 7 times 8? answer in the room in one line")
    p.add_argument("--timeout", type=float, default=240, help="seconds to wait for each agent per post")
    p.add_argument("-y", "--yes", action="store_true", help="do not ask before spending tokens")
    args = p.parse_args()

    posts = 1 + 2 * args.rounds
    print(f"{posts} posts x {len(args.agents)} agent(s) = {posts * len(args.agents)} wakes, plus one invite wake each.")
    if not args.yes and input("This uses real tokens. Continue? [y/N] ").strip().lower() not in ("y", "yes"):
        return

    room = f"costtest-{int(time.time())}"
    acm("new", room, "-t", "wake cost experiment", "-L", "inline_human=1", "--add", ",".join(args.agents))
    try:
        print(f"room {room}: waiting for {', '.join(args.agents)} to join...")
        before = {}
        deadline = time.time() + args.timeout
        while time.time() < deadline:
            members = {m["name"]: m for m in json.loads(acm("members", room, "--json").stdout)}
            if all(members.get(a, {}).get("joined") for a in args.agents):
                break
            time.sleep(2)
        else:
            sys.exit("an agent never joined: is it running, named as given, and does it have the acm tools?")
        before = wait_for(room, args.agents, {}, args.timeout) or spent(room)

        mention = " ".join(f"@{a}" for a in args.agents)
        plan = [("warm-up", 1)] + [(f"{mode} {i}", flag) for i in range(1, args.rounds + 1) for mode, flag in (("pointer", 0), ("inline", 1))]
        results = []
        for label, flag in plan:
            acm("budget", room, f"inline_human={flag}")
            out = acm("post", room, f"{mention} {args.question}").stdout
            if "woke" not in out:
                sys.exit(f"nobody was woken: {out.strip()}")
            after = wait_for(room, args.agents, before, args.timeout)
            if after is None:
                sys.exit(f"{label}: an agent did not finish in time")
            delta = {a: after[a] - before.get(a, 0) for a in args.agents}
            results.append((label, flag, delta))
            before = after
            print(f"{label:<10} inline={flag}  " + "  ".join(f"{a} {d:>7,}" for a, d in delta.items()), flush=True)

        print()
        for a in args.agents:
            for name, flag in (("pointer only", 0), ("human text in the wake", 1)):
                xs = [d[a] for label, f, d in results if f == flag and label != "warm-up"]
                print(f"{a:<10} {name:<24} mean {sum(xs) / len(xs):>9,.0f} weighted tokens over {len(xs)} wakes")
    finally:
        acm("close", room, check=False)


if __name__ == "__main__":
    main()
