import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

from tests.test_wake import FakeSession

SRC = os.path.join(os.path.dirname(__file__), "..", "src")


class MultiRoomTest(unittest.TestCase):
    """One agent in several rooms, and an agent renamed mid-room, using stand-in sessions (no live agents)."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(dir="/tmp")
        root = cls.tmp.name
        cls.saved = dict(os.environ)
        os.environ.update(
            ACM_RUNTIME=os.path.join(root, "run"), ACM_DATA=os.path.join(root, "data"), ACM_ALLOW_NO_TTY="1",
            CLAUDE_CONFIG_DIR=os.path.join(root, "claude"), PYTHONPATH=SRC, ACM_WAKE_CONFIRM_SECS="60",
            ACM_CONFIG=os.path.join(root, "none.toml"),
        )
        os.makedirs(os.path.join(root, "claude", "sessions"))
        from acm import client
        cls.client = client
        subprocess.run([sys.executable, "-m", "acm", "daemon", "start"], check=True, capture_output=True)
        cls.sessions = {}
        for i, name in enumerate(["multi", "other"]):
            sess = FakeSession(root, os.environ["CLAUDE_CONFIG_DIR"], 960000 + i)
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

    def room(self, name, agents=("multi", "other")):
        self.client.request("create_room", name=name, by="kit")
        for a in agents:
            self.client.request("join", room=name, member=a, kind="agent")
        return name

    def human(self, room, body):
        return self.client.request("post", room=room, author="kit", body=body, **{"from": "human"})["wake"]

    def agent(self, room, author, body):
        return self.client.request("post", room=room, author=author, body=body, **{"from": "agent"})["wake"]

    def read(self, room, member):
        return self.client.request("read", room=room, member=member, kind="agent")

    def test_01_wakes_from_two_rooms_reach_one_session_and_name_their_room(self):
        a, b = self.room("ma"), self.room("mb")
        self.sessions["multi"].write_record("busy", int(time.time() * 1000))  # in the middle of other work
        n = len(self.sessions["multi"].lines)
        self.assertEqual(self.human(a, "@multi question for a")["woke"], ["multi"])
        self.assertEqual(self.human(b, "@multi question for b")["woke"], ["multi"])  # a's pending wake does not block b
        texts = self.sessions["multi"].wait_for(n + 2)[n:]
        self.assertEqual(len(texts), 2)
        self.assertTrue(any('[acm room ma]' in t and "question for a" in t and 'room_post(room="ma")' in t for t in texts))
        self.assertTrue(any('[acm room mb]' in t and "question for b" in t and 'room_post(room="mb")' in t for t in texts))

    def test_02_reading_or_posting_in_one_room_leaves_the_other_alone(self):
        a, b = self.room("mc"), self.room("md")
        self.human(a, "@multi in c")
        self.human(b, "@multi in d")
        self.read(a, "multi")  # catches up in c only
        self.assertEqual(self.human(b, "@multi again in d")["already_pending"], ["multi"])  # d still waits for a read
        self.assertEqual(self.human(a, "@multi again in c")["woke"], ["multi"])  # c is clear
        self.agent(b, "multi", "answering in d without reading")  # posting clears d's pending, not c's
        self.assertEqual(self.human(b, "@multi third in d")["woke"], ["multi"])
        self.assertEqual(self.human(a, "@multi third in c")["already_pending"], ["multi"])

    def test_03_unread_counts_are_per_room(self):
        a, b = self.room("me"), self.room("mf")
        for body in ("one", "two"):
            self.human(a, body)
        self.human(b, "three")
        unread = {r["name"]: r["unread"] for r in self.client.request("list_rooms", status="open", member="multi")["rooms"]}
        self.assertEqual((unread["me"], unread["mf"]), (2, 1))
        self.read(a, "multi")
        unread = {r["name"]: r["unread"] for r in self.client.request("list_rooms", status="open", member="multi")["rooms"]}
        self.assertEqual((unread["me"], unread["mf"]), (0, 1))

    def test_04_cooldown_counts_each_room_separately(self):
        a, b = self.room("mg"), self.room("mh")
        for _ in range(2):
            self.assertEqual(self.agent(a, "other", "@multi ping")["woke"], ["multi"])
            self.read(a, "multi")
        third = self.agent(a, "other", "@multi ping")
        self.assertEqual(third["woke"], [])
        self.assertIn("cooldown", third["suppressed"])
        self.assertEqual(self.agent(b, "other", "@multi ping")["woke"], ["multi"])  # room h has its own count

    def test_05_snooze_covers_every_room(self):
        a, b = self.room("mi"), self.room("mj")
        self.client.request("snooze", name="multi", seconds=60)
        try:
            for r in (a, b):
                wake = self.human(r, "@multi hello")
                self.assertEqual(wake["woke"], [])
                self.assertTrue(wake["unreachable"][0].startswith("multi (snoozed until"))
        finally:
            self.client.request("unsnooze", name="multi")
        self.assertEqual(self.human(a, "@multi hello again")["woke"], ["multi"])


class RenameTest(unittest.TestCase):
    """The MCP server run as a child of a stand-in session whose name changes while it is running."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(dir="/tmp")
        root = cls.tmp.name
        cls.saved = dict(os.environ)
        os.environ.update(
            ACM_RUNTIME=os.path.join(root, "run"), ACM_DATA=os.path.join(root, "data"), ACM_ALLOW_NO_TTY="1",
            CLAUDE_CONFIG_DIR=os.path.join(root, "claude"), PYTHONPATH=SRC, ACM_WAKE_CONFIRM_SECS="60",
            ACM_CONFIG=os.path.join(root, "none.toml"),
        )
        os.environ.pop("ACM_NAME", None)
        os.makedirs(os.path.join(root, "claude", "sessions"))
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

    def rpc(self, proc, n, tool, **args):
        msg = {"jsonrpc": "2.0", "id": n, "method": "tools/call", "params": {"name": tool, "arguments": args}}
        proc.stdin.write(json.dumps(msg) + "\n")
        proc.stdin.flush()
        reply = json.loads(proc.stdout.readline())["result"]
        return reply["content"][0]["text"], reply["isError"]

    def test_renaming_a_session_mid_room_gives_it_a_new_member_that_can_still_be_woken(self):
        self.client.request("create_room", name="rn", by="kit")
        # the "session": a wrapper process that the MCP server is a child of, with a session record and an inbox
        wrapper = subprocess.Popen(
            [sys.executable, "-c", "import subprocess,sys;sys.exit(subprocess.run([sys.executable,'-m','acm.mcp_server']).returncode)"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
        )
        self.addCleanup(lambda: (wrapper.stdin.close(), wrapper.wait(timeout=5), wrapper.stdout.close()))
        sess = FakeSession(self.tmp.name, os.environ["CLAUDE_CONFIG_DIR"], wrapper.pid, name="old-name")
        self.addCleanup(sess.close)

        text, err = self.rpc(wrapper, 1, "room_join", room="rn")
        self.assertFalse(err, text)
        self.assertIn("joined rn as old-name", text)
        self.rpc(wrapper, 2, "room_post", room="rn", body="first")

        sess.name = "new-name"  # /rename in Claude Code rewrites the session record
        sess.write_record("idle", 0)
        text, err = self.rpc(wrapper, 3, "room_post", room="rn", body="second")
        self.assertFalse(err, text)

        log = self.client.request("read", room="rn", member="viewer", since=0)["messages"]
        by = {m["body"]: m["author"] for m in log if m["kind"] == "post"}
        self.assertEqual(by, {"first": "old-name", "second": "new-name"})
        members = {m["name"]: m["kind"] for m in self.client.request("members", room="rn")["members"]}
        self.assertEqual((members["old-name"], members["new-name"]), ("agent", "agent"))  # two members, one session
        # both names reach the same session, so a mention of either still lands
        n = len(sess.lines)
        for name in ("new-name", "old-name"):
            wake = self.client.request("post", room="rn", author="kit", body=f"@{name} still there?", **{"from": "human"})["wake"]
            self.assertEqual(wake["woke"], [name], name)
            self.client.request("read", room="rn", member=name, kind="agent")  # clear so the next one can wake
        self.assertEqual(len(sess.wait_for(n + 2)), n + 2)


if __name__ == "__main__":
    unittest.main()
