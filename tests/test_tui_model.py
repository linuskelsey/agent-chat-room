import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from acm import tui_model
from acm.errors import AcmError

SRC = os.path.join(os.path.dirname(__file__), "..", "src")


class ModelTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(dir="/tmp")
        root = cls.tmp.name
        cls.saved = dict(os.environ)
        os.makedirs(os.path.join(root, "claude", "sessions"))
        os.environ.update(
            ACM_RUNTIME=os.path.join(root, "run"), ACM_DATA=os.path.join(root, "data"), ACM_ALLOW_NO_TTY="1",
            CLAUDE_CONFIG_DIR=os.path.join(root, "claude"), PYTHONPATH=SRC, ACM_CONFIG=os.path.join(root, "none.toml"),
        )
        from acm import client
        cls.client = client
        subprocess.run([sys.executable, "-m", "acm", "daemon", "start"], check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        try:
            cls.client.request("shutdown", autostart=False)
        except Exception:
            pass
        os.environ.clear()
        os.environ.update(cls.saved)
        cls.tmp.cleanup()

    def room(self, name, by="kit", agents=()):
        self.client.request("create_room", name=name, by=by)
        for a in agents:
            self.client.request("join", room=name, member=a, kind="agent")
        return name

    def say(self, room, author, body, kind="agent", **extra):
        return self.client.request("post", room=room, author=author, body=body, **{"from": kind}, **extra)

    def model(self):
        m = tui_model.Model("kit")
        m.load_rooms()
        return m

    def test_01_list_orders_open_rooms_by_activity_then_closed_and_knows_membership(self):
        a, b, c, d = (self.room(n) for n in ("ta", "tb", "tc", "td"))
        self.say(a, "kit", "first", kind="human")
        time.sleep(0.02)
        self.say(b, "kit", "later", kind="human")
        self.client.request("close_room", name=c, by="kit")
        self.client.request("create_room", name="te", by="other")  # kit is not in this one
        m = self.model()
        names = [r.name for r in m.order() if r.name in {"ta", "tb", "tc", "td", "te"}]
        self.assertEqual(names.index("tb") < names.index("ta"), True)  # most recent first
        self.assertEqual([r.name for r in m.closed_rooms() if r.name in names], ["tc"])
        self.assertEqual(names[-1], "tc")  # closed rooms come last
        self.assertTrue(m.rooms["ta"].joined)
        self.assertFalse(m.rooms["te"].joined)

    def test_02_unread_counts_come_from_the_daemon_and_selecting_reads_the_room(self):
        r = self.room("tf", agents=("arx",))
        self.say(r, "arx", "one")
        self.say(r, "arx", "two")
        m = self.model()
        self.assertEqual(m.rooms[r].unread, 2)
        m.select(r)
        self.assertEqual((m.rooms[r].unread, [x["body"] for x in m.rooms[r].messages if x["kind"] == "post"]), (0, ["one", "two"]))
        again = self.model()
        self.assertEqual(again.rooms[r].unread, 0)  # the daemon moved my position, not just the screen

    def test_03_looking_at_a_room_does_not_join_it(self):
        self.client.request("create_room", name="tg", by="other")
        self.say("tg", "other", "hello", kind="human")
        m = self.model()
        m.select("tg")
        self.assertEqual([x["body"] for x in m.rooms["tg"].messages if x["kind"] == "post"], ["hello"])
        self.assertNotIn("kit", [x["name"] for x in self.client.request("members", room="tg")["members"]])
        self.assertFalse(m.rooms["tg"].joined)

    def test_04_events_update_unread_and_the_selected_room_stays_read(self):
        a, b = self.room("th", agents=("arx",)), self.room("ti", agents=("arx",))
        m = self.model()
        m.select(a)
        for room, body in ((a, "to a"), (b, "to b"), (b, "to b again")):
            self.say(room, "arx", body)
        # feed the events the daemon would stream
        w = self.client.watch()
        self.addCleanup(w.close)
        w._sock.settimeout(3)
        self.say(a, "arx", "live in a")
        self.say(b, "arx", "live in b")
        seen = 0
        while seen < 2:
            ev = next(w)
            if ev["event"] == "message" and ev["message"]["kind"] == "post":
                self.assertTrue(m.apply(ev))
                seen += 1
        self.assertEqual(m.rooms[a].unread, 0)
        self.assertEqual(m.rooms[b].unread, 1)
        self.assertEqual(m.rooms[a].messages[-1]["body"], "live in a")
        self.assertEqual(self.model().rooms[a].unread, 0)  # read on the daemon too
        # own messages, system lines and duplicates never count
        before = m.rooms[b].unread
        m.apply({"event": "message", "room_name": b, "message": {"id": 10**9, "ts": time.time(), "kind": "post", "author": "kit", "body": "mine"}})
        m.apply({"event": "message", "room_name": b, "message": {"id": 10**9 + 1, "ts": time.time(), "kind": "system", "author": "system", "body": "x joined"}})
        self.assertEqual(m.rooms[b].unread, before)

    def test_05_new_rooms_closed_rooms_and_warnings_arrive_as_events(self):
        m = self.model()
        self.client.request("create_room", name="tj", by="kit")
        info = self.client.request("get_room", name="tj")["room"]
        self.assertTrue(m.apply({"event": "room_created", "room_name": "tj", "room": info}))
        self.assertIn("tj", m.rooms)
        self.client.request("close_room", name="tj", by="kit")
        closed = self.client.request("get_room", name="tj")["room"]
        m.apply({"event": "closed", "room_name": "tj", "room": closed})
        self.assertFalse(m.rooms["tj"].open)
        self.assertIn("tj", [r.name for r in m.closed_rooms()])
        m.apply({"event": "warning", "room_name": "tj", "text": "max_messages reached"})
        self.assertEqual(m.notice, "tj: max_messages reached")
        self.assertFalse(m.apply({"event": "message", "room_name": "no-such-room", "message": {"id": 1, "ts": 0, "kind": "post", "author": "x", "body": "y"}}))

    def test_05b_an_agent_waiting_on_the_human_is_tracked_and_cleared(self):
        m = self.model()
        r = self.room("tw", agents=("arx",))
        m.load_rooms()
        up = {"event": "attention", "room_name": r, "agent": "arx", "waiting": True, "text": "arx is waiting for you in its own Claude Code window"}
        self.assertTrue(m.apply(up))
        self.assertEqual(m.rooms[r].waiting, {"arx"})
        self.assertIn("arx is waiting for you", m.notice)
        m.apply({**up, "waiting": False, "text": "arx is no longer waiting"})
        self.assertEqual(m.rooms[r].waiting, set())

    def test_05c_a_quiet_room_event_leaves_a_notice(self):
        m = self.model()
        r = self.room("tq2")
        m.load_rooms()
        self.assertTrue(m.apply({"event": "quiet", "room_name": r, "text": f"{r} is quiet"}))
        self.assertIn("it is your turn", m.notice)

    def test_06_a_message_in_an_unknown_room_loads_it(self):
        m = self.model()
        r = self.room("tk")
        self.assertNotIn(r, m.rooms)
        self.say(r, "kit", "hello", kind="human")
        ev = {"event": "message", "room_name": r, "message": {"id": 10**9, "ts": time.time(), "kind": "post", "author": "kit", "body": "hello"}}
        self.assertTrue(m.apply(ev))
        self.assertIn(r, m.rooms)

    def test_07_navigation_clamps_at_both_ends(self):
        for n in ("tl", "tm"):
            self.room(n)
        m = self.model()
        m.selected = None
        m.move(+1)
        first = m.selected
        self.assertEqual(first, m.order()[0].name)
        for _ in range(len(m.order()) + 3):
            m.move(+1)
        self.assertEqual(m.selected, m.order()[-1].name)
        for _ in range(len(m.order()) + 3):
            m.move(-1)
        self.assertEqual(m.selected, first)

    def test_08_posting_carries_the_flags_joins_the_room_and_reports_who_was_woken(self):
        self.client.request("create_room", name="tn", by="other")
        self.client.request("join", room="tn", member="arx", kind="agent")
        m = self.model()
        m.select("tn")
        self.assertFalse(m.rooms["tn"].joined)
        m.post("a plain message")
        self.assertTrue(m.rooms["tn"].joined)  # posting joins
        m.post("quiet @arx", fyi=True)
        self.assertEqual(m.notice, "nobody woken (fyi)")
        m.post("we will use sqlite", decision=True)
        msgs = {x["body"]: x for x in self.client.request("tail", room="tn", n=10)["messages"]}
        self.assertTrue(msgs["quiet @arx"]["no_reply_needed"])
        self.assertEqual(msgs["we will use sqlite"]["kind"], "decision")
        self.assertEqual(msgs["a plain message"]["from"], "human")

    def test_09_preview_and_the_confirmation_threshold(self):
        r = self.room("to", agents=("arx",))
        self.client.request("set_limits", room=r, updates={"confirm_wake_tokens": 1})
        m = self.model()
        m.select(r)
        pre = m.preview("@arx hello")
        self.assertEqual(pre["confirm_over"], 1)
        self.assertFalse(m.needs_confirm(pre))  # arx has no live session, so nothing would be woken (total 0)
        self.assertFalse(m.needs_confirm({"confirm_over": 1, "passive": "no_reply_needed", "total": 99}))
        self.assertTrue(m.needs_confirm({"confirm_over": 1, "passive": None, "total": 99}))
        self.assertFalse(m.needs_confirm({"confirm_over": 0, "passive": None, "total": 10**9}))  # 0 = never ask

    def test_10_older_messages_load_a_page_at_a_time(self):
        r = self.room("tp")
        for i in range(7):
            self.say(r, "kit", f"m{i}", kind="human")
        with mock.patch.object(tui_model, "PAGE", 3):
            m = self.model()
            m.select(r)
            self.assertEqual(len(m.rooms[r].messages), 3)
            self.assertFalse(m.rooms[r].exhausted)
            counts = []
            while m.load_older():
                counts.append(len(m.rooms[r].messages))
            self.assertEqual(counts, [6, 7])  # 3 -> 6 -> 7 (the room has 7 messages)
            self.assertTrue(m.rooms[r].exhausted)
            self.assertEqual([x["body"] for x in m.rooms[r].messages][0], "m0")

    def test_11_closed_rooms_are_read_only_and_closing_updates_the_state(self):
        r = self.room("tq")
        m = self.model()
        m.select(r)
        res = m.close_room()
        self.assertFalse(m.rooms[r].open)
        self.assertIn("closed tq", m.notice)
        self.assertIn("saved to", m.notice)
        for action in (lambda: m.post("late"), lambda: m.set_muted("x", True), lambda: m.invite(["x"]), m.close_room):
            with self.assertRaises(AcmError) as cm:
                action()
            self.assertEqual(cm.exception.code, "room_closed")

    def test_11b_deleting_removes_the_room_its_log_and_its_saved_summary(self):
        r = self.room("tdel", agents=("arx",))
        self.say(r, "kit", "hello", kind="human")
        m = self.model()
        m.select(r)
        res = m.close_room()
        saved = res["exported"]
        self.assertTrue(os.path.exists(saved))
        m.delete_room()
        self.assertNotIn(r, m.rooms)
        self.assertFalse(os.path.exists(saved))
        self.assertIn("deleted tdel", m.notice)
        with self.assertRaises(AcmError) as cm:
            self.client.request("get_room", name=r)
        self.assertEqual(cm.exception.code, "not_found")

    def test_11c_an_open_room_needs_force_and_a_foreign_file_is_left_alone(self):
        r = self.room("tdel2")
        with self.assertRaises(AcmError) as cm:
            self.client.request("delete_room", name=r)
        self.assertEqual(cm.exception.code, "room_open")
        mine = os.path.join(os.environ["ACM_DATA"], "rooms", "tdel2.md")
        os.makedirs(os.path.dirname(mine), exist_ok=True)
        with open(mine, "w") as f:
            f.write("somebody else's notes")
        res = self.client.request("delete_room", name=r, force=True)
        self.assertIsNone(res["export_removed"])
        self.assertTrue(os.path.exists(mine))

    def test_11d_acm_rm_deletes_from_the_command_line(self):
        r = self.room("tdel3")
        run = lambda *a: subprocess.run([sys.executable, "-m", "acm", "--as", "kit", *a], capture_output=True, text=True)
        refused = run("rm", r, "-y")
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("still open", refused.stderr)
        declined = subprocess.run([sys.executable, "-m", "acm", "--as", "kit", "rm", r, "--force"], input="n\n", capture_output=True, text=True)
        self.assertIn("not deleted", declined.stdout)
        done = run("rm", r, "-y", "--force")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("deleted tdel3", done.stdout)
        self.assertNotIn(r, run("ls", "--all").stdout)

    def test_12_mute_and_invite_report_back(self):
        r = self.room("tr", agents=("arx",))
        m = self.model()
        m.select(r)
        m.set_muted("arx", True)
        self.assertTrue(m.rooms[r].members["arx"]["muted"])
        m.set_muted("arx", False)
        self.assertFalse(m.rooms[r].members["arx"]["muted"])
        self.assertIn("not reached: ghost (no live session)", m.invite(["ghost"]))

    def test_13_the_model_follows_a_live_stream_end_to_end(self):
        r = self.room("ts", agents=("arx",))
        m = self.model()
        m.select(r)
        w = self.client.watch()
        self.addCleanup(w.close)
        lock = threading.Lock()

        def pump():
            try:
                for ev in w:
                    with lock:
                        m.apply(ev)
            except Exception:
                pass

        threading.Thread(target=pump, daemon=True).start()
        other = self.room("tt")
        self.say(r, "arx", "from the stream")
        self.say(other, "kit", "elsewhere", kind="human")
        end = time.time() + 3
        while time.time() < end and not (m.rooms[r].messages and m.rooms[r].messages[-1]["body"] == "from the stream" and "tt" in m.rooms):
            time.sleep(0.05)
        with lock:
            self.assertEqual(m.rooms[r].messages[-1]["body"], "from the stream")
            self.assertIn("tt", m.rooms)
            self.assertEqual(m.rooms[r].unread, 0)


if __name__ == "__main__":
    unittest.main()
