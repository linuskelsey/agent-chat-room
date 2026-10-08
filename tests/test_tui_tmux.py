import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

SRC = os.path.join(os.path.dirname(__file__), "..", "src")


@unittest.skipUnless(shutil.which("tmux"), "needs tmux")
class TmuxUITest(unittest.TestCase):
    """The real curses client, running in a detached tmux pane that the test types into and reads back."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(dir="/tmp")
        root = cls.tmp.name
        cls.saved = dict(os.environ)
        os.makedirs(os.path.join(root, "claude", "sessions"))
        os.environ.update(
            ACM_RUNTIME=os.path.join(root, "run"), ACM_DATA=os.path.join(root, "data"), ACM_ALLOW_NO_TTY="1",
            CLAUDE_CONFIG_DIR=os.path.join(root, "claude"), PYTHONPATH=SRC, ACM_CONFIG=os.path.join(root, "none.toml"),
            LC_ALL="C.UTF-8",
        )
        from acm import client
        cls.client = client
        subprocess.run([sys.executable, "-m", "acm", "daemon", "start"], check=True, capture_output=True)
        cls.server = f"acmtest{os.getpid()}"

    @classmethod
    def tearDownClass(cls):
        subprocess.run(["tmux", "-L", cls.server, "kill-server"], capture_output=True)
        try:
            cls.client.request("shutdown", autostart=False)
        except Exception:
            pass
        os.environ.clear()
        os.environ.update(cls.saved)
        cls.tmp.cleanup()

    # -- driving tmux ----------------------------------------------------

    def tmux(self, *args):
        return subprocess.run(["tmux", "-L", self.server, "-f", "/dev/null", *args], capture_output=True, text=True)

    def start(self, name="ui", cols=110, rows=30):
        self.session = name
        self.tmux("new-session", "-d", "-s", name, "-x", str(cols), "-y", str(rows),
                  f"{sys.executable} -m acm --as kit ui; sleep 30")
        self.addCleanup(lambda: self.tmux("kill-session", "-t", name))
        self.wait_for("acm · kit")

    def screen(self):
        return self.tmux("capture-pane", "-p", "-t", self.session).stdout

    def send(self, *keys, literal=False):
        self.tmux("send-keys", "-t", self.session, *(["-l"] if literal else []), *keys)
        time.sleep(0.25)

    def typed(self, text):
        self.send(text, literal=True)

    def wait_for(self, text, timeout=5):
        end = time.time() + timeout
        while time.time() < end:
            if text in self.screen():
                return
            time.sleep(0.1)
        self.fail(f"never saw {text!r} on screen:\n{self.screen()}")

    def gone(self, text, timeout=5):
        end = time.time() + timeout
        while time.time() < end:
            if text not in self.screen():
                return
            time.sleep(0.1)
        self.fail(f"{text!r} stayed on screen:\n{self.screen()}")

    def room(self, name, topic="", by="kit"):
        self.client.request("create_room", name=name, by=by, topic=topic)
        return name

    def say(self, room, author, body, kind="agent"):
        self.client.request("post", room=room, author=author, body=body, **{"from": kind})

    # -- tests -----------------------------------------------------------

    def test_01_shows_conversations_and_the_selected_one(self):
        self.room("alpha", "first topic")
        self.say("alpha", "arx", "hello from arx")
        self.room("beta")
        self.start("t01")
        screen = self.screen()
        self.assertIn("alpha", screen)
        self.assertIn("beta", screen)
        self.wait_for("alpha · first topic")
        self.assertIn("arx: hello from arx", screen)
        self.assertIn("[F2 fyi: off]", screen)

    def test_02_typing_and_enter_sends_a_message(self):
        self.room("gamma")
        self.start("t02")
        self.wait_for("gamma")
        self.send("Enter")  # list -> message box
        self.typed("hello there")
        self.send("Enter")
        self.wait_for("you: hello there")
        msgs = self.client.request("tail", room="gamma", n=5)["messages"]
        self.assertEqual([(m["author"], m["from"], m["body"]) for m in msgs if m["kind"] == "post"], [("kit", "human", "hello there")])

    def test_03_messages_from_others_appear_live_and_unread_badges_track_other_rooms(self):
        self.room("delta")
        self.room("epsilon")
        self.say("delta", "arx", "old news")
        self.start("t03")
        self.wait_for("delta")
        self.say("epsilon", "arx", "psst from another room")
        self.wait_for("● epsilon")  # unread marker without any key pressed
        names = [l[:27] for l in self.screen().splitlines()]  # the list column only
        self.assertLess(next(i for i, l in enumerate(names) if "epsilon" in l), next(i for i, l in enumerate(names) if "delta" in l))
        self.send("Up")  # move to epsilon, now above the room I was reading
        self.wait_for("psst from another room")
        self.gone("● epsilon")  # reading it clears the badge
        self.say("epsilon", "arx", "live line")
        self.wait_for("live line")

    def test_04_f1_opens_help_and_any_key_closes_it(self):
        self.room("zeta")
        self.start("t04")
        self.send("F1")
        self.wait_for("Message box")
        self.send("x")
        self.gone("Message box")

    def test_05_toggles_and_the_fyi_flag_reach_the_daemon(self):
        self.room("eta")
        self.start("t05")
        self.wait_for("eta")
        self.send("Enter")
        self.send("F2")
        self.wait_for("[F2 fyi: ON ]")
        self.typed("quiet note")
        self.send("Enter")
        self.wait_for("quiet note")
        self.wait_for("[F2 fyi: off]")  # one-shot
        posts = [m for m in self.client.request("tail", room="eta", n=5)["messages"] if m["kind"] == "post"]
        self.assertTrue(posts[-1]["no_reply_needed"])
        self.send("F3")
        self.typed("we pick sqlite")
        self.send("Enter")
        self.wait_for("★ kit: we pick sqlite")  # pinned decision block
        self.assertEqual(self.client.request("tail", room="eta", n=5)["messages"][-1]["kind"], "decision")

    def test_06_new_creates_and_selects_a_room(self):
        self.room("theta")
        self.start("t06")
        self.wait_for("theta")
        self.send("Enter")
        self.typed("/new fresh-room a brand new one")
        self.send("Enter")
        self.wait_for("fresh-room · a brand new one")
        self.assertEqual(self.client.request("get_room", name="fresh-room")["room"]["topic"], "a brand new one")

    def test_07_close_asks_and_the_summary_is_shown(self):
        self.room("iota")
        self.say("iota", "arx", "use sqlite", kind="agent")
        self.client.request("post", room="iota", author="arx", body="pin", kind="decision", **{"from": "agent"})
        self.start("t07")
        self.wait_for("iota")
        self.send("Enter")
        self.typed("/close")
        self.send("Enter")
        self.wait_for("close iota? this cannot be undone")
        self.send("n")
        self.assertEqual(self.client.request("get_room", name="iota")["room"]["status"], "open")
        self.typed("/close")
        self.send("Enter")
        self.wait_for("close iota?")
        self.send("y")
        self.wait_for("Closed iota")
        self.wait_for("1. (arx) pin")
        self.send("x")
        self.wait_for("read-only")

    def test_08_narrow_terminals_show_one_pane_and_q_quits(self):
        self.room("kappa")
        self.start("t08", cols=50, rows=20)
        self.assertIn("kappa", self.screen())
        self.send("Enter")
        self.wait_for("[F2 fyi")
        self.assertNotIn("acm · kit", self.screen())
        self.send("Escape")
        self.wait_for("acm · kit")
        self.send("q")
        end = time.time() + 5
        while time.time() < end and self.tmux("has-session", "-t", "t08").returncode == 0 and "acm · kit" in self.screen():
            time.sleep(0.1)
        self.assertNotIn("acm · kit", self.screen())

    def test_09_wide_characters_and_long_lines_do_not_break_the_layout(self):
        self.room("lambda")
        self.say("lambda", "arx", "日本語のメッセージ " * 12)
        self.say("lambda", "arx", "x" * 300)
        self.start("t09")
        self.wait_for("lambda")
        for line in self.screen().splitlines():
            self.assertLessEqual(len(line), 110)
        self.assertIn("日本語", self.screen())
        self.assertIn("│", self.screen())

    def test_11_plain_acm_in_a_terminal_opens_the_client(self):
        self.room("nu")
        self.session = "t11"
        self.tmux("new-session", "-d", "-s", "t11", "-x", "100", "-y", "24", f"ACM_NAME=kit {sys.executable} -m acm; sleep 30")
        self.addCleanup(lambda: self.tmux("kill-session", "-t", "t11"))
        self.wait_for("acm · kit")
        self.wait_for("nu")

    def test_12_each_conversation_keeps_its_own_half_typed_message(self):
        self.room("xi")
        self.room("omicron")
        self.start("t12")
        self.wait_for("xi")
        self.send("Enter")
        self.typed("draft for the first")
        self.send("C-n")
        self.wait_for("[F2 fyi")
        self.assertNotIn("draft for the first", self.screen().split("│", 1)[1] if "│" in self.screen() else "")
        self.typed("draft for the second")
        self.send("C-p")
        self.wait_for("draft for the first")
        self.assertNotIn("draft for the second", self.screen())

    def test_13_alt_q_does_not_quit(self):
        self.room("pi")
        self.start("t13")
        self.tmux("send-keys", "-t", "t13", "Escape", "q")  # both at once, as an Alt-q arrives
        time.sleep(0.5)
        self.assertIn("acm · kit", self.screen())
        self.send("q")  # a q on its own still quits
        end = time.time() + 5
        while time.time() < end and "acm · kit" in self.screen():
            time.sleep(0.1)
        self.assertNotIn("acm · kit", self.screen())

    def test_14_n_starts_a_new_conversation_and_help_is_readable(self):
        self.room("rho")
        self.start("t14", cols=120, rows=36)
        self.wait_for("rho")
        self.send("F1")
        self.wait_for("Commands (type them in the message box)")
        screen = self.screen()
        self.assertIn("/wrapup AGENT", screen)
        self.assertIn("┌", screen)
        for line in screen.splitlines():
            self.assertLessEqual(len(line), 120)
        self.send("x")
        self.gone("Commands (type")
        self.send("n")
        self.wait_for("new conversation (NAME [topic]): ")
        self.typed("sigma the sigma topic")
        self.send("Enter")
        self.wait_for("sigma · the sigma topic")
        self.wait_for("created sigma")
        self.assertEqual(self.client.request("get_room", name="sigma")["room"]["topic"], "the sigma topic")
        self.typed("hello in the new one")  # the message box is ready to type into
        self.send("Enter")
        self.wait_for("you: hello in the new one")

    def test_15_escape_and_enter_respond_at_once(self):
        self.room("tau")
        self.start("t15")
        self.wait_for("tau")
        worst = 0.0
        for key, marker in (("Enter", "Esc back to list"), ("Escape", "n new"), ("Enter", "Esc back to list"), ("Escape", "n new")):
            self.send_nowait(key)
            started = time.monotonic()
            self.wait_for(marker, timeout=3)
            worst = max(worst, time.monotonic() - started)
            time.sleep(0.2)
        self.assertLess(worst, 0.4, f"a key took {worst:.2f}s to show its effect")

    def test_16_a_timing_log_can_be_switched_on(self):
        self.room("upsilon")
        log = os.path.join(self.tmp.name, "ui.log")
        self.session = "t16"
        self.tmux("new-session", "-d", "-s", "t16", "-x", "100", "-y", "24",
                  f"ACM_UI_LOG={log} {sys.executable} -m acm --as kit ui; sleep 30")
        self.addCleanup(lambda: self.tmux("kill-session", "-t", "t16"))
        self.wait_for("acm · kit")
        self.send("Enter")
        self.send("Escape")
        self.send("q")
        end = time.time() + 5
        while time.time() < end and "key 'q'" not in (open(log).read() if os.path.exists(log) else ""):
            time.sleep(0.1)
        with open(log) as f:
            text = f.read()
        self.assertIn("request list_rooms", text)
        self.assertIn("redraw", text)
        self.assertIn("-> ENTER", text)
        self.assertIn("-> ESC", text)

    def send_nowait(self, *keys):
        self.tmux("send-keys", "-t", self.session, *keys)

    def test_10_the_client_survives_a_daemon_restart(self):
        self.room("mu")
        self.start("t10")
        self.wait_for("mu")
        self.client.request("shutdown")
        time.sleep(0.5)
        subprocess.run([sys.executable, "-m", "acm", "daemon", "start"], check=True, capture_output=True)
        self.wait_for("reconnected", timeout=10)
        self.say("mu", "arx", "back again")
        self.wait_for("back again")


if __name__ == "__main__":
    unittest.main()
