import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest

SRC = os.path.join(os.path.dirname(__file__), "..", "src")


class FakeSession:
    """Stands in for a Claude Code session: an inbox socket plus the record Claude Code writes for it."""

    def __init__(self, root, claude_dir, pid, status="idle", name=None, session_id=None, react_on_receipt=False):
        self.react_on_receipt = react_on_receipt
        self.pid = pid
        self.name = name
        self.session_id = session_id
        self.path = os.path.join(root, f"{pid}.sock")
        self.record = os.path.join(claude_dir, "sessions", f"{pid}.json")
        self.lines: list[dict] = []
        self.srv = socket.socket(socket.AF_UNIX)
        self.srv.bind(self.path)
        self.srv.listen(8)
        self.thread = threading.Thread(target=self._accept, daemon=True)
        self.thread.start()
        self.write_record(status, 0)

    def write_record(self, status, status_at_ms, socket_path=None):
        os.makedirs(os.path.dirname(self.record), exist_ok=True)
        with open(self.record, "w") as f:
            json.dump(
                {"pid": self.pid, "status": status, "statusUpdatedAt": status_at_ms,
                 "messagingSocketPath": socket_path or self.path, **({"name": self.name} if self.name else {}),
                 **({"sessionId": self.session_id} if self.session_id else {})},
                f,
            )

    def react(self):
        self.write_record("busy", int(time.time() * 1000) + 500)

    def _accept(self):
        while True:
            try:
                conn, _ = self.srv.accept()
            except OSError:
                return
            with conn:
                data = b""
                while chunk := conn.recv(4096):
                    data += chunk
            if self.react_on_receipt:
                self.write_record("busy", int(time.time() * 1000))  # a fast session: busy before delivery returns
            for line in data.splitlines():
                self.lines.append(json.loads(line))

    def texts(self):
        return [m["message"]["content"] for m in self.lines]

    def wait_for(self, n, timeout=3):
        end = time.time() + timeout
        while time.time() < end and len(self.lines) < n:
            time.sleep(0.02)
        return self.texts()

    def close(self):
        self.srv.close()


class WakeTest(unittest.TestCase):
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
        os.environ.update(
            ACM_RUNTIME=os.path.join(root, "run"), ACM_DATA=os.path.join(root, "data"),
            CLAUDE_CONFIG_DIR=os.path.join(root, "claude"), PYTHONPATH=SRC,
            ACM_ALLOW_NO_TTY="1", ACM_WAKE_CONFIRM_SECS="1", PATH=os.path.join(root, "bin") + os.pathsep + os.environ["PATH"],
        )
        from acm import client
        cls.client = client
        subprocess.run([sys.executable, "-m", "acm", "daemon", "start"], check=True, capture_output=True)
        cls.sessions = {}
        for i, name in enumerate(["arx", "hub", "mig", "sys"]):
            sess = FakeSession(root, os.environ["CLAUDE_CONFIG_DIR"], 910000 + i)
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

    def room(self, name, agents=("arx", "hub", "mig"), humans=("kit",)):
        self.client.request("create_room", name=name, by="kit")
        for a in agents:
            self.client.request("join", room=name, member=a, kind="agent")
        return name

    def post(self, room, author, body, kind="human"):
        return self.client.request("post", room=room, author=author, body=body, **{"from": kind})["wake"]

    def count(self, name):
        return len(self.sessions[name].lines)

    def test_01_only_the_mentioned_agent_is_woken(self):
        r = self.room("w1")
        before = {n: self.count(n) for n in self.sessions}
        wake = self.post(r, "kit", "hey @arx can you look?")
        self.assertEqual(wake["woke"], ["arx"])
        texts = self.sessions["arx"].wait_for(before["arx"] + 1)
        self.assertEqual(len(texts), before["arx"] + 1)
        self.assertIn('[acm room w1] kit (human) mentioned you: "hey @arx can you look?"', texts[-1])
        self.assertIn('room_post(room="w1")', texts[-1])  # a human's text rides along, so no read is needed
        time.sleep(0.2)
        self.assertEqual(self.count("hub"), before["hub"])
        self.assertEqual(self.count("mig"), before["mig"])

    def test_01b_wake_is_a_pointer_when_inline_is_off_or_the_author_is_an_agent(self):
        r = self.room("w1b")
        self.client.request("set_limits", room=r, updates={"inline_human": 0})
        n = self.count("arx")
        self.post(r, "kit", "private words @arx")
        text = self.sessions["arx"].wait_for(n + 1)[-1]
        self.assertIn('kit mentioned you (1 unread). Call room_read(room="w1b")', text)
        self.assertNotIn("private words", text)
        self.client.request("set_limits", room=r, updates={"inline_human": "default"})
        self.client.request("read", room=r, member="arx", kind="agent")
        n = self.count("hub")
        self.post(r, "arx", "agent secrets @hub", kind="agent")  # agent text is never copied into another agent's prompt
        text = self.sessions["hub"].wait_for(n + 1)[-1]
        self.assertIn('arx mentioned you (', text)
        self.assertIn('Call room_read(room="w1b")', text)
        self.assertNotIn("agent secrets", text)

    def test_01c_inline_wakes_trim_long_text_and_count_other_unread(self):
        r = self.room("w1c")
        n = self.count("mig")
        self.post(r, "arx", "an earlier note from an agent", kind="agent")
        self.post(r, "kit", "@mig " + "word " * 200)
        text = self.sessions["mig"].wait_for(n + 1)[-1]
        self.assertIn("... (cut short, room_read has the rest)", text)
        self.assertIn('1 other unread: room_read(room="w1c")', text)
        self.assertLess(len(text), 800)

    def test_01d_replying_without_reading_allows_the_next_wake(self):
        r = self.room("w1d")
        n = self.count("hub")
        self.assertEqual(self.post(r, "kit", "@hub one")["woke"], ["hub"])
        self.sessions["hub"].wait_for(n + 1)
        self.assertEqual(self.post(r, "kit", "@hub two")["already_pending"], ["hub"])  # it has not reacted yet
        self.post(r, "hub", "answering from the wake text alone", kind="agent")  # no room_read
        self.assertEqual(self.post(r, "kit", "@hub three")["woke"], ["hub"])

    def test_02_unaddressed_posts_are_passive(self):
        r = self.room("w2")
        before = {n: self.count(n) for n in self.sessions}
        wake = self.post(r, "kit", "just chatting, no mentions")
        self.assertEqual((wake["woke"], wake["already_pending"], wake["unreachable"], wake["notified"]), ([], [], [], []))
        self.assertIsNone(wake["suppressed"])
        time.sleep(0.2)
        self.assertEqual({n: self.count(n) for n in self.sessions}, before)

    def test_03_one_outstanding_wake_until_the_agent_reads(self):
        r = self.room("w3")
        n = self.count("hub")
        self.assertEqual(self.post(r, "kit", "@hub first")["woke"], ["hub"])
        self.sessions["hub"].wait_for(n + 1)
        again = self.post(r, "kit", "@hub second")
        self.assertEqual(again["woke"], [])
        self.assertEqual(again["already_pending"], ["hub"])
        time.sleep(0.2)
        self.assertEqual(self.count("hub"), n + 1)
        self.client.request("read", room=r, member="hub", kind="agent")  # hub catches up
        self.assertEqual(self.post(r, "kit", "@hub third")["woke"], ["hub"])
        self.sessions["hub"].wait_for(n + 2)
        self.assertEqual(self.count("hub"), n + 2)

    def test_04_at_all_wakes_every_agent_but_the_author(self):
        r = self.room("w4")
        wake = self.post(r, "arx", "@all heads up", kind="agent")
        self.assertEqual(sorted(wake["woke"]), ["hub", "mig"])
        text = self.sessions["hub"].wait_for(self.count("hub"))[-1]
        self.assertIn("arx addressed everyone", text)

    def test_05_unreachable_mentions_are_reported(self):
        r = self.room("w5", agents=("arx",))
        self.client.request("join", room=r, member="ghost", kind="agent")  # never registered
        self.client.request("join", room=r, member="mig", kind="agent")
        self.client.request("mute", room=r, member="mig")
        wake = self.post(r, "kit", "@ghost @mig @nobody hello")
        self.assertEqual(wake["woke"], [])
        self.assertEqual(
            sorted(wake["unreachable"]),
            ["ghost (no live session)", "mig (muted)", "nobody (not in room)"],
        )

    def test_06_a_registration_cannot_aim_the_daemon_at_another_socket(self):
        r = self.room("w6", agents=())
        victim = self.sessions["sys"]
        self.client.request("join", room=r, member="evil", kind="agent")
        # claims sys's socket, but the session record for this pid names a different socket
        liar = FakeSession(self.tmp.name, os.environ["CLAUDE_CONFIG_DIR"], 920000)
        liar.write_record("idle", 0, socket_path="/somewhere/else.sock")
        self.client.request("register", name="evil", pid=liar.pid, inbox=victim.path)
        before = self.count("sys")
        wake = self.post(r, "kit", "@evil hi")
        self.assertEqual(wake["woke"], [])
        time.sleep(0.2)
        self.assertEqual(self.count("sys"), before)
        liar.close()

    def test_07_humans_get_a_desktop_notification(self):
        r = self.room("w7", agents=("arx",))
        self.client.request("join", room=r, member="boss", kind="human")
        wake = self.post(r, "arx", "need a decision from @boss <b>now</b>", kind="agent")
        self.assertEqual(wake["notified"], ["boss"])
        end = time.time() + 3
        while time.time() < end and not os.path.exists(self.notes):
            time.sleep(0.05)
        with open(self.notes) as f:
            note = f.read()
        self.assertIn("acm: w7", note)
        self.assertIn("need a decision from @boss", note)
        self.assertNotIn("<b>", note)  # markup is escaped

    def test_07b_at_human_notifies_every_human_except_the_author(self):
        r = self.room("w7b", agents=("arx",))
        self.client.request("join", room=r, member="boss2", kind="human")
        wake = self.post(r, "arx", "@human please review", kind="agent")
        self.assertEqual(sorted(wake["notified"]), ["boss2", "kit"])
        self.assertEqual(self.post(r, "kit", "@human talking to myself")["notified"], ["boss2"])

    def test_08_an_unconfirmed_wake_raises_a_warning(self):
        r = self.room("w8", agents=("mig",))
        w = self.client.watch(r)
        self.addCleanup(w.close)
        w._sock.settimeout(4)
        self.post(r, "kit", "@mig ping")
        ev = next(w)
        self.assertEqual(ev["event"], "message")
        ev = next(w)
        self.assertEqual(ev["event"], "warning")
        self.assertIn("mig did not react", ev["text"])
        self.assertIn("crossSessionInbound", ev["text"])
        w.close()

    def test_08b_a_session_that_reacts_instantly_is_not_flagged(self):
        sess = FakeSession(self.tmp.name, os.environ["CLAUDE_CONFIG_DIR"], 950001, react_on_receipt=True)
        self.addCleanup(sess.close)
        self.client.request("register", name="fast", pid=sess.pid, inbox=sess.path)
        r = self.room("w8b", agents=("fast",))
        w = self.client.watch(r)
        self.addCleanup(w.close)
        w._sock.settimeout(2.5)
        self.post(r, "kit", "@fast ping")
        self.assertEqual(next(w)["event"], "message")
        with self.assertRaises(TimeoutError):  # no "did not react" warning follows
            next(w)

    def test_09_a_confirmed_wake_raises_no_warning(self):
        r = self.room("w9", agents=("sys",))
        w = self.client.watch(r)
        self.addCleanup(w.close)
        w._sock.settimeout(2.5)
        self.post(r, "kit", "@sys ping")
        self.assertEqual(next(w)["event"], "message")
        self.sessions["sys"].react()  # the session starts a turn
        with self.assertRaises(TimeoutError):
            next(w)
        w.close()

    def test_11_unread_hook_is_silent_and_starts_nothing_when_the_daemon_is_down(self):
        env = {**os.environ, "ACM_NAME": "hookbot", "ACM_RUNTIME": os.path.join(self.tmp.name, "nowhere")}
        r = subprocess.run([sys.executable, "-m", "acm", "unread", "--hook"], env=env, capture_output=True, text=True)
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "nowhere", "acm.sock")))

    def test_10_unread_hook_prints_only_when_there_is_something(self):
        r = self.room("w10", agents=("hookbot",))
        env = {**os.environ, "ACM_NAME": "hookbot"}
        run = lambda *a: subprocess.run([sys.executable, "-m", "acm", "unread", *a], env=env, capture_output=True, text=True)
        self.assertEqual(run("--hook").stdout, "")
        self.post(r, "kit", "one")
        self.post(r, "kit", "two")
        out = run("--hook").stdout
        self.assertIn("[acm]", out)
        self.assertIn("w10: 2 unread", out)
        self.client.request("read", room=r, member="hookbot", kind="agent")
        self.assertEqual(run("--hook").stdout, "")

    def live_session(self, name):
        """A fake session whose pid belongs to a real (sleeping) process, since invites check liveness."""
        proc = subprocess.Popen(["sleep", "60"])
        self.addCleanup(lambda: (proc.kill(), proc.wait()))
        sess = FakeSession(self.tmp.name, os.environ["CLAUDE_CONFIG_DIR"], proc.pid, name=name)
        self.addCleanup(sess.close)
        return sess

    def test_12_add_invites_agents_by_session_name(self):
        newbie = self.live_session("newbie")  # never called an acm tool, so it is not registered
        r = self.room("w12", agents=("arx",))
        res = self.client.request("invite", room=r, by="kit", names=["newbie", "ghost2", "arx"])
        self.assertEqual(res["added"], ["newbie"])
        self.assertEqual(res["already"], ["arx"])
        self.assertEqual(res["unreachable"], ["ghost2 (no live session)"])
        text = newbie.wait_for(1)[0]
        self.assertIn("[acm room w12] kit added you to this room", text)
        self.assertIn('room_join(room="w12")', text)
        members = {m["name"]: m["kind"] for m in self.client.request("members", room=r)["members"]}
        self.assertEqual(members["newbie"], "agent")
        log = self.client.request("read", room=r, member="kit", since=0)["messages"]
        self.assertEqual(log[-1]["body"], "kit added newbie")
        self.assertFalse({m["name"]: m["joined"] for m in self.client.request("members", room=r)["members"]}["newbie"])
        # now registered, so it can be woken by a mention like any other agent
        self.client.request("read", room=r, member="newbie", kind="agent")  # its first action: it has joined
        bodies = [m["body"] for m in self.client.request("read", room=r, member="kit", since=0)["messages"]]
        self.assertIn("newbie joined", bodies)
        self.assertEqual(self.post(r, "kit", "@newbie ready?")["woke"], ["newbie"])

    def test_12b_joins_appear_live_in_the_room_stream(self):
        r = self.room("w12b", agents=())
        w = self.client.watch(r)
        self.addCleanup(w.close)
        w._sock.settimeout(3)
        self.client.request("catch_up", room=r, member="latecomer", keep=5, kind="agent")
        ev = next(w)
        self.assertEqual(ev["event"], "message")
        self.assertEqual((ev["message"]["body"], ev["message"]["kind"]), ("latecomer joined", "system"))

    def test_13_ambiguous_names_and_closed_rooms_are_refused(self):
        self.live_session("twin")
        self.live_session("twin")
        r = self.room("w13", agents=())
        res = self.client.request("invite", room=r, by="kit", names=["twin"])
        self.assertEqual(res["added"], [])
        self.assertIn("2 live sessions share that name", res["unreachable"][0])
        self.client.request("close_room", name=r, by="kit")
        with self.assertRaises(self.client.AcmError) as cm:
            self.client.request("invite", room=r, by="kit", names=["twin"])
        self.assertEqual(cm.exception.code, "room_closed")

    def test_14_cli_add_flag(self):
        self.live_session("cliguy")
        r = subprocess.run(
            [sys.executable, "-m", "acm", "--as", "kit", "room", "-c", "w14", "--add", "cliguy,nobody"],
            capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=30,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("added and notified: cliguy", r.stdout)
        self.assertIn("nobody (no live session)", r.stderr)


if __name__ == "__main__":
    unittest.main()
