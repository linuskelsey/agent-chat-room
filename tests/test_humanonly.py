import json
import os
import subprocess
import sys
import tempfile
import unittest

SRC = os.path.join(os.path.dirname(__file__), "..", "src")


class HumanOnlyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(dir="/tmp")
        cls.claude = os.path.join(cls.tmp.name, "claude")
        os.makedirs(os.path.join(cls.claude, "sessions"))
        cls.env = {
            **os.environ, "ACM_RUNTIME": os.path.join(cls.tmp.name, "run"), "ACM_DATA": os.path.join(cls.tmp.name, "data"),
            "CLAUDE_CONFIG_DIR": cls.claude, "PYTHONPATH": SRC, "ACM_ALLOW_NO_TTY": "1",
        }
        cls.run_acm("new", "ho", name="kit")

    @classmethod
    def tearDownClass(cls):
        cls.run_acm("daemon", "stop")
        cls.tmp.cleanup()

    @classmethod
    def run_acm(cls, *args, name="kit", inside_session=False):
        """Run `acm`. With inside_session, a wrapper process that has a Claude session record is its parent."""
        cmd = [sys.executable, "-m", "acm", "--as", name, *args]
        if inside_session:
            wrapper = (
                "import json,os,subprocess,sys;"
                f"open(os.path.join({cls.claude!r},'sessions',str(os.getpid())+'.json'),'w').write(json.dumps({{'pid':os.getpid()}}));"
                f"r=subprocess.run({cmd!r});sys.exit(r.returncode)"
            )
            cmd = [sys.executable, "-c", wrapper]
        return subprocess.run(cmd, env=cls.env, capture_output=True, text=True, timeout=30)

    @classmethod
    def child_of_session(cls, code, env):
        """Run python `code` as a child of a process that has a Claude session record."""
        wrapper = (
            "import json,os,subprocess,sys;"
            f"open(os.path.join({cls.claude!r},'sessions',str(os.getpid())+'.json'),'w').write(json.dumps({{'pid':os.getpid()}}));"
            f"r=subprocess.run([sys.executable,'-c',{code!r}]);sys.exit(r.returncode)"
        )
        return subprocess.run([sys.executable, "-c", wrapper], env=env, capture_output=True, text=True, timeout=30)

    def test_01_admin_actions_are_refused_inside_a_session(self):
        for args in (
            ("new", "sneaky"),
            ("close", "ho"),
            ("kill", "ho"),
            ("mute", "ho", "kit"),
            ("budget", "ho", "max_messages=1"),
            ("invite", "ho", "someone"),
            ("post", "ho", "pretending to be the human"),
            ("daemon", "stop"),
        ):
            r = self.run_acm(*args, inside_session=True)
            self.assertNotEqual(r.returncode, 0, args)
            self.assertIn("inside a Claude Code session", r.stderr, args)
        self.assertEqual(self.run_acm("ls").returncode, 0)
        self.assertNotIn("sneaky", self.run_acm("ls", "--all").stdout)
        self.assertEqual(self.run_acm("daemon", "status").returncode, 0)  # the daemon is still up
        self.assertIn("open", json.loads(self.run_acm("ls", "--json").stdout)[0]["status"])

    def test_02_reading_and_agent_posts_still_work_inside_a_session(self):
        for args in (("ls",), ("tail", "ho"), ("members", "ho"), ("read", "ho", "--peek"), ("budget", "ho")):
            r = self.run_acm(*args, inside_session=True)
            self.assertEqual(r.returncode, 0, (args, r.stderr))
        env = {**self.env, "ACM_NAME": "botty"}
        r = self.child_of_session(
            "from acm import client;"
            "print(client.request('post',room='ho',author='botty',body='agent post',**{'from':'agent'})['message']['id'])",
            env,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        msgs = json.loads(self.run_acm("read", "ho", "--since", "0", "--json").stdout)["messages"]
        self.assertEqual([(m["author"], m["from"]) for m in msgs if m["body"] == "agent post"], [("botty", "agent")])

    def test_03_humans_in_a_normal_terminal_can_do_everything(self):
        self.assertEqual(self.run_acm("new", "second").returncode, 0)
        self.assertEqual(self.run_acm("post", "second", "hello").returncode, 0)
        self.assertEqual(self.run_acm("budget", "second", "max_messages=50").returncode, 0)
        self.assertEqual(self.run_acm("close", "second").returncode, 0)

    def test_04_a_session_cannot_claim_a_human_post_by_lying_about_its_kind(self):
        r = self.child_of_session(
            "from acm import client;"
            "client.request('post',room='ho',author='kit',body='as human',**{'from':'human'})",
            self.env,
        )
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("inside a Claude Code session", r.stderr)

    def test_05_detaching_from_the_session_does_not_help(self):
        """The environment a session's tools run in is inherited by a detached process, which gives it away."""
        rc = os.path.join(self.tmp.name, "detached.rc")
        if os.path.exists(rc):
            os.remove(rc)
        code = (
            "import os,subprocess,sys,time;"
            f"subprocess.run(['setsid','-f','sh','-c',{sys.executable!r}+' -m acm --as kit new detached 2>{rc}.err; echo $? > {rc}']);"
            f"[time.sleep(0.1) for _ in range(100) if not os.path.exists({rc!r})]"
        )
        env = {**self.env, "CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "abc"}  # what a session's Bash tool carries
        subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, timeout=30)
        with open(rc) as f:
            self.assertNotEqual(f.read().strip(), "0")
        with open(rc + ".err") as f:
            self.assertIn("inside a Claude Code session", f.read())
        self.assertNotIn("detached", self.run_acm("ls", "--all").stdout)

    def test_06_a_marked_environment_is_enough_on_its_own(self):
        env = {**self.env, "CLAUDE_CODE_MESSAGING_SOCKET": "/x"}
        r = subprocess.run([sys.executable, "-m", "acm", "--as", "kit", "close", "ho"], env=env, capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("inside a Claude Code session", r.stderr)


class TerminalCheckTest(unittest.TestCase):
    """The daemon as users run it: admin actions need a controlling terminal."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(dir="/tmp")
        os.makedirs(os.path.join(cls.tmp.name, "claude", "sessions"))
        cls.env = {k: v for k, v in os.environ.items() if k != "ACM_ALLOW_NO_TTY"}
        cls.env.update(
            ACM_RUNTIME=os.path.join(cls.tmp.name, "run"), ACM_DATA=os.path.join(cls.tmp.name, "data"),
            CLAUDE_CONFIG_DIR=os.path.join(cls.tmp.name, "claude"), PYTHONPATH=SRC,
        )
        for marker in ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_PID"):
            cls.env.pop(marker, None)

    @classmethod
    def tearDownClass(cls):
        cls.on_terminal("daemon", "stop")
        cls.tmp.cleanup()

    @classmethod
    def on_terminal(cls, *args):
        """Run acm with a controlling terminal, like a person's shell. Returns (exit code, output)."""
        import pty
        pid, fd = pty.fork()
        if pid == 0:
            os.execvpe(sys.executable, [sys.executable, "-m", "acm", "--as", "kit", *args], cls.env)
        out = b""
        while True:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            out += chunk
        _, status = os.waitpid(pid, 0)
        os.close(fd)
        return os.waitstatus_to_exitcode(status), out.decode()

    def no_terminal(self, *args, env=None):
        return subprocess.run(
            [sys.executable, "-m", "acm", "--as", "kit", *args], env=env or self.env, capture_output=True, text=True,
            stdin=subprocess.DEVNULL, start_new_session=True, timeout=30,
        )

    def test_admin_actions_need_a_terminal(self):
        code, out = self.on_terminal("new", "withtty")
        self.assertEqual(code, 0, out)
        r = self.no_terminal("new", "notty")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("needs an interactive terminal", r.stderr)
        self.assertNotIn("notty", self.no_terminal("ls", "--all").stdout)
        self.assertIn("withtty", self.no_terminal("ls").stdout)  # reading needs no terminal
        self.assertNotEqual(self.no_terminal("post", "withtty", "hi").returncode, 0)
        code, out = self.on_terminal("post", "withtty", "hi")
        self.assertEqual(code, 0, out)

    def test_scrubbing_the_environment_still_leaves_no_terminal(self):
        # the trick that beats the environment check: strip the markers and detach (setsid gives up the terminal)
        clean = {k: v for k, v in self.env.items() if not k.startswith("CLAUDE")}
        r = self.no_terminal("close", "withtty", env=clean)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("needs an interactive terminal", r.stderr)
        self.assertEqual(self.on_terminal("close", "withtty")[0], 0)


if __name__ == "__main__":
    unittest.main()
