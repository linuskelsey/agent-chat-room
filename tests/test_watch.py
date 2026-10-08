import json
import os
import select
import subprocess
import sys
import tempfile
import time
import unittest

SRC = os.path.join(os.path.dirname(__file__), "..", "src")


class WatchTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(dir="/tmp")
        root = cls.tmp.name
        cls.saved = dict(os.environ)
        os.makedirs(os.path.join(root, "claude", "sessions"))
        os.environ.update(
            ACM_RUNTIME=os.path.join(root, "run"), ACM_DATA=os.path.join(root, "data"), ACM_ALLOW_NO_TTY="1",
            CLAUDE_CONFIG_DIR=os.path.join(root, "claude"), PYTHONPATH=SRC,
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

    def events(self, w, n, timeout=3):
        w._sock.settimeout(timeout)
        return [next(w) for _ in range(n)]

    def test_01_one_stream_carries_every_room(self):
        w = self.client.watch()
        self.addCleanup(w.close)
        self.client.request("create_room", name="wa", by="kit", topic="first")
        self.client.request("create_room", name="wb", by="kit")
        self.client.request("post", room="wa", author="kit", body="in a", **{"from": "human"})
        self.client.request("post", room="wb", author="kit", body="in b", **{"from": "human"})
        self.client.request("close_room", name="wb", by="kit")
        evs = self.events(w, 6)
        kinds = [(e["event"], e["room_name"]) for e in evs]
        self.assertEqual(kinds[:2], [("room_created", "wa"), ("room_created", "wb")])
        messages = [(e["room_name"], e["message"]["body"]) for e in evs if e["event"] == "message" and e["message"]["kind"] == "post"]
        self.assertEqual(messages, [("wa", "in a"), ("wb", "in b")])
        self.assertIn(("closed", "wb"), kinds)
        self.assertEqual(evs[0]["room"]["topic"], "first")  # created rooms arrive with their details

    def test_02_a_room_stream_carries_only_that_room_and_names_it(self):
        self.client.request("create_room", name="wc", by="kit")
        self.client.request("create_room", name="wd", by="kit")
        w = self.client.watch("wc")
        self.addCleanup(w.close)
        self.client.request("post", room="wd", author="kit", body="elsewhere", **{"from": "human"})
        self.client.request("post", room="wc", author="kit", body="here", **{"from": "human"})
        ev = self.events(w, 1)[0]
        self.assertEqual((ev["room_name"], ev["message"]["body"]), ("wc", "here"))

    def test_03_admin_changes_arrive_as_room_updated(self):
        self.client.request("create_room", name="we", by="kit")
        self.client.request("join", room="we", member="arx", kind="agent")
        w = self.client.watch()
        self.addCleanup(w.close)
        self.client.request("mute", room="we", member="arx")
        self.client.request("set_limits", room="we", updates={"max_messages": 9})
        evs = self.events(w, 2)
        self.assertEqual([(e["event"], e["what"], e["room_name"]) for e in evs],
                         [("room_updated", "mute", "we"), ("room_updated", "limits", "we")])

    def test_04_cap_warnings_arrive_on_the_stream(self):
        self.client.request("create_room", name="wf", by="kit")
        self.client.request("set_limits", room="wf", updates={"max_messages": 5})
        w = self.client.watch()
        self.addCleanup(w.close)
        for i in range(4):
            self.client.request("post", room="wf", author="kit", body=f"m{i}", **{"from": "human"})
        evs = [e for e in self.events(w, 5) if e["event"] == "warning"]
        self.assertTrue(evs and "max_messages" in evs[0]["text"] and evs[0]["room_name"] == "wf")

    def test_05_cli_watch_prints_json_lines(self):
        self.client.request("create_room", name="wg", by="kit")
        p = subprocess.Popen([sys.executable, "-m", "acm", "watch"], stdout=subprocess.PIPE, text=True, env=os.environ)
        self.addCleanup(lambda: (p.kill(), p.wait(), p.stdout.close()))
        lines = []
        end = time.time() + 6
        while time.time() < end and not any('"cli says hi"' in l for l in lines):
            self.client.request("post", room="wg", author="kit", body="cli says hi", **{"from": "human"})
            if select.select([p.stdout], [], [], 0.5)[0]:
                lines.append(p.stdout.readline())
        events = [json.loads(l) for l in lines if l.strip()]
        self.assertTrue(any(e["event"] == "message" and e["message"]["body"] == "cli says hi" and e["room_name"] == "wg" for e in events))

    def test_06_cli_watch_can_follow_one_room(self):
        self.client.request("create_room", name="wh", by="kit")
        self.client.request("create_room", name="wi", by="kit")
        p = subprocess.Popen([sys.executable, "-m", "acm", "watch", "--room", "wh"], stdout=subprocess.PIPE, text=True, env=os.environ)
        self.addCleanup(lambda: (p.kill(), p.wait(), p.stdout.close()))
        time.sleep(0.8)
        self.client.request("post", room="wi", author="kit", body="not for me", **{"from": "human"})
        self.client.request("post", room="wh", author="kit", body="for me", **{"from": "human"})
        self.assertTrue(select.select([p.stdout], [], [], 3)[0])
        ev = json.loads(p.stdout.readline())
        self.assertEqual((ev["room_name"], ev["message"]["body"]), ("wh", "for me"))


if __name__ == "__main__":
    unittest.main()
