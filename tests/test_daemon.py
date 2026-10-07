import json
import os
import select
import subprocess
import sys
import tempfile
import threading
import unittest

SRC = os.path.join(os.path.dirname(__file__), "..", "src")


class DaemonTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.env = {
            **os.environ,
            "ACM_RUNTIME": os.path.join(cls.tmp.name, "run"),
            "ACM_DATA": os.path.join(cls.tmp.name, "data"),
            "PYTHONPATH": SRC,
        }

    @classmethod
    def tearDownClass(cls):
        cls.acm("daemon", "stop")
        cls.tmp.cleanup()

    @classmethod
    def acm(cls, *args, name="tester", stdin=None):
        return subprocess.run(
            [sys.executable, "-m", "acm", "--as", name, *args],
            env=cls.env, capture_output=True, text=True, input=stdin, timeout=30,
        )

    def test_01_autostart_and_flow(self):
        r = self.acm("new", "flow", "-t", "topic")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.acm("daemon", "status").returncode, 0)
        self.assertEqual(self.acm("post", "flow", "hello", "there", name="ann").returncode, 0)
        self.assertEqual(self.acm("post", "flow", "-d", "ship it", name="bob").returncode, 0)
        out = json.loads(self.acm("read", "flow", "--json", name="tester").stdout)
        self.assertEqual([m["body"] for m in out["messages"]], ["hello there", "ship it"])
        self.assertEqual([m["author"] for m in out["messages"]], ["ann", "bob"])
        self.assertTrue(all(m["from"] == "human" for m in out["messages"]))
        self.assertEqual(self.acm("read", "flow", "--json", name="tester").stdout.count('"id"'), 1)  # decision only
        self.assertIn("flow", self.acm("ls").stdout)

    def test_02_stdin_post(self):
        self.acm("new", "pipe")
        self.assertEqual(self.acm("post", "pipe", stdin="from stdin\nline two").returncode, 0)
        msgs = json.loads(self.acm("read", "pipe", "--since", "0", "--json").stdout)["messages"]
        self.assertEqual(msgs[0]["body"], "from stdin\nline two")

    def test_03_concurrent_posters(self):
        self.acm("new", "busy")
        n_threads, per = 6, 10

        def work(i):
            for j in range(per):
                r = self.acm("post", "busy", f"t{i}-{j}", name=f"w{i}")
                assert r.returncode == 0, r.stderr

        ts = [threading.Thread(target=work, args=(i,)) for i in range(n_threads)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        msgs = json.loads(self.acm("read", "busy", "--since", "0", "--json").stdout)["messages"]
        ids = [m["id"] for m in msgs]
        self.assertEqual(len(msgs), n_threads * per)
        self.assertEqual(ids, sorted(ids))
        self.assertEqual(len(set(ids)), len(ids))
        for i in range(n_threads):  # per-author order preserved
            mine = [m["body"] for m in msgs if m["author"] == f"w{i}"]
            self.assertEqual(mine, [f"t{i}-{j}" for j in range(per)])

    def test_04_close_rules(self):
        self.acm("new", "gate", name="owner")
        r = self.acm("close", "gate", name="other")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("only the creator", r.stderr)
        self.assertEqual(self.acm("close", "gate", name="owner").returncode, 0)
        r = self.acm("post", "gate", "late", name="owner")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("closed", r.stderr)
        self.acm("new", "forced", name="owner")
        self.assertEqual(self.acm("kill", "forced", name="other").returncode, 0)

    def test_05_tail_follow_sees_new_messages_and_close(self):
        self.acm("new", "live", name="owner")
        p = subprocess.Popen(
            [sys.executable, "-m", "acm", "--as", "watcher", "tail", "live", "-f"],
            env=self.env, stdout=subprocess.PIPE, text=True,
        )
        try:
            # tail -f is subscribed once the daemon sees its watcher; poll by posting until echoed
            import time
            deadline = time.time() + 10
            first = None
            while time.time() < deadline and first is None:
                self.acm("post", "live", "ping", name="owner")
                time.sleep(0.2)
                if select.select([p.stdout], [], [], 1)[0]:
                    first = p.stdout.readline() or None
            self.assertIn("ping", first)
            self.acm("close", "live", name="owner")
            rest = p.stdout.read()
            self.assertIn("closed", rest)
            p.wait(timeout=5)
        finally:
            p.kill()
            p.stdout.close()

    def test_06_socket_is_private(self):
        self.acm("daemon", "start")
        sock = os.path.join(self.env["ACM_RUNTIME"], "acm.sock")
        self.assertEqual(oct(os.stat(sock).st_mode & 0o777), "0o600")
        self.assertEqual(oct(os.stat(os.path.dirname(sock)).st_mode & 0o777), "0o700")

    def test_07_garbage_does_not_kill_daemon(self):
        import socket
        s = socket.socket(socket.AF_UNIX)
        s.connect(os.path.join(self.env["ACM_RUNTIME"], "acm.sock"))
        s.sendall(b"not json\n[1,2]\n" + json.dumps({"op": "post", "room": "flow", "author": 5, "body": 7}).encode() + b"\n")
        f = s.makefile("rb")
        for _ in range(3):
            self.assertFalse(json.loads(f.readline())["ok"])
        s.close()
        self.assertEqual(self.acm("daemon", "status").returncode, 0)

    def test_08_as_flag_works_after_subcommand(self):
        r = subprocess.run(
            [sys.executable, "-m", "acm", "new", "aftersub", "--as", "zed"], env=self.env, capture_output=True, text=True
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(self.acm("members", "aftersub", "--json").stdout)[0]["name"], "zed")

    def test_09_room_create_flag(self):
        def room(*extra):
            return subprocess.run(
                [sys.executable, "-m", "acm", "--as", "kit", "room", *extra],
                env=self.env, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=30,
            )

        r = room("ghost")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("no such room", r.stderr)
        r = room("-c", "-t", "made on demand", "ghost")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("created room ghost", r.stdout)
        self.assertEqual(room("-c", "ghost").returncode, 0)  # already exists: just joins
        self.assertIn("made on demand", self.acm("ls", "--json").stdout)


if __name__ == "__main__":
    unittest.main()
