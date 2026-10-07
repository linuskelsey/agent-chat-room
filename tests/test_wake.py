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

    def __init__(self, root, claude_dir, pid, status="idle"):
        self.pid = pid
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
                 "messagingSocketPath": socket_path or self.path},
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
            ACM_WAKE_CONFIRM_SECS="1", PATH=os.path.join(root, "bin") + os.pathsep + os.environ["PATH"],
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
        self.assertIn("[acm room w1] kit mentioned you", texts[-1])
        self.assertIn('room_read(room="w1")', texts[-1])
        self.assertNotIn("can you look", texts[-1])  # a pointer, never the message itself
        time.sleep(0.2)
        self.assertEqual(self.count("hub"), before["hub"])
        self.assertEqual(self.count("mig"), before["mig"])

    def test_02_unaddressed_posts_are_passive(self):
        r = self.room("w2")
        before = {n: self.count(n) for n in self.sessions}
        wake = self.post(r, "kit", "just chatting, no mentions")
        self.assertEqual(wake, {"woke": [], "already_pending": [], "unreachable": [], "notified": []})
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


if __name__ == "__main__":
    unittest.main()
