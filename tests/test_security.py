import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from acm import client, fsutil
from acm.errors import AcmError

SRC = os.path.join(os.path.dirname(__file__), "..", "src")


def mode(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


class FsUtilTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_a_private_dir_is_created_with_owner_only_access(self):
        d = fsutil.ensure_private_dir(self.root / "a" / "b")
        self.assertEqual(mode(d), 0o700)

    def test_an_existing_dir_we_own_is_tightened(self):
        d = self.root / "loose"
        d.mkdir(mode=0o755)
        os.chmod(d, 0o755)
        fsutil.ensure_private_dir(d)
        self.assertEqual(mode(d), 0o700)

    def test_a_symlink_is_refused_even_if_it_points_at_a_good_directory(self):
        target = self.root / "real"
        target.mkdir()
        link = self.root / "link"
        link.symlink_to(target)
        with self.assertRaises(AcmError) as cm:
            fsutil.ensure_private_dir(link)
        self.assertEqual(cm.exception.code, "insecure_path")

    def test_a_directory_owned_by_someone_else_is_refused(self):
        d = self.root / "theirs"
        d.mkdir()
        with mock.patch("acm.fsutil.os.getuid", return_value=os.getuid() + 1):
            with self.assertRaises(AcmError) as cm:
                fsutil.ensure_private_dir(d)
        self.assertEqual(cm.exception.code, "insecure_path")

    def test_atomic_write_creates_private_files_and_keeps_existing_modes(self):
        new = self.root / "new.txt"
        fsutil.atomic_write(new, "hello")
        self.assertEqual((new.read_text(), mode(new)), ("hello", 0o600))
        old = self.root / "old.txt"
        old.write_text("x")
        os.chmod(old, 0o644)
        fsutil.atomic_write(old, "y")
        self.assertEqual((old.read_text(), mode(old)), ("y", 0o644))

    def test_atomic_write_follows_a_symlink_so_dotfile_managers_keep_working(self):
        real = self.root / "dotfiles" / "settings.json"
        real.parent.mkdir()
        real.write_text("{}")
        link = self.root / "settings.json"
        link.symlink_to(real)
        fsutil.atomic_write(link, '{"a": 1}')
        self.assertTrue(link.is_symlink())
        self.assertEqual(real.read_text(), '{"a": 1}')

    def test_atomic_write_leaves_no_temporary_file_behind(self):
        target = self.root / "t.txt"
        fsutil.atomic_write(target, "one")
        fsutil.atomic_write(target, "two")
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["t.txt"])


class DaemonFilesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(dir="/tmp")
        root = cls.tmp.name
        cls.root = Path(root)
        cls.saved = dict(os.environ)
        os.makedirs(os.path.join(root, "claude", "sessions"))
        os.environ.update(
            ACM_RUNTIME=os.path.join(root, "run"), ACM_DATA=os.path.join(root, "data"), ACM_ALLOW_NO_TTY="1",
            CLAUDE_CONFIG_DIR=os.path.join(root, "claude"), PYTHONPATH=SRC, ACM_CONFIG=os.path.join(root, "none.toml"),
        )
        os.umask(0o022)  # a typical user umask: the daemon must not inherit it
        subprocess.run([sys.executable, "-m", "acm", "daemon", "start"], check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        try:
            client.request("shutdown", autostart=False)
        except Exception:
            pass
        os.environ.clear()
        os.environ.update(cls.saved)
        cls.tmp.cleanup()

    def test_everything_the_daemon_keeps_is_private(self):
        client.request("create_room", name="sec", by="kit")
        client.request("post", room="sec", author="kit", body="private", **{"from": "human"})
        data, run = self.root / "data", self.root / "run"
        self.assertEqual(mode(data), 0o700)
        self.assertEqual(mode(run), 0o700)
        self.assertEqual(mode(run / "acm.sock"), 0o600)
        self.assertEqual(mode(data / "acm.db"), 0o600)
        self.assertEqual(mode(data / "daemon.log"), 0o600)
        for extra in ("acm.db-wal", "acm.db-shm"):
            if (data / extra).exists():
                self.assertEqual(mode(data / extra), 0o600, extra)

    def test_a_closed_rooms_export_is_private(self):
        client.request("create_room", name="sec2", by="kit")
        res = client.request("close_room", name="sec2", by="kit")
        self.assertEqual(mode(res["exported"]), 0o600)
        self.assertEqual(mode(Path(res["exported"]).parent), 0o700)  # created by the daemon, so private too

    def test_the_client_will_not_talk_to_a_daemon_owned_by_someone_else(self):
        with mock.patch("acm.client.os.getuid", return_value=os.getuid() + 1):
            with self.assertRaises(AcmError) as cm:
                client.request("ping", autostart=False)
        self.assertEqual(cm.exception.code, "insecure_socket")
        self.assertEqual(client.request("ping")["ok"], True)  # and nothing was disturbed

    def test_an_insecure_socket_is_not_mistaken_for_no_daemon(self):
        # it must not trigger an autostart, which would then try to bind a second daemon on the same path
        with mock.patch("acm.client.os.getuid", return_value=os.getuid() + 1), mock.patch("acm.client.start_daemon") as start:
            with self.assertRaises(AcmError) as cm:
                client.connect(autostart=True)
        self.assertEqual(cm.exception.code, "insecure_socket")
        start.assert_not_called()


class UnsafeRuntimeDirTest(unittest.TestCase):
    def test_a_runtime_dir_that_is_a_symlink_stops_the_daemon_with_a_clear_error(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            elsewhere = Path(tmp) / "elsewhere"
            elsewhere.mkdir()
            link = Path(tmp) / "run"
            link.symlink_to(elsewhere)
            env = {**os.environ, "ACM_RUNTIME": str(link), "ACM_DATA": str(Path(tmp) / "data"), "PYTHONPATH": SRC}
            started = time.monotonic()
            r = subprocess.run([sys.executable, "-m", "acm", "ls"], env=env, capture_output=True, text=True, timeout=30)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("not a plain directory", r.stderr)
            self.assertLess(time.monotonic() - started, 4)  # immediately, not after the start-up timeout
            self.assertEqual(list(elsewhere.iterdir()), [])  # nothing was placed in the link's target


class SlowWatcherTest(unittest.TestCase):
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
        subprocess.run([sys.executable, "-m", "acm", "daemon", "start"], check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        try:
            client.request("shutdown", autostart=False)
        except Exception:
            pass
        os.environ.clear()
        os.environ.update(cls.saved)
        cls.tmp.cleanup()

    def test_a_watcher_that_never_reads_is_cut_off_and_the_daemon_stays_responsive(self):
        client.request("create_room", name="flood", by="kit")
        s = socket.socket(socket.AF_UNIX)
        s.connect(os.path.join(os.environ["ACM_RUNTIME"], "acm.sock"))
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        s.sendall(b'{"op":"watch_all"}\n')  # and then never read from it
        time.sleep(0.2)
        big = "x" * 20000
        for _ in range(300):
            client.request("post", room="flood", author="kit", body=big, **{"from": "human"})
        s.settimeout(5)
        total = 0
        try:
            while chunk := s.recv(1 << 16):
                total += len(chunk)
                if total > 50_000_000:
                    break
        except (ConnectionResetError, socket.timeout):
            pass
        s.close()
        self.assertEqual(client.request("ping")["ok"], True)
        self.assertLess(total, 50_000_000)  # the daemon stopped feeding it long before everything was sent


class IdentityTest(unittest.TestCase):
    """What an agent inside a Claude session may claim to be. Sessions are stand-in processes with records."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(dir="/tmp")
        root = cls.tmp.name
        cls.root = root
        cls.claude = os.path.join(root, "claude")
        os.makedirs(os.path.join(cls.claude, "sessions"))
        cls.saved = dict(os.environ)
        os.environ.update(
            ACM_RUNTIME=os.path.join(root, "run"), ACM_DATA=os.path.join(root, "data"), ACM_ALLOW_NO_TTY="1",
            CLAUDE_CONFIG_DIR=cls.claude, PYTHONPATH=SRC, ACM_CONFIG=os.path.join(root, "none.toml"),
        )
        subprocess.run([sys.executable, "-m", "acm", "daemon", "start"], check=True, capture_output=True)
        client.request("create_room", name="idroom", by="kit")
        client.request("set_limits", room="idroom", updates={"join_policy": "anyone"})  # these tests are about identity
        cls.procs = []

    @classmethod
    def tearDownClass(cls):
        for p in cls.procs:
            p.kill()
            p.wait()
            for pipe in (p.stdout, p.stderr):
                if pipe:
                    pipe.close()
        try:
            client.request("shutdown", autostart=False)
        except Exception:
            pass
        os.environ.clear()
        os.environ.update(cls.saved)
        cls.tmp.cleanup()

    SESSION = (
        "import json,os,subprocess,sys;"
        "rec=os.path.join(%(claude)r,'sessions',str(os.getpid())+'.json');"
        "open(rec,'w').write(json.dumps({'pid':os.getpid(),'name':%(name)r,'messagingSocketPath':'/sock/'+str(os.getpid())}));"
    )

    def in_session(self, name, code, stay=False):
        """Run `code` (python, with `client` imported) as a child of a stand-in session called `name`."""
        wrapper = self.SESSION % {"claude": self.claude, "name": name} + (
            f"r=subprocess.run([sys.executable,'-c','from acm import client\\n'+{code!r}],capture_output=True,text=True);"
            "sys.stdout.write(r.stdout);sys.stderr.write(r.stderr);" + ("os.execvp('sleep',['sleep','60'])" if stay else "sys.exit(r.returncode)")
        )
        if stay:
            p = subprocess.Popen([sys.executable, "-c", wrapper], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            self.procs.append(p)
            time.sleep(1.0)
            return p
        return subprocess.run([sys.executable, "-c", wrapper], capture_output=True, text=True, timeout=30)

    def post_as(self, session_name, author, body="hi"):
        return self.in_session(session_name, f"client.request('post',room='idroom',author={author!r},body={body!r},**{{'from':'agent'}})")

    def test_an_agent_can_act_as_its_own_session_name(self):
        r = self.post_as("alpha", "alpha", "from alpha")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("from alpha", [m["body"] for m in client.request("tail", room="idroom", n=20)["messages"]])

    def test_an_agent_cannot_post_as_someone_else(self):
        r = self.post_as("alpha", "bravo", "forged")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("this session is alpha; it cannot act as bravo", r.stderr)
        self.assertNotIn("forged", [m["body"] for m in client.request("tail", room="idroom", n=50)["messages"]])

    def test_nor_read_join_or_leave_as_someone_else(self):
        for op, extra in (("read", ""), ("join", ",kind='agent'"), ("leave", ""), ("catch_up", "")):
            r = self.in_session("alpha", f"client.request({op!r},room='idroom',member='bravo'{extra})")
            self.assertNotEqual(r.returncode, 0, op)
            self.assertIn("cannot act as bravo", r.stderr, op)
        self.assertNotIn("bravo", [m["name"] for m in client.request("members", room="idroom")["members"]])

    def test_looking_without_moving_a_read_position_is_not_acting_as_anyone(self):
        r = self.in_session("alpha", "client.request('read',room='idroom',member='kit',peek=True);client.request('read',room='idroom',member='kit',since=0)")
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self.in_session("alpha", "client.request('read',room='idroom',member='kit')")  # but moving kit's position is
        self.assertIn("cannot act as kit", r.stderr)

    def test_a_session_registers_only_itself(self):
        mine = "client.request('register',name='alpha',pid=__import__('os').getppid(),inbox='/sock/'+str(__import__('os').getppid()))"
        r = self.in_session("alpha", mine)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self.in_session("alpha", "client.request('register',name='alpha',pid=1,inbox='/sock/1')")
        self.assertIn("a session can only register itself", r.stderr)
        r = self.in_session("alpha", "client.request('register',name='alpha',pid=__import__('os').getppid(),inbox='/run/user/1000/someone-elses.sock')")
        self.assertIn("a session can only register itself", r.stderr)

    def test_a_name_held_by_a_live_session_cannot_be_taken_but_a_dead_ones_can(self):
        reg = "client.request('register',name='twin',pid=__import__('os').getppid(),inbox='/sock/'+str(__import__('os').getppid()))"
        first = self.in_session("twin", reg, stay=True)  # registers, then stays alive
        self.assertEqual(client.request("budget", room="idroom")["limits"]["style"], "terse")  # daemon fine
        second = self.in_session("twin", reg)
        self.assertNotEqual(second.returncode, 0)
        self.assertIn("already held by another live session", second.stderr)
        first.kill()
        first.wait()
        third = self.in_session("twin", reg)
        self.assertEqual(third.returncode, 0, third.stderr)

    def test_after_a_rename_both_names_work_and_a_third_does_not(self):
        reg = lambda n: f"client.request('register',name={n!r},pid=__import__('os').getppid(),inbox='/sock/'+str(__import__('os').getppid()))"
        code = (
            f"{reg('old-name')};client.request('post',room='idroom',author='old-name',body='before',**{{'from':'agent'}});"
            "import json,os;rec=os.path.join(%r,'sessions',str(os.getppid())+'.json');d=json.load(open(rec));d['name']='new-name';json.dump(d,open(rec,'w'));"
            f"{reg('new-name')};client.request('post',room='idroom',author='new-name',body='after',**{{'from':'agent'}});"
            "client.request('post',room='idroom',author='old-name',body='still me',**{'from':'agent'});"
            "client.request('post',room='idroom',author='stranger',body='no',**{'from':'agent'})"
        ) % self.claude
        r = self.in_session("old-name", code)
        self.assertIn("cannot act as stranger", r.stderr)
        bodies = [m["body"] for m in client.request("tail", room="idroom", n=50)["messages"]]
        for ok in ("before", "after", "still me"):
            self.assertIn(ok, bodies)

    def test_a_process_with_a_sessions_environment_but_no_session_may_not_act_as_an_agent(self):
        env = {**os.environ, "CLAUDECODE": "1"}
        r = subprocess.run(
            [sys.executable, "-c", "from acm import client;client.request('post',room='idroom',author='ghost',body='x',**{'from':'agent'})"],
            env=env, capture_output=True, text=True,
        )
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("cannot be tied to a session", r.stderr)

    def test_callers_outside_any_session_are_not_restricted_by_this(self):
        client.request("join", room="idroom", member="plainagent", kind="agent")  # the harness, as in the other tests
        client.request("post", room="idroom", author="plainagent", body="ok", **{"from": "agent"})



# ---- text that reaches a terminal ------------------------------------------------------------------------------

from acm import fmt, summary, textsafe, tui  # noqa: E402
from acm.db import Store  # noqa: E402

EVIL = "before\x1b]0;pwned\x07\x1b[2J\x9b31m\x00\x7f after ‮evil"


class TextSafeTest(unittest.TestCase):
    def test_control_characters_and_direction_overrides_are_replaced(self):
        out = textsafe.clean(EVIL)
        for bad in ("\x1b", "\x07", "\x9b", "\x00", "\x7f", "‮"):
            self.assertNotIn(bad, out)
        self.assertIn("before", out)
        self.assertIn("after", out)

    def test_ordinary_text_is_untouched(self):
        for text in ("plain", "line one\nline two", "café 日本語 🙂", "family 👨‍👩‍👧 zwj", "é"):
            self.assertEqual(textsafe.clean(text), text)
        self.assertEqual(textsafe.clean("a\tb"), "a b")
        self.assertEqual(textsafe.one_line("a\nb"), "a b")

    def test_nothing_escape_shaped_is_stored(self):
        s = Store(":memory:")
        self.addCleanup(s.close)
        s.create_room("r", "kit", "topic\x1b[31m")
        m = s.post("r", "kit", EVIL, refs=["path\x1b[1m"], from_kind="human")
        self.assertNotIn("\x1b", m["body"] + m["refs"][0] + s.get_room("r")["topic"])
        self.assertNotIn("\x1b", "".join(x["body"] for x in s.tail("r", 10)))

    def test_rows_stored_before_cleaning_existed_are_cleaned_on_display(self):
        s = Store(":memory:")
        self.addCleanup(s.close)
        s.create_room("r", "kit")
        s.conn.execute("INSERT INTO messages (room_id, author, kind, from_kind, body, ts) VALUES (1, 'old', 'post', 'agent', ?, 1)", (EVIL,))
        raw = s.tail("r", 1)[0]
        self.assertIn("\x1b", raw["body"])  # as stored long ago
        self.assertNotIn("\x1b", fmt.message(raw, color=False))
        data = summary.build(s, "r")
        self.assertNotIn("\x1b", summary.render(data) + summary.transcript(s, "r"))

    def test_the_terminal_client_never_draws_them(self):
        from tests.test_tui_view import FakeModel, msg, room

        m = FakeModel([room("r", messages=[msg(1, "arx", EVIL, refs=["x\x1b[1m"])])])
        m.selected = "r"
        view = tui.View(m)
        canvas = tui.FakeCanvas(24, 100)
        view.draw(canvas, 24, 100)
        self.assertNotIn("\x1b", canvas.text())
        self.assertIn("before", canvas.text())

    def test_the_command_line_never_prints_them(self):
        tmp = tempfile.mkdtemp(dir="/tmp")
        self.addCleanup(shutil.rmtree, tmp, True)  # registered first, so it runs last: after the daemon is stopped
        os.makedirs(os.path.join(tmp, "claude", "sessions"))
        env = {**os.environ, "ACM_RUNTIME": os.path.join(tmp, "run"), "ACM_DATA": os.path.join(tmp, "data"),
               "ACM_ALLOW_NO_TTY": "1", "CLAUDE_CONFIG_DIR": os.path.join(tmp, "claude"), "PYTHONPATH": SRC,
               "ACM_CONFIG": os.path.join(tmp, "n.toml")}
        run = lambda *a, stdin=None: subprocess.run([sys.executable, "-m", "acm", "--as", "kit", *a], env=env, capture_output=True, text=True, input=stdin)
        self.addCleanup(lambda: run("daemon", "stop"))
        self.assertEqual(run("new", "evil", "-t", "topic\x1b]0;x\x07").returncode, 0)
        self.assertEqual(run("post", "evil", stdin=EVIL).returncode, 0)  # a NUL cannot travel in an argument
        for args in (("read", "evil", "--since", "0"), ("tail", "evil"), ("summary", "evil"), ("export", "evil"),
                     ("search", "before"), ("ls",), ("close", "evil")):
            r = run(*args)
            self.assertNotIn("\x1b", r.stdout, args)
        with open(os.path.join(tmp, "data", "rooms", "evil.md")) as f:
            self.assertNotIn("\x1b", f.read())


# ---- who may join -------------------------------------------------------------------------------------------------


class JoinPolicyTest(IdentityTest):
    """Rooms are invited-only by default: an agent that was not added cannot use a room, read it or see it."""

    def room(self, name, **limits):
        client.request("create_room", name=name, by="kit", topic=f"about {name}")
        if limits:
            client.request("set_limits", room=name, updates=limits)
        return name

    def as_agent(self, code, name="alpha"):
        return self.in_session(name, code)

    def test_the_default_is_invited_only(self):
        self.assertEqual(client.request("budget", room=self.room("plain"))["limits"]["join_policy"], "invited")

    def test_an_agent_that_was_not_added_cannot_join_post_or_catch_up(self):
        self.room("private")
        for op, extra in (("join", ",kind='agent'"), ("catch_up", "")):  # asking to join gets the helpful answer
            r = self.as_agent(f"client.request({op!r},room='private',member='alpha'{extra})")
            self.assertNotEqual(r.returncode, 0, op)
            self.assertIn("only takes agents a human has added", r.stderr, op)
        r = self.as_agent("client.request('post',room='private',author='alpha',body='let me in',**{'from':'agent'})")
        self.assertIn("only takes agents a human has added", r.stderr)
        self.assertNotIn("alpha", [m["name"] for m in client.request("members", room="private")["members"]])

    def test_it_cannot_read_about_the_room_either_and_the_room_looks_like_it_does_not_exist(self):
        self.room("secret")
        client.request("post", room="secret", author="kit", body="the plan is to...", **{"from": "human"})
        for op, extra in (("tail", ",n=5"), ("members", ""), ("summary", ""), ("export", ""), ("budget", ""),
                          ("read", ",member='alpha'"), ("read", ",member='alpha',peek=True"), ("read", ",member='alpha',since=0"),
                          ("wake_preview", ",author='alpha',body='x'")):
            r = self.as_agent(f"client.request({op!r},room='secret'{extra})")
            self.assertNotEqual(r.returncode, 0, op)
            self.assertIn("no such room: secret", r.stderr, op)
            self.assertNotIn("the plan", r.stdout + r.stderr, op)
        r = self.as_agent("client.request('get_room',name='secret')")
        self.assertIn("no such room: secret", r.stderr)
        # the same answer as for a room that is not there at all, so its existence is not revealed
        hidden = self.as_agent("client.request('tail',room='secret',n=5)").stderr.strip().splitlines()[-1]
        absent = self.as_agent("client.request('tail',room='no-such-room-at-all',n=5)").stderr.strip().splitlines()[-1]
        self.assertEqual(hidden.replace("secret", "X"), absent.replace("no-such-room-at-all", "X"))

    def test_lists_and_searches_leave_it_out(self):
        self.room("hidden-one")
        self.room("open-one", join_policy="anyone")
        client.request("post", room="hidden-one", author="kit", body="needle in the hidden room", **{"from": "human"})
        client.request("post", room="open-one", author="kit", body="needle in the open room", **{"from": "human"})
        r = self.as_agent("import json;print(json.dumps([x['name'] for x in client.request('list_rooms',status='open',member='alpha')['rooms']]))")
        self.assertIn("open-one", r.stdout)
        self.assertNotIn("hidden-one", r.stdout)
        r = self.as_agent("import json;print(json.dumps([m['room'] for m in client.request('search',query='needle')['matches']]))")
        self.assertIn("open-one", r.stdout)
        self.assertNotIn("hidden-one", r.stdout)
        humans = client.request("list_rooms", status="open")["rooms"]  # people still see everything
        self.assertTrue({"hidden-one", "open-one"} <= {x["name"] for x in humans})
        self.assertEqual(len(client.request("search", query="needle")["matches"]), 2)

    def test_the_event_stream_shows_an_agent_only_what_it_may_see(self):
        self.room("quiet-room")
        self.room("loud-room", join_policy="anyone")
        code = (
            "from acm import client\n"
            "w = client.watch()\n"
            "w._sock.settimeout(3)\n"
            "print('ready', flush=True)\n"
            "try:\n"
            "    for ev in w:\n"
            "        print(ev['event'], ev.get('room_name'), flush=True)\n"
            "except Exception as e:\n"
            "    print('end', type(e).__name__)\n"
        )
        wrapper = self.SESSION % {"claude": self.claude, "name": "watcher"} + f"subprocess.run([sys.executable, '-c', {code!r}])"
        p = subprocess.Popen([sys.executable, "-c", wrapper], stdout=subprocess.PIPE, text=True)
        self.procs.append(p)
        self.assertEqual(p.stdout.readline().strip(), "ready")
        client.request("post", room="quiet-room", author="kit", body="secret chatter", **{"from": "human"})
        client.request("post", room="loud-room", author="kit", body="public chatter", **{"from": "human"})
        seen = p.stdout.read()
        p.wait(timeout=10)
        self.assertIn("message loud-room", seen)
        self.assertNotIn("quiet-room", seen)

    def test_a_single_room_stream_is_refused_too(self):
        self.room("no-watching")
        r = self.as_agent("client.watch('no-watching')")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("no such room: no-watching", r.stderr)

    def test_once_added_the_agent_can_see_and_act(self):
        self.room("members-only")
        client.request("join", room="members-only", member="alpha", kind="agent")  # what a human's /add does
        client.request("post", room="members-only", author="kit", body="welcome", **{"from": "human"})
        r = self.as_agent("print([m['body'] for m in client.request('tail',room='members-only',n=5)['messages']])")
        self.assertIn("welcome", r.stdout)
        r = self.as_agent("client.request('post',room='members-only',author='alpha',body='thanks for adding me',**{'from':'agent'})")
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self.as_agent("print(len(client.request('list_rooms',status='open',member='alpha')['rooms']))")
        self.assertGreaterEqual(int(r.stdout.strip()), 1)

    def test_an_earlier_name_of_a_renamed_session_keeps_its_access(self):
        self.room("rename-room")
        client.request("join", room="rename-room", member="old-me", kind="agent")
        reg = lambda n: f"client.request('register',name={n!r},pid=__import__('os').getppid(),inbox='/sock/'+str(__import__('os').getppid()))"
        code = (
            f"{reg('old-me')};"
            "import json,os;rec=os.path.join(%r,'sessions',str(os.getppid())+'.json');d=json.load(open(rec));d['name']='new-me';json.dump(d,open(rec,'w'));"
            f"{reg('new-me')};print(len(client.request('tail',room='rename-room',n=5)['messages'])>=0)"
        ) % self.claude
        r = self.in_session("old-me", code)
        self.assertEqual(r.returncode, 0, r.stderr)  # it was added under its old name, and still is that session

    def test_open_to_anyone_still_works_when_a_room_asks_for_it(self):
        self.room("everyone", join_policy="anyone")
        r = self.as_agent("client.request('catch_up',room='everyone',member='alpha',keep=5,kind='agent')")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_a_process_with_a_sessions_environment_but_no_session_sees_only_open_rooms(self):
        self.room("closed-door")
        self.room("open-door", join_policy="anyone")
        env = {**os.environ, "CLAUDECODE": "1"}
        run = lambda code: subprocess.run([sys.executable, "-c", f"from acm import client\n{code}"], env=env, capture_output=True, text=True)
        self.assertIn("no such room", run("client.request('tail',room='closed-door',n=1)").stderr)
        self.assertEqual(run("client.request('tail',room='open-door',n=1)").returncode, 0)

    def test_the_setting_is_validated(self):
        self.room("v")
        with self.assertRaises(AcmError):
            client.request("set_limits", room="v", updates={"join_policy": "everyone"})


# ---- files acm writes ------------------------------------------------------------------------------------------------


class WriterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.victim = self.root / "victim.txt"
        self.victim.write_text("precious")
        self.claude = self.root / "claude"
        self.claude.mkdir()
        self.env = {**os.environ, "CLAUDE_CONFIG_DIR": str(self.claude), "ACM_CONFIG": str(self.root / "cfg" / "config.toml"),
                    "ACM_LIMITS_FILE": str(self.root / "limits.json"), "ACM_RUNTIME": str(self.root / "run"),
                    "ACM_DATA": str(self.root / "data"), "ACM_OMARCHY_USAGE_CACHE": "", "PYTHONPATH": SRC, "ACM_ALLOW_NO_TTY": "1"}

    def plant(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        os.symlink(self.victim, path)

    def acm(self, *args, stdin=""):
        return subprocess.run([sys.executable, "-m", "acm", *args], env=self.env, capture_output=True, text=True, input=stdin)

    def test_the_usage_limit_tap_does_not_write_through_a_planted_link(self):
        for name in ("limits.json.tmp", "limits.tmp", ".limits.json.tmp"):
            self.plant(self.root / name)
        raw = '{"rate_limits":{"five_hour":{"used_percentage":5,"resets_at":%d}}}' % (time.time() + 3600)
        self.assertEqual(self.acm("limits-tap", stdin=raw).returncode, 0)
        self.assertEqual(self.victim.read_text(), "precious")
        self.assertIn("5-hour: 5% used", self.acm("limits").stdout)

    def test_the_first_use_config_does_not_write_through_a_planted_link(self):
        for name in ("config.tmp", "config.toml.tmp"):
            self.plant(self.root / "cfg" / name)
        self.assertEqual(self.acm("config", "init").returncode, 0)
        self.assertEqual(self.victim.read_text(), "precious")
        self.assertTrue((self.root / "cfg" / "config.toml").read_text().startswith("# acm configuration."))

    def test_the_status_line_installer_does_not_write_through_planted_links(self):
        (self.claude / "settings.json").write_text('{"model": "x"}')
        for name in ("settings.acm-tmp", "settings.json.tmp"):
            self.plant(self.claude / name)
        self.assertEqual(self.acm("statusline", "install", "-y").returncode, 0)
        self.assertEqual(self.victim.read_text(), "precious")

    def test_the_installer_refuses_if_its_backup_name_is_a_link(self):
        (self.claude / "settings.json").write_text('{"model": "x"}')
        self.plant(self.claude / "settings.json.acm-backup")
        r = self.acm("statusline", "install", "-y")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("not a plain file", r.stderr)
        self.assertEqual(self.victim.read_text(), "precious")
        self.assertEqual((self.claude / "settings.json").read_text(), '{"model": "x"}')  # and changed nothing

    def test_closing_a_room_never_overwrites_a_file_that_is_not_its_own(self):
        notes = self.root / "notes"
        notes.mkdir()
        (notes / "billing.md").write_text("my own notes")
        (notes / "billing-1.md").write_text("more of my notes")
        r = self.acm("new", "billing", "-L", f"export_dir={notes}")
        self.assertEqual(r.returncode, 0, r.stderr)
        out = self.acm("close", "billing").stdout
        self.assertEqual((notes / "billing.md").read_text(), "my own notes")
        self.assertEqual((notes / "billing-1.md").read_text(), "more of my notes")
        self.assertIn(f"saved to {notes / 'billing-2.md'}", out)
        self.assertTrue((notes / "billing-2.md").read_text().startswith("<!-- acm-room: billing -->"))
        self.acm("daemon", "stop")

    def test_a_file_acm_wrote_for_the_same_room_is_replaced(self):
        mine = fsutil.unclaimed_path(self.root / "x.md", marker="<!-- acm-room: x -->")
        self.assertEqual(mine, self.root / "x.md")
        (self.root / "x.md").write_text("<!-- acm-room: x -->\nold")
        self.assertEqual(fsutil.unclaimed_path(self.root / "x.md", marker="<!-- acm-room: x -->"), self.root / "x.md")
        self.assertEqual(fsutil.unclaimed_path(self.root / "x.md", marker="<!-- acm-room: other -->"), self.root / "x-1.md")


# ---- what a request or a repository can make the daemon do -------------------------------------------------------------


class ResourceTest(unittest.TestCase):
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
        subprocess.run([sys.executable, "-m", "acm", "daemon", "start"], check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        try:
            client.request("shutdown", autostart=False)
        except Exception:
            pass
        os.environ.clear()
        os.environ.update(cls.saved)
        cls.tmp.cleanup()

    def test_a_request_line_over_the_limit_is_refused_and_the_daemon_carries_on(self):
        s = socket.socket(socket.AF_UNIX)
        s.connect(os.path.join(os.environ["ACM_RUNTIME"], "acm.sock"))
        s.settimeout(10)
        chunk = b"x" * (1 << 20)
        try:
            for _ in range(10):  # 10 MB with no newline
                s.sendall(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        reply = b""
        try:
            while not reply.endswith(b"\n"):
                data = s.recv(4096)
                if not data:
                    break
                reply += data
        except (ConnectionResetError, socket.timeout):
            pass
        s.close()
        self.assertIn(b"too_large", reply)
        self.assertEqual(client.request("ping")["ok"], True)

    def test_git_output_is_bounded_and_the_number_of_lookups_is_capped(self):
        repo = Path(os.environ["ACM_DATA"]).parent / "repo"
        repo.mkdir()
        git = lambda *a: subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *a], capture_output=True, text=True, check=True)
        git("init", "-q")
        (repo / "f").write_text("x")
        git("add", ".")
        git("commit", "-q", "-m", "s" * 5000 + "\x1b[31m")
        sha = git("rev-parse", "--short", "HEAD").stdout.strip()
        note = summary._ref_note(sha, str(repo))
        self.assertLessEqual(len(note), summary.SUBJECT_BYTES + 5)
        self.assertNotIn("\x1b", note)
        data = {"project_dir": str(repo), "refs": [(f"{i:07x}", "kit") for i in range(100)]}
        with mock.patch.object(summary, "_ref_note", return_value="") as looked_up:
            summary.with_ref_notes(data)
        self.assertEqual(looked_up.call_count, summary.MAX_REFS_CHECKED)


if __name__ == "__main__":
    unittest.main()
