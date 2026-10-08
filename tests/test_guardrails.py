import glob
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest

from tests.test_wake import FakeSession

SRC = os.path.join(os.path.dirname(__file__), "..", "src")


def usage_line(msg_id, req_id, inp, out, cc=0, cr=0, ts=None):
    ts = ts or time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
    return json.dumps({
        "type": "assistant", "requestId": req_id, "timestamp": ts,
        "message": {"id": msg_id, "usage": {
            "input_tokens": inp, "output_tokens": out,
            "cache_creation_input_tokens": cc, "cache_read_input_tokens": cr}},
    })


class mock_env:
    """Set environment variables for a block of the test process (the daemon is unaffected)."""

    def __init__(self, **values):
        self.values, self.old = values, {}

    def __enter__(self):
        for k, v in self.values.items():
            self.old[k] = os.environ.get(k)
            os.environ[k] = v

    def __exit__(self, *exc):
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class GuardrailTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(dir="/tmp")
        root = cls.tmp.name
        os.makedirs(os.path.join(root, "bin"))
        cls.notes = os.path.join(root, "notes.txt")
        fake = os.path.join(root, "bin", "notify-send")
        with open(fake, "w") as f:
            f.write(f'#!/bin/sh\nprintf "%s\\n" "$*" >> {cls.notes}\n')
        os.chmod(fake, os.stat(fake).st_mode | stat.S_IEXEC)
        cls.saved = dict(os.environ)
        cls.limits_file = os.path.join(root, "limits.json")
        os.environ.update(
            ACM_RUNTIME=os.path.join(root, "run"), ACM_DATA=os.path.join(root, "data"),
            CLAUDE_CONFIG_DIR=os.path.join(root, "claude"), PYTHONPATH=SRC,
            ACM_CONFIG=os.path.join(root, "none.toml"), ACM_LIMITS_FILE=cls.limits_file,
            ACM_ALLOW_NO_TTY="1", ACM_WAKE_CONFIRM_SECS="60", ACM_TICK_SECS="1", ACM_ACCOUNT_POLL_SECS="0.2",
            PATH=os.path.join(root, "bin") + os.pathsep + os.environ["PATH"],
        )
        from acm import client
        cls.client = client
        subprocess.run([sys.executable, "-m", "acm", "daemon", "start"], check=True, capture_output=True)
        cls.sessions = {}
        for i, name in enumerate(["arx", "hub", "mig"]):
            sess = FakeSession(root, os.environ["CLAUDE_CONFIG_DIR"], 930000 + i)
            cls.sessions[name] = sess
            client.request("register", name=name, pid=sess.pid, inbox=sess.path)

    @classmethod
    def tearDownClass(cls):
        try:
            cls.client.request("shutdown", autostart=False)
        except Exception:
            pass
        for s in cls.sessions.values():
            s.close()
        os.environ.clear()
        os.environ.update(cls.saved)
        cls.tmp.cleanup()

    def setUp(self):
        for path in (self.limits_file, self.notes):
            if os.path.exists(path):
                os.remove(path)

    def room(self, name, agents=("arx", "hub"), **limits):
        self.client.request("create_room", name=name, by="kit")
        for a in agents:
            self.client.request("join", room=name, member=a, kind="agent")
        if limits:
            self.client.request("set_limits", room=name, updates=limits)
        return name

    def post(self, room, author, body, kind="agent", **extra):
        return self.client.request("post", room=room, author=author, body=body, **{"from": kind}, **extra)

    def err(self, fn, *a, **kw):
        try:
            fn(*a, **kw)
        except self.client.AcmError as e:
            return e.code
        return None

    def notified(self):
        if not os.path.exists(self.notes):
            return ""
        with open(self.notes) as f:
            return f.read()

    def count(self, name):
        return len(self.sessions[name].lines)

    def budget(self, room):
        return self.client.request("budget", room=room)

    def wait_closed(self, room, secs=4):
        end = time.time() + secs
        while time.time() < end:
            if self.client.request("get_room", name=room)["room"]["status"] == "closed":
                return True
            time.sleep(0.1)
        return False

    # -- no_reply_needed -------------------------------------------------

    def test_01_no_reply_needed_wakes_and_notifies_nobody(self):
        r = self.room("g1")
        self.client.request("join", room=r, member="boss", kind="human")
        before = self.count("hub")
        res = self.post(r, "arx", "fyi @hub @boss all done", no_reply_needed=True)
        self.assertEqual(res["wake"]["passive"], "no_reply_needed")
        self.assertEqual((res["wake"]["woke"], res["wake"]["notified"]), ([], []))
        time.sleep(0.3)
        self.assertEqual(self.count("hub"), before)
        self.assertEqual(self.notified(), "")
        msgs = self.client.request("read", room=r, member="hub", kind="agent")["messages"]
        self.assertTrue(any(m["body"] == "fyi @hub @boss all done" and m["no_reply_needed"] for m in msgs))

    # -- brevity ---------------------------------------------------------

    def test_02_overlong_agent_posts_are_accepted_flagged_and_counted(self):
        r = self.room("g2")
        res = self.post(r, "arx", "x" * 500)
        self.assertTrue(res["message"]["id"])
        self.assertIn("be shorter next time (strike 1)", res["notices"][0])
        self.post(r, "arx", "short one")
        strikes = {m["name"]: m["strikes"] for m in self.budget(r)["members"]}
        self.assertEqual(strikes["arx"], 1)
        human = self.post(r, "kit", "y" * 5000, kind="human")  # humans are never held to the target
        self.assertEqual(human["notices"], [])

    def test_03_hard_ceiling_rejects_runaway_posts(self):
        r = self.room("g3", ceiling_chars=100)
        self.assertEqual(self.err(self.post, r, "arx", "z" * 101), "too_long")
        self.post(r, "arx", "z" * 100)

    def test_04_style_and_target_are_per_room(self):
        r = self.room("g4", style="normal")
        self.assertEqual(self.post(r, "arx", "x" * 900)["notices"], [])
        r2 = self.room("g4b", target_chars=50)
        self.assertEqual(len(self.post(r2, "arx", "x" * 60)["notices"]), 1)

    # -- rate limit and cooldown -----------------------------------------

    def test_05_rate_limit_and_overlong_posts_count_double(self):
        r = self.room("g5", agent_rate_per_min=3, target_chars=20)
        self.post(r, "arx", "x" * 30)  # overlong: costs 2
        self.post(r, "arx", "ok")  # costs 1, total 3
        self.assertEqual(self.err(self.post, r, "arx", "ok"), "rate_limited")
        self.post(r, "hub", "other agents have their own allowance")
        self.post(r, "kit", "humans are not limited", kind="human")

    def test_06_cooldown_after_agent_only_turns_until_a_human_speaks(self):
        r = self.room("g6", cooldown_turns=2)
        for n in (1, 2):
            self.assertEqual(self.post(r, "arx", f"@hub round {n}")["wake"]["woke"], ["hub"])
            self.client.request("read", room=r, member="hub", kind="agent")
        third = self.post(r, "arx", "@hub round 3")
        self.assertEqual(third["wake"]["woke"], [])
        self.assertIn("cooldown", third["wake"]["suppressed"])
        self.assertTrue(any("nobody was woken" in n for n in third["notices"]))
        self.assertTrue(third["message"]["id"])  # still posted
        human = self.post(r, "kit", "@hub carry on", kind="human")
        self.assertEqual(human["wake"]["woke"], ["hub"])
        self.client.request("read", room=r, member="hub", kind="agent")
        self.assertEqual(self.post(r, "arx", "@hub again")["wake"]["woke"], ["hub"])

    # -- caps ------------------------------------------------------------

    def test_07_message_cap_warns_then_pauses_agent_wakes_until_raised(self):
        r = self.room("g7", agents=("arx", "hub"), max_messages=5, agent_rate_per_min=30)
        w = self.client.watch(r)
        self.addCleanup(w.close)
        w._sock.settimeout(4)
        for i in range(4):
            self.post(r, "arx", f"m{i}")
        texts = []
        while not any("max_messages" in t for t in texts):  # the 80% warning arrives after the 4th message
            ev = next(w)
            texts.append(ev["text"] if ev["event"] == "warning" else "")
        self.post(r, "arx", "m4")  # the 5th message reaches the cap
        self.assertEqual(self.client.request("get_room", name=r)["room"]["status"], "open")  # not closed
        self.assertEqual(self.budget(r)["exceeded"], ["max_messages"])
        end = time.time() + 3
        while time.time() < end and "acm: g7 is at its cap" not in self.notified():
            time.sleep(0.05)
        self.assertIn("acm: g7 is at its cap", self.notified())
        before = self.count("hub")
        res = self.post(r, "arx", "@hub more work")
        self.assertEqual(res["wake"]["woke"], [])
        self.assertIn("max_messages cap reached", res["wake"]["suppressed"])
        time.sleep(0.2)
        self.assertEqual(self.count("hub"), before)
        human = self.post(r, "kit", "@hub please summarise and wrap up", kind="human")  # humans can still wake
        self.assertEqual(human["wake"]["woke"], ["hub"])
        self.client.request("read", room=r, member="hub", kind="agent")
        self.client.request("set_limits", room=r, updates={"max_messages": 50})  # raising the cap resumes
        self.assertEqual(self.budget(r)["exceeded"], [])
        self.assertEqual(self.post(r, "arx", "@hub back to work")["wake"]["woke"], ["hub"])

    def test_08_time_cap_is_reported_for_idle_rooms(self):
        r = self.room("g8", agents=(), max_minutes=0.01)
        end = time.time() + 4
        while time.time() < end and "acm: g8 is at its cap" not in self.notified():
            time.sleep(0.1)
        self.assertIn("acm: g8 is at its cap", self.notified())
        self.assertEqual(self.client.request("get_room", name=r)["room"]["status"], "open")

    # -- token accounting ------------------------------------------------

    def transcript_session(self, name, pid, session_id):
        sess = FakeSession(self.tmp.name, os.environ["CLAUDE_CONFIG_DIR"], pid, name=name, session_id=session_id)
        self.addCleanup(sess.close)
        d = os.path.join(os.environ["CLAUDE_CONFIG_DIR"], "projects", "proj")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, f"{session_id}.jsonl")
        with open(path, "w") as f:
            old = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(time.time() - 6 * 3600))
            f.write(usage_line("old", "r0", 5000, 5000, ts=old) + "\n")  # before the wake: must not be counted
        self.client.request("register", name=name, pid=pid, inbox=sess.path)
        return sess, path

    def test_09_wake_turn_tokens_are_counted_from_the_transcript(self):
        sess, path = self.transcript_session("acct", 940001, "sess-acct")
        r = self.room("g9", agents=("acct",), max_tokens=1000)
        self.post(r, "kit", "@acct please look", kind="human")
        sess.wait_for(1)
        with open(path, "a") as f:  # one streamed turn written as three lines, then a second turn
            for out in (5, 12, 20):
                f.write(usage_line("m1", "q1", 10, out, 0, 1000) + "\n")
            f.write(usage_line("m2", "q2", 100, 200, 300, 0) + "\n")
        sess.write_record("idle", int(time.time() * 1000) + 500)
        end = time.time() + 5
        while time.time() < end and self.budget(r)["usage"]["wake"] == 0:
            time.sleep(0.1)
        usage = self.budget(r)["usage"]
        # turn 1: 10 + 20 + 1000/10; turn 2: 100 + 200 + 300. Duplicated lines and old lines are ignored.
        self.assertEqual(usage["wake"], 130 + 600)
        self.assertGreaterEqual(usage["by_agent"]["acct"], 730)
        self.assertEqual(self.client.request("get_room", name=r)["room"]["status"], "open")  # under the cap of 1000

    def test_10_token_cap_pauses_the_room(self):
        sess, path = self.transcript_session("acct2", 940002, "sess-acct2")
        self.client.request("join", room=self.room("g10", agents=("acct2", "hub"), max_tokens=500), member="hub", kind="agent")
        r = "g10"
        self.post(r, "kit", "@acct2 go", kind="human")
        sess.wait_for(1)
        with open(path, "a") as f:
            f.write(usage_line("m1", "q1", 400, 400) + "\n")
        sess.write_record("idle", int(time.time() * 1000) + 500)
        end = time.time() + 5
        while time.time() < end and "max_tokens" not in self.budget(r)["exceeded"]:
            time.sleep(0.1)
        self.assertEqual(self.budget(r)["exceeded"], ["max_tokens"])
        self.assertEqual(self.client.request("get_room", name=r)["room"]["status"], "open")
        res = self.post(r, "acct2", "@hub done")
        self.assertIn("max_tokens cap reached", res["wake"]["suppressed"])

    # -- snooze and instructions -------------------------------------------

    def test_10b_snoozed_agents_are_not_woken_and_can_be_woken_again(self):
        r = self.room("g10b", agents=("arx", "hub"))
        before = self.count("hub")
        self.client.request("snooze", name="hub", seconds=60)
        res = self.post(r, "kit", "@hub @arx hello", kind="human")
        self.assertEqual(res["wake"]["woke"], ["arx"])
        self.assertTrue(res["wake"]["unreachable"][0].startswith("hub (snoozed until"))
        time.sleep(0.2)
        self.assertEqual(self.count("hub"), before)
        members = {m["name"]: m for m in self.client.request("members", room=r)["members"]}
        self.assertTrue(members["hub"]["snoozed_until"])
        self.assertIsNone(members["arx"]["snoozed_until"])
        self.client.request("unsnooze", name="hub")
        self.assertEqual(self.post(r, "kit", "@hub now?", kind="human")["wake"]["woke"], ["hub"])

    def test_10c_snooze_cli(self):
        run = lambda *a: subprocess.run([sys.executable, "-m", "acm", "--as", "kit", *a], capture_output=True, text=True)
        out = run("snooze", "mig", "30m")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("will not be woken by rooms until", out.stdout)
        self.assertIn("mig", self.client.request("budget", room=self.room("g10c", agents=("mig",)))["snoozed"])
        self.assertNotEqual(run("snooze", "mig", "soon").returncode, 0)
        self.assertIn("woken again", run("snooze", "mig", "off").stdout)
        self.assertNotIn("mig", self.client.request("budget", room="g10c")["snoozed"])

    # -- cost preview ------------------------------------------------------

    def preview(self, room, body, **extra):
        return self.client.request("wake_preview", room=room, author="kit", body=body, **{"from": "human"}, **extra)

    def test_10d_preview_estimates_the_cost_without_posting(self):
        r = self.room("g10d", agents=("arx", "hub", "mig"))
        self.sessions["arx"].write_record("idle", int(time.time() * 1000))  # arx is warm, the others have been idle for ages
        pre = self.preview(r, "@arx @hub hello")
        by = {w["name"]: w for w in pre["wakes"]}
        self.assertEqual((by["arx"]["tokens"], by["arx"]["cold"]), (25000, False))
        self.assertEqual((by["hub"]["tokens"], by["hub"]["cold"]), (90000, True))
        self.assertEqual(pre["total"], 115000)
        self.assertEqual(pre["confirm_over"], 100000)
        self.assertEqual([m for m in self.client.request("read", room=r, member="viewer", since=0)["messages"] if m["kind"] == "post"], [])
        self.assertEqual(self.preview(r, "@arx @hub fyi", no_reply_needed=True)["passive"], "no_reply_needed")
        self.client.request("snooze", name="mig", seconds=60)
        pre = self.preview(r, "@mig @ghost hi")
        self.assertEqual(pre["wakes"], [])
        self.assertTrue(any(x.startswith("mig (snoozed until") for x in pre["unreachable"]))
        self.assertIn("ghost (not in room)", pre["unreachable"])
        self.client.request("unsnooze", name="mig")
        self.sessions["arx"].write_record("idle", 0)

    def test_10e_preview_uses_what_waking_that_agent_really_cost(self):
        sess, path = self.transcript_session("hist", 940010, "sess-hist")
        r = self.room("g10e", agents=("hist",))
        self.post(r, "kit", "@hist go", kind="human")
        sess.wait_for(1)
        with open(path, "a") as f:
            f.write(usage_line("m1", "q1", 10, 20, 0, 1000) + "\n")  # 130
            f.write(usage_line("m2", "q2", 100, 200, 300, 0) + "\n")  # 600
        sess.write_record("idle", int(time.time() * 1000) + 500)
        end = time.time() + 5
        while time.time() < end and self.budget(r)["usage"]["wake"] == 0:
            time.sleep(0.1)
        self.client.request("read", room=r, member="hist", kind="agent")
        pre = self.preview(r, "@hist again")
        self.assertEqual(pre["wakes"][0]["tokens"], 730)
        self.assertEqual(pre["wakes"][0]["history"], 1)

    def test_10f_overlapping_wakes_in_two_rooms_split_the_transcript(self):
        sess, path = self.transcript_session("busy", 940011, "sess-busy")
        a = self.room("g10fa", agents=("busy",))
        b = self.room("g10fb", agents=("busy",))
        self.post(a, "kit", "@busy question in a", kind="human")
        sess.wait_for(1)
        with open(path, "a") as f:
            f.write(usage_line("m1", "q1", 100, 0) + "\n")  # spent while working on room a
        self.post(b, "kit", "@busy question in b", kind="human")  # woken for b while still busy
        sess.wait_for(2)
        with open(path, "a") as f:
            f.write(usage_line("m2", "q2", 400, 0) + "\n")  # spent after the second wake
        sess.write_record("idle", int(time.time() * 1000) + 500)
        end = time.time() + 6
        while time.time() < end and not (self.budget(a)["usage"]["wake"] and self.budget(b)["usage"]["wake"]):
            time.sleep(0.1)
        self.assertEqual(self.budget(a)["usage"]["wake"], 100)  # not 500: b's tokens are not counted twice
        self.assertEqual(self.budget(b)["usage"]["wake"], 400)

    def room_client(self, room, text, answer=None):
        return subprocess.run(
            [sys.executable, "-m", "acm", "--as", "kit", "room", room], capture_output=True, text=True,
            input=text + "\n" + (answer + "\n" if answer is not None else ""), timeout=30,
        )

    def test_10g_room_client_asks_before_an_expensive_post(self):
        r = self.room("g10g", agents=("arx", "hub"), confirm_wake_tokens=1000)
        before = self.count("arx")
        out = self.room_client(r, "@arx @hub expensive?", "n")
        self.assertIn("this wakes 2 agents, about 180,000 tokens (arx 90k cold, hub 90k cold). send? [y/N/f=as fyi]", out.stdout)
        self.assertIn("not sent", out.stdout)
        self.assertEqual([m["body"] for m in self.client.request("read", room=r, member="viewer", since=0)["messages"] if m["kind"] == "post"], [])
        out = self.room_client(r, "@arx @hub as an fyi", "f")
        posts = [m for m in self.client.request("read", room=r, member="viewer", since=0)["messages"] if m["kind"] == "post"]
        self.assertEqual([(m["body"], m["no_reply_needed"]) for m in posts], [("@arx @hub as an fyi", True)])
        time.sleep(0.3)
        self.assertEqual(self.count("arx"), before)  # nobody was woken
        self.room_client(r, "@arx now for real", "y")
        self.assertEqual(self.sessions["arx"].wait_for(before + 1)[-1].count("now for real"), 1)

    def test_10h_room_client_does_not_ask_below_the_threshold_or_for_fyi(self):
        r = self.room("g10h", agents=("arx",), confirm_wake_tokens=1000000)
        out = self.room_client(r, "@arx cheap enough")
        self.assertNotIn("send? [y/N", out.stdout)
        self.assertIn("cheap enough", [m["body"] for m in self.client.request("read", room=r, member="viewer", since=0)["messages"]][-1])
        r2 = self.room("g10h2", agents=("arx",), confirm_wake_tokens=1)
        out = self.room_client(r2, "/fyi @arx thanks")
        self.assertNotIn("send? [y/N", out.stdout)

    def test_10i_post_dry_run_shows_the_estimate_and_posts_nothing(self):
        r = self.room("g10i", agents=("arx",))
        out = subprocess.run([sys.executable, "-m", "acm", "--as", "kit", "post", r, "@arx hi", "--dry-run"], capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("would wake arx: about 90,000 tokens (cold: its cache has probably expired)", out.stdout)
        self.assertIn("total about 90,000 weighted tokens", out.stdout)
        self.assertEqual([m for m in self.client.request("read", room=r, member="viewer", since=0)["messages"] if m["kind"] == "post"], [])

    # -- account usage limits ---------------------------------------------

    def write_limits(self, five=None, seven=None):
        data = {"updated_at": time.time()}
        if five is not None:
            data["five_hour"] = {"used_percentage": five, "resets_at": time.time() + 3 * 3600}
        if seven is not None:
            data["seven_day"] = {"used_percentage": seven, "resets_at": time.time() + 3 * 86400}
        with open(self.limits_file, "w") as f:
            json.dump(data, f)

    def test_11_usage_limit_valve_pauses_posts_and_wakes_until_it_drops(self):
        r = self.room("g11", pause_session_pct=90)
        self.write_limits(five=95)
        self.assertEqual(self.err(self.post, r, "arx", "hello"), "paused")
        before = self.count("hub")
        human = self.post(r, "kit", "@hub are you there", kind="human")
        self.assertEqual(human["wake"]["woke"], [])
        self.assertIn("5-hour usage limit is at 95%", human["wake"]["suppressed"])
        self.assertIn("PAUSED", subprocess.run([sys.executable, "-m", "acm", "budget", r], capture_output=True, text=True).stdout)
        self.write_limits(five=50)
        self.assertTrue(self.post(r, "arx", "back to work")["message"]["id"])
        self.write_limits(seven=99)  # the weekly valve is separate and off for this room
        self.assertTrue(self.post(r, "arx", "still fine")["message"]["id"])
        self.client.request("set_limits", room=r, updates={"pause_week_pct": 95})
        self.assertEqual(self.err(self.post, r, "arx", "now paused"), "paused")
        time.sleep(0.2)
        self.assertEqual(self.count("hub"), before)

    def test_12_limits_tap_keeps_status_line_rate_limits(self):
        now = int(time.time())
        raw = json.dumps({"model": {"id": "x"}, "rate_limits": {
            "five_hour": {"used_percentage": 23.5, "resets_at": now + 3600},
            "seven_day": {"used_percentage": 41.2, "resets_at": now + 86400}}})
        tap = subprocess.run([sys.executable, "-m", "acm", "limits-tap"], input=raw, capture_output=True, text=True)
        self.assertEqual((tap.returncode, tap.stdout, tap.stderr), (0, "", ""))
        shown = subprocess.run([sys.executable, "-m", "acm", "limits"], capture_output=True, text=True).stdout
        self.assertIn("5-hour: 24% used", shown)
        self.assertIn("7-day: 41% used", shown)
        junk = subprocess.run([sys.executable, "-m", "acm", "limits-tap"], input="not json", capture_output=True, text=True)
        self.assertEqual((junk.returncode, junk.stdout, junk.stderr), (0, "", ""))  # never breaks the status line
        self.assertIn("5-hour: 24% used", subprocess.run([sys.executable, "-m", "acm", "limits"], capture_output=True, text=True).stdout)

    def omarchy_cache(self, session_pct, week_pct, fetched_ago=0, resets_in=3 * 3600):
        reset = lambda secs: time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() + secs))
        path = os.path.join(self.tmp.name, "omarchy-limits.json")
        with open(path, "w") as f:
            json.dump({"fetchedAtMs": int((time.time() - fetched_ago) * 1000), "limits": [
                {"label": "Session (5-hour)", "percent": session_pct, "resetsAt": reset(resets_in)},
                {"label": "Weekly (7-day)", "percent": week_pct, "resetsAt": reset(2 * 86400)},
                {"label": "Opus (7-day)", "percent": 0.9, "resetsAt": reset(86400)},  # a model-scoped limit: ignored
            ]}, f)
        return path

    def test_12b_the_omarchy_usage_cache_is_read_when_present_and_the_newest_source_wins(self):
        from acm import usagelimits
        cache = self.omarchy_cache(0.35, 0.5)
        with mock_env(ACM_OMARCHY_USAGE_CACHE=cache):
            cur = usagelimits.current()
            self.assertEqual(cur["five_hour"]["used_percentage"], 35.0)  # a fraction becomes a percentage
            self.assertEqual(cur["seven_day"]["used_percentage"], 50.0)
            self.assertEqual(cur["five_hour"]["source"], "Omarchy usage cache")
            self.assertEqual(len(cur), 2)
            self.write_limits(five=80)  # the status-line tap wrote more recently
            self.assertEqual(usagelimits.current()["five_hour"]["used_percentage"], 80)
            self.assertEqual(usagelimits.current()["five_hour"]["source"], "status line")
            self.assertEqual(usagelimits.current()["seven_day"]["used_percentage"], 50.0)  # tap has no week: cache fills in
            old = self.omarchy_cache(0.1, 0.1, fetched_ago=3600)  # an older cache loses to the tap
            with mock_env(ACM_OMARCHY_USAGE_CACHE=old):
                self.assertEqual(usagelimits.current()["five_hour"]["used_percentage"], 80)
            expired = self.omarchy_cache(0.9, 0.9, resets_in=-60)  # a window that has already reset is dropped
            with mock_env(ACM_OMARCHY_USAGE_CACHE=expired):
                os.remove(self.limits_file)
                self.assertNotIn("five_hour", usagelimits.current())  # its window is over; the week's is still open
                self.assertIn("seven_day", usagelimits.current())
        with mock_env(ACM_OMARCHY_USAGE_CACHE="/no/such/file.json"):
            os.path.exists(self.limits_file) and os.remove(self.limits_file)
            self.assertEqual(usagelimits.current(), {})

    def test_12c_acm_limits_names_the_source_and_age(self):
        cache = self.omarchy_cache(0.04, 0.35, fetched_ago=600)
        env = {**os.environ, "ACM_OMARCHY_USAGE_CACHE": cache}
        out = subprocess.run([sys.executable, "-m", "acm", "limits"], env=env, capture_output=True, text=True).stdout
        self.assertIn("5-hour: 4% used", out)
        self.assertIn("7-day: 35% used", out)
        self.assertIn("(Omarchy usage cache, 10 min ago)", out)

    def test_13_room_share_estimate_pauses_a_heavy_room(self):
        for stale in glob.glob(os.path.join(os.environ["CLAUDE_CONFIG_DIR"], "projects", "*", "*.jsonl")):
            os.remove(stale)  # earlier tests' transcripts would dilute the share
        sess, path = self.transcript_session("share", 940003, "sess-share")
        other = os.path.join(os.environ["CLAUDE_CONFIG_DIR"], "projects", "elsewhere", "other.jsonl")
        os.makedirs(os.path.dirname(other), exist_ok=True)
        with open(other, "w") as f:
            f.write(usage_line("o1", "p1", 900, 0) + "\n")  # someone else's 900 weighted tokens in the window
        r = self.room("g13", agents=("share",))
        self.write_limits(five=40)
        self.post(r, "kit", "@share go", kind="human")
        sess.wait_for(1)
        with open(path, "a") as f:
            f.write(usage_line("m1", "q1", 100, 0) + "\n")  # this room: 100 of the 1000 weighted tokens
        sess.write_record("idle", int(time.time() * 1000) + 500)
        end = time.time() + 5
        while time.time() < end and self.budget(r)["usage"]["wake"] == 0:
            time.sleep(0.1)
        self.assertIsNone(self.budget(r)["paused"])  # no cap set yet
        self.client.request("set_limits", room=r, updates={"room_share_pct": 3})
        paused = self.budget(r)["paused"]
        self.assertIn("estimated 4.0%", paused)  # 40% x 100/1000
        self.assertEqual(self.err(self.post, r, "share", "more"), "paused")
        self.client.request("set_limits", room=r, updates={"room_share_pct": 5})
        self.assertIsNone(self.budget(r)["paused"])

    # -- cli ---------------------------------------------------------------

    def test_14_budget_cli_sets_and_shows_limits(self):
        run = lambda *a: subprocess.run([sys.executable, "-m", "acm", "--as", "kit", *a], capture_output=True, text=True)
        self.assertEqual(run("new", "g14", "-L", "max_messages=7", "-L", "style=normal").returncode, 0)
        out = run("budget", "g14", "agent_rate_per_min=4", "pause_week_pct=80").stdout
        self.assertIn("/ 7", out)
        self.assertIn("style normal", out)
        self.assertIn("rate 4/min", out)
        self.assertIn("pause_week_pct 80", out)
        out = run("budget", "g14", "max_messages=default", "pause_week_pct=none").stdout
        self.assertIn("/ 200", out)
        self.assertNotIn("pause_week_pct", out)
        bad = run("budget", "g14", "nonsense=1")
        self.assertNotEqual(bad.returncode, 0)
        self.assertIn("unknown setting", bad.stderr)
        self.assertNotEqual(run("budget", "g14", "style=loud").returncode, 0)
        self.assertNotEqual(run("budget", "g14", "max_messages=-5").returncode, 0)


if __name__ == "__main__":
    unittest.main()
