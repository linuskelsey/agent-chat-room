import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest

SRC = os.path.join(os.path.dirname(__file__), "..", "src")


class LifecycleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(dir="/tmp")
        root = cls.tmp.name
        os.makedirs(os.path.join(root, "bin"))
        os.makedirs(os.path.join(root, "claude", "sessions"))
        cls.notes = os.path.join(root, "notes.txt")
        fake = os.path.join(root, "bin", "notify-send")
        with open(fake, "w") as f:
            f.write(f'#!/bin/sh\nprintf "%s\\n" "$*" >> {cls.notes}\n')
        os.chmod(fake, os.stat(fake).st_mode | stat.S_IEXEC)
        cls.exports = os.path.join(root, "exports")
        cls.saved = dict(os.environ)
        os.environ.update(
            ACM_RUNTIME=os.path.join(root, "run"), ACM_DATA=os.path.join(root, "data"),
            CLAUDE_CONFIG_DIR=os.path.join(root, "claude"), PYTHONPATH=SRC, ACM_ALLOW_NO_TTY="1",
            ACM_CONFIG=os.path.join(root, "none.toml"), PATH=os.path.join(root, "bin") + os.pathsep + os.environ["PATH"],
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

    def acm(self, *args, name="kit"):
        return subprocess.run([sys.executable, "-m", "acm", "--as", name, *args], capture_output=True, text=True)

    def room(self, name, *flags):
        r = self.acm("new", name, "-t", f"topic of {name}", "-L", f"export_dir={self.exports}", *flags)
        self.assertEqual(r.returncode, 0, r.stderr)
        return name

    def agent_post(self, room, author, body, kind="post", **extra):
        return self.client.request("post", room=room, author=author, body=body, kind=kind, **{"from": "agent"}, **extra)

    def human_post(self, room, body, name="kit", **extra):
        return self.client.request("post", room=room, author=name, body=body, **{"from": "human"}, **extra)

    def log(self, room):
        return self.client.request("read", room=room, member="viewer", since=0)["messages"]

    def notified(self):
        if not os.path.exists(self.notes):
            return ""
        with open(self.notes) as f:
            return f.read()

    def wait_notified(self, text, secs=3):
        end = time.time() + secs
        while time.time() < end and text not in self.notified():
            time.sleep(0.05)
        return text in self.notified()

    def test_01_close_prints_logs_saves_and_notifies(self):
        r = self.room("l1")
        self.client.request("join", room=r, member="arx", kind="agent")
        self.client.request("join", room=r, member="hub", kind="agent")
        self.human_post(r, "@arx please review src/db.py", refs=["src/db.py"])
        self.agent_post(r, "arx", "reviewed, looks fine", refs=["abc1234"])
        self.agent_post(r, "arx", "use sqlite for storage", kind="decision")
        self.human_post(r, "@hub what do you think?")
        out = self.acm("close", r)
        self.assertEqual(out.returncode, 0, out.stderr)
        text = out.stdout
        self.assertIn("closed l1", text)
        self.assertIn("1. (arx) use sqlite for storage", text)
        self.assertIn("@hub was asked by kit", text)
        self.assertNotIn("@arx was asked", text)  # arx answered
        self.assertIn("- src/db.py (kit)", text)
        self.assertIn("- abc1234 (arx)", text)
        self.assertIn("How it ended:", text)
        # it is also the room's final system message, and the same text is saved to a file
        last = self.log(r)[-1]
        self.assertEqual(last["kind"], "system")
        self.assertTrue(last["body"].startswith("room closed by kit\n\nTopic: topic of l1"))
        self.assertIn("1. (arx) use sqlite for storage", last["body"])
        saved = os.path.join(self.exports, "l1.md")
        self.assertIn(f"saved to {saved}", text)
        with open(saved) as f:
            body = f.read()
        self.assertTrue(body.startswith("# Room: l1"))
        self.assertIn("## Transcript", body)  # the saved file has the whole conversation, not only the summary
        self.assertIn("**kit (human)**: @arx please review src/db.py [refs: src/db.py]", body)
        self.assertIn("**arx** **DECISION**: use sqlite for storage", body)
        self.assertIn("*room closed by kit*", body)
        self.assertTrue(self.wait_notified("acm: l1 closed"))
        self.assertIn("Final decision: use sqlite for storage", self.notified())

    def test_02_no_decisions_is_said_plainly(self):
        r = self.room("l2")
        self.human_post(r, "just chatting")
        out = self.acm("close", r).stdout
        self.assertIn("none pinned", out)
        self.assertTrue(self.wait_notified("No decisions were pinned."))

    def test_03_kill_also_summarises(self):
        r = self.room("l3")
        self.agent_post(r, "arx", "one decision", kind="decision")
        out = self.acm("kill", r, name="other").stdout
        self.assertIn("killed l3", out)
        self.assertIn("1. (arx) one decision", out)
        self.assertIn("room killed by other", self.log(r)[-1]["body"])

    def test_04_open_items_ignore_answered_quiet_and_broadcast_mentions(self):
        r = self.room("l4")
        self.client.request("join", room=r, member="arx", kind="agent")
        self.client.request("join", room=r, member="hub", kind="agent")  # open items are people actually in the room
        self.human_post(r, "@arx answered question?")
        self.agent_post(r, "arx", "yes")
        self.human_post(r, "@mig fyi only", no_reply_needed=True)
        self.human_post(r, "@all general note @human too")
        self.human_post(r, "@hub unanswered one")
        text = self.acm("summary", r).stdout
        self.assertIn("@hub was asked by kit", text)
        self.assertEqual(text.count("was asked by"), 1)

    def test_04b_prose_that_looks_like_a_mention_is_not_an_open_item(self):
        r = self.room("l4b")
        self.client.request("join", room=r, member="arx", kind="agent")
        self.agent_post(r, "arx", "collapse repeated @mentions into one wake, as @nobody suggested", kind="decision")
        text = self.acm("summary", r).stdout
        self.assertNotIn("was asked by", text)

    def test_05_summary_works_for_open_and_closed_rooms_and_export_has_the_transcript(self):
        r = self.room("l5")
        self.human_post(r, "first line")
        self.assertIn("Decisions:", self.acm("summary", r).stdout)
        out = self.acm("export", r, "-o", os.path.join(self.tmp.name, "full.md"))
        self.assertEqual(out.returncode, 0, out.stderr)
        with open(os.path.join(self.tmp.name, "full.md")) as f:
            full = f.read()
        self.assertIn("## Transcript", full)
        self.assertIn("**kit (human)**: first line", full)
        brief = self.acm("export", r, "--summary-only").stdout
        self.assertNotIn("## Transcript", brief)
        self.acm("close", r)
        self.assertIn("first line", self.acm("summary", r).stdout)  # still readable once closed
        self.assertNotEqual(self.acm("post", r, "late").returncode, 0)  # but read-only

    def test_06_search_covers_closed_rooms(self):
        a = self.room("l6a")
        b = self.room("l6b")
        self.human_post(a, "the Zebra migration plan is 100% ready")
        self.agent_post(b, "arx", "zebra crossing notes")
        self.acm("close", a)
        everywhere = self.acm("search", "ZEBRA").stdout
        self.assertIn("l6a [closed]", everywhere)
        self.assertIn("l6b #", everywhere)
        self.assertNotIn("[closed]", self.acm("search", "zebra", "--room", "l6b").stdout)
        only_closed = self.acm("search", "zebra", "--closed").stdout
        self.assertIn("l6a", only_closed)
        self.assertNotIn("l6b", only_closed)
        self.assertIn("arx", self.acm("search", "zebra", "--author", "arx").stdout)
        self.assertIn("l6a", self.acm("search", "100%").stdout)  # % is literal
        self.assertEqual(self.acm("search", "zzzz-nothing").stdout.strip(), "no matches")
        self.assertNotIn("closed by", self.acm("search", "closed by").stdout.replace("no matches", ""))  # system lines hidden
        self.assertIn("room closed by", self.acm("search", "closed by", "--system").stdout)
        hits = json.loads(self.acm("search", "zebra", "--json").stdout)
        self.assertEqual({h["room"] for h in hits}, {"l6a", "l6b"})

    def test_07_project_link_resolves_commits_and_flags_missing_files(self):
        repo = os.path.join(self.tmp.name, "repo")
        os.makedirs(os.path.join(repo, "src"))
        with open(os.path.join(repo, "src", "real.py"), "w") as f:
            f.write("x = 1\n")
        git = lambda *a: subprocess.run(["git", "-C", repo, *a], capture_output=True, text=True, check=True)
        git("init", "-q")
        git("-c", "user.name=t", "-c", "user.email=t@t", "add", ".")
        git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "add the real file")
        sha = git("rev-parse", "--short", "HEAD").stdout.strip()
        r = self.room("l7", "--dir", repo)
        self.assertEqual(self.client.request("get_room", name=r)["room"]["project_dir"], os.path.realpath(repo))
        self.human_post(r, "refs", refs=["src/real.py", "src/ghost.py", sha, "deadbeef", "../outside", "/etc/hostname"])
        text = self.acm("summary", r).stdout
        self.assertIn(f"Project: {os.path.realpath(repo)}", text)
        self.assertIn("- src/real.py (kit)\n", text)
        self.assertIn("- src/ghost.py (kit) (not found in the project)", text)
        self.assertIn(f"- {sha} (kit) add the real file", text)
        self.assertIn("- deadbeef (kit) (not found in the project repo)", text)
        self.assertIn("- ../outside (kit)\n", text)  # outside the project: no probing
        self.assertIn("- /etc/hostname (kit)\n", text)
        self.assertIn("is unlinked", self.acm("link", r, "none").stdout)
        self.assertNotIn("Project:", self.acm("summary", r).stdout)
        bad = self.acm("link", r, "/no/such/dir")
        self.assertNotEqual(bad.returncode, 0)
        self.assertIn("not a directory", bad.stderr)

    def test_08_continue_seeds_a_new_room_with_the_old_summary(self):
        repo = os.path.join(self.tmp.name, "repo8")
        os.makedirs(repo)
        old = self.room("l8old", "--dir", repo)
        self.agent_post(old, "arx", "ship it behind a flag", kind="decision")
        self.acm("close", old)
        r = self.acm("new", "l8new", "--continue", "l8old")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("seeded with the summary of l8old", r.stdout)
        first = self.log("l8new")[0]
        self.assertEqual(first["kind"], "system")
        self.assertTrue(first["body"].startswith("continued from l8old"))
        self.assertIn("1. (arx) ship it behind a flag", first["body"])
        self.assertEqual(self.client.request("get_room", name="l8new")["room"]["project_dir"], os.path.realpath(repo))
        # a joining agent sees the carried decisions' context in its recent messages
        joined = self.client.request("catch_up", room="l8new", member="newagent", keep=5, kind="agent")
        self.assertIn("ship it behind a flag", " ".join(m["body"] for m in joined["messages"]))
        missing = self.acm("new", "l8x", "--continue", "nope")
        self.assertNotEqual(missing.returncode, 0)
        self.assertNotIn("l8x", self.acm("ls", "--all").stdout)  # nothing half-created

    def test_09_ls_closed_and_the_room_client_close_prints_the_summary(self):
        r = self.room("l9")
        self.agent_post(r, "arx", "pinned in the client test", kind="decision")
        out = subprocess.run(
            [sys.executable, "-m", "acm", "--as", "kit", "room", r], capture_output=True, text=True,
            input="/close\ny\n", timeout=30,
        )
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("1. (arx) pinned in the client test", out.stdout)
        self.assertIn("saved to", out.stdout)
        listed = self.acm("ls", "--closed").stdout
        self.assertIn("l9", listed)
        self.assertNotIn("l9", self.acm("ls").stdout)

    def test_09b_budget_shows_where_the_summary_will_go(self):
        r = self.acm("new", "l9b", "-t", "t")
        default = self.acm("budget", "l9b").stdout
        self.assertIn(f"summary on close: {os.environ['ACM_DATA']}/rooms/l9b.md (default)", default)
        set_ = self.acm("budget", "l9b", f"export_dir={self.exports}").stdout
        self.assertIn(f"summary on close: {self.exports}/l9b.md\n", set_)
        self.assertIn("(default)", self.acm("budget", "l9b", "export_dir=default").stdout)

    def test_09c_wrapup_asks_exactly_one_agent(self):
        r = self.room("l9c")
        for a in ("arx", "hub"):
            self.client.request("join", room=r, member=a, kind="agent")
        out = self.acm("wrapup", r, "arx")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("asked arx to pin a summary", out.stdout)
        posts = [m for m in self.log(r) if m["kind"] == "post"]
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]["mentions"], ["arx"])  # not hub, not @all
        self.assertIn("room_pin_decision", posts[0]["body"])
        self.assertNotIn("@all", posts[0]["body"])
        many = self.acm("wrapup", r, "arx", "hub")
        self.assertNotEqual(many.returncode, 0)  # exactly one agent
        client_out = subprocess.run(
            [sys.executable, "-m", "acm", "--as", "kit", "room", r], capture_output=True, text=True,
            input="/wrapup arx hub\n/wrapup\n", timeout=30,
        )
        self.assertEqual(client_out.stdout.count("usage: /wrapup AGENT (exactly one agent)"), 2)  # both forms refused
        self.assertEqual(len([m for m in self.log(r) if m["kind"] == "post"]), 1)  # still one: both invalid forms refused

    def test_10_default_export_directory_and_forbidden_close_keep_the_room_open(self):
        r = self.acm("new", "l10", "-t", "t")
        self.assertEqual(r.returncode, 0, r.stderr)
        denied = self.acm("close", "l10", name="intruder")
        self.assertNotEqual(denied.returncode, 0)
        self.assertIn("only the creator", denied.stderr)
        self.assertEqual(self.client.request("get_room", name="l10")["room"]["status"], "open")
        out = self.acm("close", "l10").stdout
        default = os.path.join(os.environ["ACM_DATA"], "rooms", "l10.md")
        self.assertIn(f"saved to {default}", out)
        self.assertTrue(os.path.exists(default))


if __name__ == "__main__":
    unittest.main()
