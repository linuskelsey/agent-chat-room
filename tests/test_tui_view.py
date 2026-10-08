import os
import subprocess
import sys
import tempfile
import time
import unittest

from acm import tui, tui_model
from acm.errors import AcmError

SRC = os.path.join(os.path.dirname(__file__), "..", "src")


class HelperTest(unittest.TestCase):
    def test_widths(self):
        self.assertEqual(tui.text_width("abc"), 3)
        self.assertEqual(tui.text_width("日本"), 4)  # wide characters take two cells
        self.assertEqual(tui.text_width("é"), 1)  # a combining accent takes none

    def test_clip(self):
        self.assertEqual(tui.clip("hello world", 20), "hello world")
        self.assertEqual(tui.clip("hello world", 8), "hello w…")
        self.assertEqual(tui.clip("hello world", 8, ellipsis=False), "hello wo")
        self.assertEqual(tui.clip("日本語日本語", 5), "日本…")
        self.assertEqual(tui.clip("abc", 0), "")

    def test_wrap(self):
        self.assertEqual(tui.wrap("the quick brown fox", 10), ["the quick", "brown fox"])
        self.assertEqual(tui.wrap("a\nb", 10), ["a", "b"])  # explicit newlines survive
        self.assertEqual(tui.wrap("abcdefghijkl", 5), ["abcde", "fghij", "kl"])  # a long word is split
        for line in tui.wrap("日本語の長い文章がここに入ります " * 3, 12):
            self.assertLessEqual(tui.text_width(line), 12)
        self.assertEqual(tui.wrap("", 10), [""])


class FakeModel(tui_model.Model):
    """A model with rooms set up by hand, so drawing is tested without a daemon."""

    def __init__(self, rooms, me="kit"):
        super().__init__(me, request=self._no_requests)
        self.rooms = {r.name: r for r in rooms}

    @staticmethod
    def _no_requests(op, **kw):
        raise AssertionError(f"unexpected request {op}")


def msg(i, author, body, kind="post", frm="agent", refs=(), fyi=False, ts=None):
    return {"id": i, "author": author, "body": body, "kind": kind, "from": frm, "refs": list(refs), "no_reply_needed": fyi,
            "ts": ts or 1_800_000_000 + i * 60, "mentions": []}


def room(name, topic="", status="open", unread=0, messages=(), decisions=(), joined=True, last_ts=1_800_000_000):
    r = tui_model.Room(name, topic=topic, status=status, unread=unread, joined=joined, last_ts=last_ts, created_at=1_799_990_000)
    r.messages, r.decisions, r.loaded, r.exhausted = list(messages), list(decisions), True, True
    r.members = {"kit": {"name": "kit"}, "arx": {"name": "arx"}}
    return r


class ViewTest(unittest.TestCase):
    def render(self, model, rows=24, cols=100, view=None):
        view = view or tui.View(model)
        canvas = tui.FakeCanvas(rows, cols)
        view.draw(canvas, rows, cols)
        return view, canvas

    def model(self):
        m = FakeModel([
            room("login-redesign", "new login", unread=3, last_ts=1_800_000_500,
                 messages=[msg(1, "kit", "hello @arx", frm="human"), msg(2, "arx", "on it", refs=["src/login.py"]),
                           msg(3, "system", "arx joined", kind="system", frm="system"),
                           msg(4, "arx", "use passkeys", kind="decision"), msg(5, "arx", "quiet note", fyi=True)],
                 decisions=[msg(4, "arx", "use passkeys", kind="decision")]),
            room("billing", unread=0, last_ts=1_800_000_100),
            room("old-one", status="closed", last_ts=1_799_999_000),
        ])
        m.selected = "login-redesign"
        return m

    def test_wide_layout_shows_the_list_and_the_conversation(self):
        view, c = self.render(self.model())
        text = c.text()
        lines = c.lines()
        self.assertIn("acm · kit", lines[0])
        self.assertRegex(text, r"● login-redesign\s+3")  # unread badge, right-aligned
        self.assertIn("billing", text)
        order = [i for i, l in enumerate(lines) if "login-redesign" in l[:30]] + [i for i, l in enumerate(lines) if "billing" in l[:30]]
        self.assertLess(order[0], order[-1])  # busiest conversation first
        self.assertTrue(any(l.strip().startswith("closed") or " closed" in l[:30] for l in lines))
        self.assertLess(next(i for i, l in enumerate(lines) if "closed" in l[:30]), next(i for i, l in enumerate(lines) if "old-one" in l[:30]))
        self.assertEqual(c.styles[(1, 5)], "selected")  # the selected conversation is highlighted
        self.assertIn("login-redesign · new login", text)
        self.assertIn("★ arx: use passkeys", text)  # pinned decision block
        self.assertIn("kit", text)
        self.assertIn(": hello @arx", text)
        self.assertIn("↳ src/login.py", text)
        self.assertIn("* arx joined", text)
        self.assertIn("✎fyi", text)
        self.assertIn("[F2 fyi: off]", text)
        self.assertIn("[F3 decision: off]", text)
        self.assertIn("3 unread", lines[-1])

    def test_my_own_messages_say_you_and_other_humans_are_marked(self):
        m = FakeModel([room("r", messages=[msg(1, "kit", "mine", frm="human"), msg(2, "bob", "theirs", frm="human")])])
        m.selected = "r"
        _, c = self.render(m)
        text = c.text()
        self.assertIn("you: mine", text)
        self.assertIn("bob (human): theirs", text)

    def test_a_waiting_agent_shows_in_the_list_and_the_title(self):
        m = self.model()
        m.rooms["login-redesign"].waiting = {"arx"}
        _, c = self.render(m)
        text, lines = c.text(), c.lines()
        self.assertIn("⚠ arx waiting for you", lines[0])
        self.assertRegex(lines[1], r"^ !\s+login-redesign")  # the list marks the room too, ahead of the unread dot
        m.rooms["login-redesign"].waiting = set()
        _, c = self.render(m)
        self.assertNotIn("waiting for you", c.text())

    def test_closed_rooms_are_read_only(self):
        m = FakeModel([room("done", status="closed", messages=[msg(1, "arx", "bye")])])
        m.selected = "done"
        _, c = self.render(m)
        self.assertIn("read-only", c.text())
        self.assertNotIn("[F2", c.text())
        self.assertIn("closed", c.lines()[0])

    def test_empty_state_explains_how_to_start(self):
        _, c = self.render(FakeModel([]))
        self.assertIn("no rooms yet", c.text())
        self.assertIn("/new NAME", c.text())

    def test_narrow_screens_show_one_pane_at_a_time(self):
        m = self.model()
        view = tui.View(m)
        view.focus = "list"
        _, c = self.render(m, cols=50, view=view)
        self.assertIn("billing", c.text())
        self.assertNotIn("hello @arx", c.text())
        view.focus = "input"
        _, c = self.render(m, cols=50, view=view)
        self.assertIn("hello @arx", c.text())
        self.assertNotIn("billing", c.text())

    def test_long_conversations_scroll_and_say_how_much_is_below(self):
        msgs = [msg(i, "arx", f"line number {i}") for i in range(1, 61)]
        m = FakeModel([room("long", messages=msgs)])
        m.selected = "long"
        view, c = self.render(m, rows=20)
        self.assertIn("line number 60", c.text())
        self.assertNotIn("line number 1:", c.text())
        view.scroll["long"] = 10
        _, c = self.render(m, rows=20, view=view)
        self.assertNotIn("line number 60", c.text())
        self.assertIn("↓ 10 lines below", c.text())

    def test_decisions_can_be_hidden_and_overflow_is_counted(self):
        decisions = [msg(i, "arx", f"decision {i}", kind="decision") for i in range(1, 6)]
        m = FakeModel([room("d", decisions=decisions, messages=decisions)])
        m.selected = "d"
        view, c = self.render(m)
        self.assertIn("decision 5", c.text())
        self.assertIn("+2 earlier decisions", c.text())
        view.show_decisions = False
        _, c = self.render(m, view=view)
        self.assertNotIn("+2 earlier", c.text())

    def test_long_lines_wrap_with_a_hanging_indent(self):
        m = FakeModel([room("w", messages=[msg(1, "arx", "word " * 60)])])
        m.selected = "w"
        _, c = self.render(m, cols=80)
        body = [l for l in c.lines() if "word" in l]
        self.assertGreater(len(body), 2)
        for l in body:
            self.assertLessEqual(tui.text_width(l), 80)

    def test_overlay_covers_the_screen_and_scrolls(self):
        m = self.model()
        view = tui.View(m)
        view.show("Help", "\n".join(f"help line {i}" for i in range(80)))
        _, c = self.render(m, view=view)
        self.assertIn("Help", c.text())
        self.assertIn("help line 0", c.text())
        self.assertIn("any other key closes", c.text())

    def test_help_is_one_readable_column_at_any_width(self):
        everything = " ".join(k for _, entries in tui.HELP_SECTIONS for k, _ in entries)
        for width in (96, 60, 40, 24):
            lines = tui.help_lines(width)
            for text, _ in lines:
                self.assertLessEqual(tui.text_width(text), width, f"width {width}: {text!r}")
            joined = "\n".join(t for t, _ in lines)
            for _, entries in tui.HELP_SECTIONS:
                for key, desc in entries:
                    self.assertIn(key[:10], joined)
        wide = [t for t, _ in tui.help_lines(96)]
        row = next(t for t in wide if t.startswith("/wrapup AGENT"))
        self.assertIn("ask one agent to pin a summary", row)  # key and description on the same line, in columns
        self.assertEqual(row.index("ask"), next(t for t in wide if t.startswith("/close")).index("close this"))  # aligned
        narrow = [t for t, _ in tui.help_lines(40)]
        self.assertTrue(any(t.startswith(" ") and t.strip() for t in narrow))  # long descriptions wrap with an indent

    def test_help_is_drawn_in_a_box(self):
        m = self.model()
        view = tui.View(m)
        view.show_help()
        _, c = self.render(m, rows=60, view=view)  # tall enough to show every section
        text = c.text()
        for needle in ("┌", "└", "Help", "Conversations", "Message box", "Commands", "/wrapup AGENT", "n  ", "any other key closes"):
            self.assertIn(needle, text)

    def test_the_focused_pane_has_the_reversed_title(self):
        m = self.model()
        view = tui.View(m)
        view.focus = "list"
        _, c = self.render(m, view=view)
        self.assertEqual((c.styles[(0, 2)], c.styles[(0, 40)]), ("reverse", "title"))  # list title lit, conversation title not
        view.focus = "input"
        _, c = self.render(m, view=view)
        self.assertEqual((c.styles[(0, 2)], c.styles[(0, 40)]), ("title", "reverse"))

    def test_the_message_box_edits_like_a_line_editor(self):
        view = tui.View(self.model())
        for ch in "hello world":
            view.edit(ch)
        self.assertEqual((view.text, view.cursor), ("hello world", 11))
        view.edit("HOME"); view.edit("DELETE")
        self.assertEqual(view.text, "ello world")
        view.edit("END"); view.edit("KILL_WORD")
        self.assertEqual(view.text, "ello ")
        view.edit("LEFT"); view.edit("LEFT"); view.edit("x")
        self.assertEqual(view.text, "ellxo ")
        view.edit("KILL_END")
        self.assertEqual(view.text, "ellx")
        view.edit("BACKSPACE")
        self.assertEqual(view.text, "ell")
        view.edit("日")
        self.assertEqual(view.text, "ell日")
        view.edit("KILL_LINE")
        self.assertEqual((view.text, view.cursor), ("", 0))

    def test_ctrl_arrows_move_by_word(self):
        view = tui.View(self.model())
        for ch in "hello big  world":
            view.edit(ch)
        view.edit("WORD_LEFT")
        self.assertEqual(view.cursor, 11)  # start of "world"
        view.edit("WORD_LEFT")
        self.assertEqual(view.cursor, 6)  # start of "big"
        view.edit("WORD_LEFT")
        self.assertEqual(view.cursor, 0)
        view.edit("WORD_LEFT")
        self.assertEqual(view.cursor, 0)  # stays at the start
        view.edit("WORD_RIGHT")
        self.assertEqual(view.cursor, 5)  # end of "hello"
        view.edit("WORD_RIGHT")
        self.assertEqual(view.cursor, 9)  # end of "big", across the space
        view.edit("WORD_RIGHT")
        view.edit("WORD_RIGHT")
        self.assertEqual(view.cursor, len(view.text))  # stays at the end

    def test_modified_arrows_are_recognised_however_the_terminal_sends_them(self):
        for seq in ("[1;5D", "[5D", "Od", "[1;3D"):
            self.assertEqual(tui.decode_escape(seq), ["WORD_LEFT"], seq)
        for seq in ("[1;5C", "[5C", "Oc", "[1;3C"):
            self.assertEqual(tui.decode_escape(seq), ["WORD_RIGHT"], seq)
        self.assertEqual(tui.decode_escape(""), ["ESC"])  # a lone Esc
        self.assertEqual(tui.decode_escape("q"), ["ESC", "q"])  # an Alt-q stays Esc then q
        self.assertEqual(tui.decode_escape("[9z"), ["ESC", "[", "9", "z"])  # unknown: nothing is swallowed
        self.assertEqual((tui.KEYMAP["kLFT5"], tui.KEYMAP["kRIT5"]), ("WORD_LEFT", "WORD_RIGHT"))

    def test_history_recall(self):
        view = tui.View(self.model())
        for text in ("first", "second"):
            for ch in text:
                view.edit(ch)
            view.clear_input()
        view.recall(-1)
        self.assertEqual(view.text, "second")
        view.recall(-1)
        self.assertEqual(view.text, "first")
        view.recall(+1)
        view.recall(+1)
        self.assertEqual(view.text, "")

    def test_the_cursor_follows_the_text_and_long_input_scrolls_sideways(self):
        m = self.model()
        view = tui.View(m)
        view.focus = "input"
        view.text, view.cursor = "x" * 200, 200
        _, c = self.render(m, view=view)
        y, x = c.cursor_at
        self.assertEqual(y, 22)
        self.assertLess(x, 100)  # kept on screen
        self.assertIn("xxxx", c.lines()[22])


class ControllerTest(unittest.TestCase):
    def setup(self, answers=()):
        self.calls = []
        answers = list(answers)
        asked = []

        def request(op, **kw):
            self.calls.append((op, kw))
            if op == "wake_preview":
                return self.preview
            if op == "post":
                return {"message": {}, "wake": {"woke": ["arx"], "passive": None, "suppressed": None, "unreachable": []}}
            if op == "members":
                return {"members": [{"name": "kit", "kind": "human", "muted": False, "joined": True, "strikes": 0, "wake_cost": None},
                                    {"name": "arx", "kind": "agent", "muted": True, "joined": True, "strikes": 2, "wake_cost": {"warm": 25000, "cold": 90000, "n": 3}}]}
            if op == "invite":
                return {"added": ["a", "b"], "already": [], "unreachable": []}
            if op == "close_room":
                return {"summary": "the summary", "exported": "/tmp/x.md"}
            return {}

        self.preview = {"wakes": [], "total": 0, "notified": [], "suppressed": None, "passive": None, "unreachable": [], "already_pending": [], "confirm_over": 100000}
        model = tui_model.Model("kit", request=request)
        model.rooms = {"r": room("r")}
        model.selected = "r"
        self.view = tui.View(model)
        self.view.focus = "input"
        self.asked = asked
        self.ctl = tui.Controller(self.view, lambda prompt: (asked.append(prompt), answers.pop(0))[1])
        return model

    def type(self, text):
        for ch in text:
            self.ctl.key(ch)

    def ops(self):
        return [c[0] for c in self.calls]

    def test_enter_sends_and_the_toggles_apply_once(self):
        m = self.setup()
        self.ctl.key("F2")
        self.ctl.key("F3")
        self.type("hello")
        self.ctl.key("ENTER")
        post = next(kw for op, kw in self.calls if op == "post")
        self.assertEqual((post["body"], post["no_reply_needed"], post["kind"]), ("hello", True, "decision"))
        self.assertEqual((self.view.fyi, self.view.decision, self.view.text), (False, False, ""))
        self.type("again")
        self.ctl.key("ENTER")
        post = [kw for op, kw in self.calls if op == "post"][-1]
        self.assertEqual((post["no_reply_needed"], post["kind"]), (False, "post"))

    def test_an_expensive_post_asks_first_and_each_answer_does_its_thing(self):
        self.setup(answers=["n", "f", "y"])
        self.preview.update(total=250000, wakes=[{"name": "arx", "tokens": 250000, "cold": True}])
        self.type("big")
        self.ctl.key("ENTER")
        self.assertEqual(self.ops().count("post"), 0)  # n: not sent, and the text is kept
        self.assertEqual(self.view.text, "big")
        self.assertIn("this wakes 1 agents, about 250,000 tokens (arx 250k cold)", self.asked[0])
        self.ctl.key("ENTER")  # f: send as fyi
        self.assertTrue(next(kw for op, kw in self.calls if op == "post")["no_reply_needed"])
        self.type("more")
        self.ctl.key("ENTER")  # y: send normally
        self.assertFalse([kw for op, kw in self.calls if op == "post"][-1]["no_reply_needed"])

    def test_slash_commands(self):
        m = self.setup(answers=["y"])
        self.type("/mute arx"); self.ctl.key("ENTER")
        self.assertIn(("mute", {"room": "r", "member": "arx"}), self.calls)
        self.type("/add a b"); self.ctl.key("ENTER")
        self.assertEqual(next(kw for op, kw in self.calls if op == "invite")["names"], ["a", "b"])
        self.type("/fyi thanks @arx"); self.ctl.key("ENTER")
        self.assertTrue([kw for op, kw in self.calls if op == "post"][-1]["no_reply_needed"])
        self.type("/decision use sqlite"); self.ctl.key("ENTER")
        self.assertEqual([kw for op, kw in self.calls if op == "post"][-1]["kind"], "decision")
        self.type("/members"); self.ctl.key("ENTER")
        title, builder, _ = self.view.overlay
        self.assertEqual(title, "Members of r")
        self.assertIn("arx  (agent)  muted, strikes 2, wakes ~25k", [text for text, _ in builder(80)])
        self.ctl.key("x")  # any key closes the overlay
        self.assertIsNone(self.view.overlay)
        self.type("/close"); self.ctl.key("ENTER")
        self.assertIn("close r? this cannot be undone", self.asked[0])
        self.assertEqual(self.view.overlay[0], "Closed r")
        self.assertIn("saved to /tmp/x.md", m.notice)

    def test_close_asks_and_a_no_closes_nothing(self):
        self.setup(answers=["n"])
        self.type("/close"); self.ctl.key("ENTER")
        self.assertNotIn("close_room", self.ops())

    def test_wrapup_names_exactly_one_agent(self):
        self.setup()
        self.type("/wrapup a b"); self.ctl.key("ENTER")
        self.assertIn("usage: /wrapup AGENT", self.view.model.notice)
        self.assertNotIn("post", self.ops())
        self.type("/wrapup arx"); self.ctl.key("ENTER")
        body = next(kw for op, kw in self.calls if op == "post")["body"]
        self.assertTrue(body.startswith("@arx please pin one decision"))
        self.assertNotIn("@all", body)

    def test_errors_become_a_notice_not_a_crash(self):
        m = self.setup()
        self.type("/nonsense"); self.ctl.key("ENTER")
        self.assertIn("unknown command: /nonsense", m.notice)
        self.view.text = ""
        self.type("/mute"); self.ctl.key("ENTER")
        self.assertIn("usage: /mute NAME", m.notice)

    def test_list_keys(self):
        m = self.setup()
        self.view.focus = "list"
        self.ctl.key("ENTER")
        self.assertEqual(self.view.focus, "input")
        self.ctl.key("ESC")
        self.assertEqual(self.view.focus, "list")
        self.ctl.key("/")
        self.assertEqual((self.view.focus, self.view.text), ("input", "/"))
        self.ctl.key("ESC"); self.ctl.key("TAB")
        self.assertEqual(self.view.focus, "input")
        self.ctl.key("TAB")
        self.ctl.key("q")
        self.assertTrue(self.view.quit)

    def test_a_q_right_after_escape_is_an_alt_key_but_a_later_q_quits(self):
        self.setup()
        self.view.focus = "input"
        self.ctl.key("ESC")
        self.ctl.key("q")
        self.assertFalse(self.view.quit)
        self.assertEqual(self.view.focus, "list")
        time.sleep(0.12)
        self.ctl.key("q")
        self.assertTrue(self.view.quit)

    def test_each_room_keeps_its_own_draft(self):
        m = self.setup()
        m.rooms["other"] = room("other", last_ts=1)
        m.request = lambda op, **kw: {"members": []} if op == "members" else {"messages": [], "decisions": []}
        self.view.focus = "input"
        self.type("half typed for r")
        self.ctl.key("NEXT")
        self.assertEqual((m.selected, self.view.text), ("other", ""))
        self.type("for other")
        self.ctl.key("PREV")
        self.assertEqual((m.selected, self.view.text), ("r", "half typed for r"))
        self.ctl.key("NEXT")
        self.assertEqual(self.view.text, "for other")

    def test_the_wheel_scrolls_three_lines_over_a_conversation_and_changes_conversation_over_the_list(self):
        msgs = [msg(i, "arx", f"line {i}") for i in range(1, 61)]
        m = FakeModel([room("a", messages=msgs, last_ts=2), room("b", last_ts=1)])
        m.selected = "a"
        m.request = lambda op, **kw: {"members": []} if op == "members" else {"messages": [], "decisions": []}
        view = tui.View(m)
        ctl = tui.Controller(view, lambda p: "")
        ctl.wheel(+1, x=60, left=25)  # up over the conversation
        self.assertEqual(view.scroll["a"], 3)
        ctl.wheel(+1, x=60, left=25)
        self.assertEqual(view.scroll["a"], 6)
        ctl.wheel(-1, x=60, left=25)
        ctl.wheel(-1, x=60, left=25)
        ctl.wheel(-1, x=60, left=25)
        self.assertEqual(view.scroll["a"], 0)  # never below the newest
        ctl.wheel(-1, x=5, left=25)  # down over the list: the next conversation
        self.assertEqual(m.selected, "b")
        ctl.wheel(+1, x=5, left=25)
        self.assertEqual(m.selected, "a")
        ctl.wheel(+1, x=5, left=0)  # no list on screen (a narrow terminal): it scrolls
        self.assertEqual(view.scroll["a"], 3)

    def test_f1_opens_help_and_f4_hides_decisions(self):
        self.setup()
        self.ctl.key("F1")
        self.assertEqual(self.view.overlay[0], "Help")
        self.ctl.key("PPAGE")
        self.assertEqual(self.view.overlay[0], "Help")  # paging keeps it open
        self.ctl.key("x")
        self.assertIsNone(self.view.overlay)
        self.ctl.key("F4")
        self.assertFalse(self.view.show_decisions)

    def test_n_in_the_list_asks_for_a_name_and_creates_the_conversation(self):
        m = self.setup()
        created = []
        real_request = m.request

        def request(op, **kw):
            if op == "create_room":
                created.append(kw)
            return real_request(op, **kw)

        m.request = request
        import acm.tui as tui_module

        sent = []
        original = tui_module.client.request
        tui_module.client.request = lambda op, **kw: (sent.append((op, kw)) or {})
        self.addCleanup(lambda: setattr(tui_module.client, "request", original))
        asked = []
        self.ctl.ask_text = lambda prompt: (asked.append(prompt), "newroom a topic here")[1]
        self.view.focus = "list"
        m.load_rooms = lambda: m.rooms.__setitem__("newroom", room("newroom"))
        m.request = lambda op, **kw: {"members": []} if op == "members" else {"messages": [], "decisions": []}
        self.ctl.key("n")
        self.assertIn("new conversation (NAME [topic])", asked[0])
        self.assertEqual(sent[0], ("create_room", {"name": "newroom", "by": "kit", "topic": "a topic here"}))
        self.assertEqual((m.selected, self.view.focus), ("newroom", "input"))
        self.assertIn("created newroom", m.notice)

    def test_n_and_an_empty_answer_do_nothing(self):
        m = self.setup()
        self.view.focus = "list"
        self.ctl.ask_text = lambda prompt: None
        self.ctl.key("n")
        self.ctl.ask_text = lambda prompt: "   "
        self.ctl.key("n")
        self.assertEqual(m.selected, "r")

    def test_d_in_the_list_asks_and_a_no_deletes_nothing(self):
        m = self.setup(answers=["n"])
        self.view.focus = "list"
        self.ctl.key("d")
        self.assertIn("delete r?", self.asked[0])
        self.assertNotIn("delete_room", self.ops())
        self.assertEqual(m.selected, "r")

    def test_d_in_the_list_deletes_the_conversation_and_selects_a_neighbour(self):
        m = self.setup(answers=["y"])
        m.rooms["s"] = room("s")
        m.request = lambda op, **kw: (self.calls.append((op, kw)) or ({"export_removed": None} if op == "delete_room" else {"members": [], "messages": [], "decisions": []}))
        self.view.focus = "list"
        before = m.order()[0].name
        m.select(before)
        self.ctl.key("d")
        self.assertEqual([c for c in self.calls if c[0] == "delete_room"][0][1]["name"], before)
        self.assertNotIn(before, m.rooms)
        self.assertEqual(m.selected, next(iter(m.rooms)))
        self.assertIn(f"deleted {before}", m.notice)

    def test_clicking_a_conversation_selects_it(self):
        m = FakeModel([room("a", last_ts=2), room("b", last_ts=1)])
        m.request = lambda op, **kw: {"members": []} if op == "members" else {"messages": [], "decisions": []}
        view = tui.View(m)
        view.draw(tui.FakeCanvas(20, 100), 20, 100)
        ctl = tui.Controller(view, lambda p: "")
        ctl.click(view.hit_boxes[1][0])
        self.assertEqual(m.selected, "b")
        ctl.click(0)  # the title row is not a conversation
        self.assertEqual(m.selected, "b")


if __name__ == "__main__":
    unittest.main()
