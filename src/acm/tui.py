"""The terminal messaging client: conversations in a left column, the selected one on the right.

Drawing goes through a small Canvas so the whole screen can be rendered to plain text in tests;
only CursesCanvas and run() touch the terminal. What the client knows and does lives in tui_model.py.
"""

import os
import queue
import threading
import time
import unicodedata

from acm import client, fmt, textsafe
from acm.errors import AcmError
from acm.tui_model import Model, Room

# -- text measuring ------------------------------------------------------


def cell_width(ch: str) -> int:
    if unicodedata.combining(ch) or unicodedata.category(ch) in ("Cf", "Mn", "Me"):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def text_width(text: str) -> int:
    return sum(cell_width(c) for c in text)


def clip(text: str, width: int, ellipsis: bool = True) -> str:
    """The longest prefix of `text` that fits in `width` terminal cells, with an ellipsis if it was cut."""
    if width <= 0:
        return ""
    if text_width(text) <= width:
        return text
    limit = width - (1 if ellipsis else 0)
    out, used = [], 0
    for ch in text:
        w = cell_width(ch)
        if used + w > limit:
            break
        out.append(ch)
        used += w
    return "".join(out) + ("…" if ellipsis else "")


def wrap(text: str, width: int) -> list[str]:
    """Break `text` into lines of at most `width` cells, at spaces where possible. Keeps explicit newlines."""
    width = max(1, width)
    lines: list[str] = []
    for para in text.split("\n"):
        cur, cur_w = "", 0
        for word in para.split(" "):
            ww = text_width(word)
            while ww > width:  # a word longer than a line is split
                take = clip(word, width - (cur_w + 1 if cur else 0) or width, ellipsis=False)
                if cur:
                    lines.append(cur)
                    cur, cur_w = "", 0
                    take = clip(word, width, ellipsis=False)
                lines.append(take)
                word = word[len(take):]
                ww = text_width(word)
            if cur and cur_w + 1 + ww > width:
                lines.append(cur)
                cur, cur_w = word, ww
            else:
                cur = f"{cur} {word}" if cur else word
                cur_w = text_width(cur)
        lines.append(cur)
    return lines


# -- canvases --------------------------------------------------------------


class FakeCanvas:
    """Draws into a grid of characters, so tests can read the screen as text."""

    def __init__(self, rows: int, cols: int):
        self.rows, self.cols = rows, cols
        self.grid = [[" "] * cols for _ in range(rows)]
        self.styles: dict[tuple[int, int], str] = {}
        self.cursor_at: tuple[int, int] | None = None

    def put(self, y: int, x: int, text: str, style: str = "normal") -> None:
        if not 0 <= y < self.rows:
            return
        for ch in text:
            w = cell_width(ch)
            if w == 0:
                continue
            if 0 <= x < self.cols:
                self.grid[y][x] = ch
                self.styles[(y, x)] = style
                if w == 2 and x + 1 < self.cols:
                    self.grid[y][x + 1] = ""
            x += w

    def fill(self, y: int, x: int, width: int, style: str = "normal", char: str = " ") -> None:
        self.put(y, x, char * max(0, min(width, self.cols - x)), style)

    def cursor(self, y: int, x: int) -> None:
        self.cursor_at = (y, x)

    def lines(self) -> list[str]:
        return ["".join(row).rstrip() for row in self.grid]

    def text(self) -> str:
        return "\n".join(self.lines())


class CursesCanvas:
    STYLES = {
        "normal": 0, "bold": "A_BOLD", "dim": "A_DIM", "reverse": "A_REVERSE", "unread": "A_BOLD", "decision": "A_BOLD",
        "notice": "A_BOLD", "selected": "A_REVERSE", "title": "A_BOLD", "system": "A_DIM", "closed": "A_DIM",
    }

    def __init__(self, stdscr):
        import curses

        self.curses, self.scr = curses, stdscr
        self.pairs = {}
        self.cursor_at = None

    def attr(self, style: str) -> int:
        c = self.curses
        if style.startswith("author:"):
            return c.color_pair(1 + int(style[7:]) % 6) | c.A_BOLD if c.has_colors() else c.A_BOLD
        spec = self.STYLES.get(style, 0)
        return getattr(c, spec) if isinstance(spec, str) else spec

    def put(self, y: int, x: int, text: str, style: str = "normal") -> None:
        rows, cols = self.scr.getmaxyx()
        if not 0 <= y < rows or x >= cols:
            return
        text = clip(text, cols - x, ellipsis=False)
        try:
            self.scr.addstr(y, x, text, self.attr(style))
        except self.curses.error:
            pass  # writing the bottom-right cell raises after drawing; nothing to do

    def fill(self, y: int, x: int, width: int, style: str = "normal", char: str = " ") -> None:
        self.put(y, x, char * max(0, width), style)

    def cursor(self, y: int, x: int) -> None:
        self.cursor_at = (y, x)


AUTHOR_COLORS = 6


def author_style(name: str) -> str:
    return f"author:{sum(map(ord, name)) % AUTHOR_COLORS}"


# -- the view ----------------------------------------------------------------

HELP_SECTIONS = [
    ("Conversations", [
        ("Up / Down, j / k", "move between conversations"),
        ("n", "start a new conversation"),
        ("d", "delete the selected conversation with its log and saved summary (asks first)"),
        ("Enter, Tab", "go to the message box"),
        ("Ctrl-N / Ctrl-P", "next / previous conversation, from anywhere"),
        ("PgUp / PgDn", "scroll the conversation"),
        ("q", "quit (from the list)"),
    ]),
    ("Message box", [
        ("Enter", "send"),
        ("Esc", "back to the list"),
        ("F2", "send as fyi: wakes nobody"),
        ("F3", "pin as a decision"),
        ("F4", "show or hide pinned decisions"),
        ("Up / Down", "recall what you sent earlier"),
        ("Ctrl-Left / Ctrl-Right", "move by word"),
        ("Ctrl-A / Ctrl-E", "start / end of the line"),
        ("Ctrl-U", "clear the line"),
        ("Ctrl-K / Ctrl-W", "cut to the end / cut a word"),
    ]),
    ("Commands (type them in the message box)", [
        ("/new NAME [topic]", "create a conversation"),
        ("/close", "close this conversation (asks first)"),
        ("/members", "who is in it"),
        ("/mute NAME", "stop that member posting"),
        ("/unmute NAME", "let them post again"),
        ("/add NAME ...", "bring agents in, by session name"),
        ("/wrapup AGENT", "ask one agent to pin a summary"),
        ("/fyi TEXT", "post without waking anyone"),
        ("/decision TEXT", "post and pin"),
        ("/summary", "decisions, open items, files"),
        ("/quit", "leave the client"),
    ]),
]
HELP_FOOTER = "Anything else you type is sent to the conversation. A message that would wake expensive agents asks first: y to send, n to cancel, f to send as an fyi."


def help_lines(width: int) -> list[tuple[str, str]]:
    """The help text laid out for `width` cells: one entry per line, keys in a fixed column, long text indented."""
    key_w = min(26, max(14, width // 3))
    out: list[tuple[str, str]] = []
    for title, entries in HELP_SECTIONS:
        out += [("", "normal")] + [(line, "title") for line in wrap(title, width)]
        for key, text in entries:
            wrapped = wrap(text, max(10, width - key_w - 2)) if text else [""]
            out.append((f"{clip(key, key_w - 1, ellipsis=False):<{key_w}}{wrapped[0]}", "normal"))
            out += [(" " * key_w + line, "normal") for line in wrapped[1:]]
    out += [("", "normal")] + [(line, "dim") for line in wrap(HELP_FOOTER, width)]
    return out


class View:
    """All the screen state and drawing. `model` holds the data."""

    def __init__(self, model: Model):
        self.model = model
        self.focus = "list"
        self.text = ""
        self.cursor = 0
        self.history: list[str] = []
        self.history_at: int | None = None
        self.fyi = False
        self.decision = False
        self.show_decisions = True
        self.scroll: dict[str, int] = {}  # room -> lines scrolled up from the newest
        self.list_scroll = 0
        self.overlay: tuple[str, object, int] | None = None  # (title, builder(width) -> [(text, style)], scroll)
        self.prompt: str | None = None
        self.hit_boxes: list[tuple[int, int, str]] = []  # (y0, y1, room) rows of the list, for clicks
        self.quit = False

    # -- layout --------------------------------------------------------

    def widths(self, cols: int) -> tuple[int, int]:
        """(left pane width, x where the conversation starts). The left pane is a drawer on narrow screens."""
        if cols < 70:
            return (cols, cols) if self.focus == "list" else (0, 0)
        left = max(22, min(32, cols // 4))
        return left, left + 1

    def draw(self, canvas, rows: int, cols: int) -> None:
        m = self.model
        self.hit_boxes = []
        left, x0 = self.widths(cols)
        if left:
            self.draw_list(canvas, rows, left)
            if x0 > left:
                for y in range(rows - 1):
                    canvas.put(y, left, "│", "dim")
        if x0 < cols:
            self.draw_conversation(canvas, rows, x0, cols - x0)
        self.draw_status(canvas, rows, cols)
        if self.overlay:
            self.draw_overlay(canvas, rows, cols)

    def draw_list(self, canvas, rows: int, width: int) -> None:
        m = self.model
        canvas.fill(0, 0, width, "reverse" if self.focus == "list" else "title")
        canvas.put(0, 0, clip(f" acm · {m.me}", width), "reverse" if self.focus == "list" else "title")
        entries: list[tuple[str, Room | None]] = [("room", r) for r in m.open_rooms()]
        closed = m.closed_rooms()
        if closed:
            entries.append(("divider", None))
            entries += [("room", r) for r in closed]
        height = rows - 2
        selected_at = next((i for i, (k, r) in enumerate(entries) if r and r.name == m.selected), 0)
        self.list_scroll = max(0, min(self.list_scroll, max(0, len(entries) - height)))
        if selected_at < self.list_scroll:
            self.list_scroll = selected_at
        elif selected_at >= self.list_scroll + height:
            self.list_scroll = selected_at - height + 1
        if not entries:
            canvas.put(2, 1, clip("no rooms yet", width - 1), "dim")
            canvas.put(3, 1, clip("type /new NAME", width - 1), "dim")
        for row, (kind, room) in enumerate(entries[self.list_scroll:self.list_scroll + height]):
            y = 1 + row
            if kind == "divider":
                canvas.put(y, 1, clip("closed", width - 1), "dim")
                continue
            badge = f" {room.unread}" if room.unread else ""
            mark = "!" if room.waiting else ("●" if room.unread else (" " if room.open else "·"))
            label = clip(f"{mark} {room.name}", width - len(badge) - 1)
            selected = room.name == m.selected
            here = "selected" if self.focus == "list" else "unread"  # bold, not reversed, while typing elsewhere
            style = here if selected else ("unread" if room.unread else ("closed" if not room.open else "normal"))
            canvas.fill(y, 0, width, style)
            canvas.put(y, 1, label, style)
            if badge:
                canvas.put(y, width - len(badge) - 1, badge, style)
            self.hit_boxes.append((y, y, room.name))

    def message_lines(self, room: Room, width: int) -> list[list[tuple[str, str]]]:
        """The conversation as screen lines, each a list of (text, style) pieces."""
        out = []
        for msg in room.messages:
            stamp = fmt.clock(msg["ts"])
            if msg["kind"] == "system":
                first, *rest = textsafe.clean(msg["body"]).split("\n")
                out.append([(f"{stamp} ", "dim"), (clip(f"* {first}", width - 6), "system")])
                continue
            who = "you" if msg["author"] == self.model.me else msg["author"]
            msg = {**msg, "body": textsafe.clean(msg["body"]), "refs": [textsafe.one_line(r) for r in msg["refs"]]}
            tags = (" (human)" if msg["from"] == "human" and msg["author"] != self.model.me else "")
            tags += " ✎fyi" if msg["no_reply_needed"] else ""
            prefix = f"{stamp} {who}{tags}: "
            mark = "★ " if msg["kind"] == "decision" else ""
            avail = max(10, width - text_width(prefix))
            body_lines = wrap(mark + msg["body"], avail)
            style = "decision" if msg["kind"] == "decision" else "normal"
            out.append([(f"{stamp} ", "dim"), (f"{who}{tags}", author_style(msg["author"])), (": ", "dim"), (body_lines[0], style)])
            pad = " " * text_width(prefix)
            out += [[(pad + line, style)] for line in body_lines[1:]]
            if msg["refs"]:
                out.append([(pad + clip("↳ " + ", ".join(msg["refs"]), avail), "dim")])
        return out

    def draw_conversation(self, canvas, rows: int, x0: int, width: int) -> None:
        m = self.model
        room = m.current
        input_y, toggles_y, sep_y = rows - 2, rows - 3, rows - 4
        if room is None:
            canvas.put(1, x0 + 1, "Select a conversation on the left, or type /new NAME to create one.", "dim")
            self.draw_input(canvas, input_y, toggles_y, sep_y, x0, width, None)
            return
        members = f"{room.member_count} members" if room.members is None else f"{len(room.members)} members"
        waiting = f"⚠ {', '.join(sorted(room.waiting))} waiting for you · " if room.waiting else ""
        right = f"{waiting}{members}{' · closed' if not room.open else ''}"
        head = f" {room.name}" + (f" · {textsafe.one_line(room.topic)}" if room.topic else "")
        focused = self.focus == "input"
        canvas.fill(0, x0, width, "reverse" if focused else "title")
        canvas.put(0, x0, clip(head, width - len(right) - 2), "reverse" if focused else "title")
        canvas.put(0, x0 + width - len(right) - 1, right, "reverse" if focused else "dim")
        canvas.fill(1, x0, width, "dim", "─")
        top = 2
        if self.show_decisions and room.decisions:
            shown = room.decisions[-3:]
            for d in shown:
                line = clip(f"★ {d['author']}: {' '.join(d['body'].split())}", width - 1)
                canvas.put(top, x0, line, "decision")
                top += 1
            if len(room.decisions) > len(shown):
                canvas.put(top, x0, f"  (+{len(room.decisions) - len(shown)} earlier decisions, /summary lists all)", "dim")
                top += 1
            canvas.fill(top, x0, width, "dim", "─")
            top += 1
        height = max(1, sep_y - top)
        lines = self.message_lines(room, width - 1)
        offset = self.scroll.get(room.name, 0)
        offset = max(0, min(offset, max(0, len(lines) - height)))
        self.scroll[room.name] = offset
        end = len(lines) - offset
        for i, pieces in enumerate(lines[max(0, end - height):end]):
            x = x0
            for text, style in pieces:
                canvas.put(top + height - min(height, end) + i, x, text, style)
                x += text_width(text)
        self.draw_input(canvas, input_y, toggles_y, sep_y, x0, width, room)
        if offset:
            canvas.put(sep_y, x0 + 1, f" ↓ {offset} lines below · PgDn ", "notice")

    def draw_input(self, canvas, input_y, toggles_y, sep_y, x0, width, room) -> None:
        canvas.fill(sep_y, x0, width, "dim", "─")
        if room is not None and not room.open:
            canvas.put(toggles_y, x0 + 1, "closed: read-only. /summary shows how it ended.", "dim")
            canvas.put(input_y, x0 + 1, "> ", "dim")
            return
        fyi = "[F2 fyi: ON ]" if self.fyi else "[F2 fyi: off]"
        dec = "[F3 decision: ON ]" if self.decision else "[F3 decision: off]"
        canvas.put(toggles_y, x0 + 1, fyi, "bold" if self.fyi else "dim")
        canvas.put(toggles_y, x0 + 2 + len(fyi), dec, "bold" if self.decision else "dim")
        hint = "Enter send · Tab list · F1 help"
        if width > len(fyi) + len(dec) + len(hint) + 6:
            canvas.put(toggles_y, x0 + width - len(hint) - 1, hint, "dim")
        prompt = "> "
        room_w = max(1, width - len(prompt) - 1)
        before = text_width(self.text[:self.cursor])
        start = 0
        while text_width(self.text[start:self.cursor]) >= room_w and start < self.cursor:
            start += 1
        shown = clip(self.text[start:], room_w, ellipsis=False)
        canvas.put(input_y, x0, prompt, "bold" if self.focus == "input" else "dim")
        canvas.put(input_y, x0 + len(prompt), shown, "normal")
        if self.focus == "input" and not self.prompt and not self.overlay:
            canvas.cursor(input_y, x0 + len(prompt) + text_width(self.text[start:self.cursor]))

    def draw_status(self, canvas, rows: int, cols: int) -> None:
        y = rows - 1
        canvas.fill(y, 0, cols, "reverse")
        if self.prompt:
            canvas.put(y, 0, clip(" " + self.prompt, cols), "reverse")
            return
        notice = self.model.notice
        if notice:
            canvas.put(y, 0, clip(" " + notice, cols), "reverse")
            return
        unread = self.model.total_unread()
        left = " ↑↓ conversations · Enter message · n new · F1 help · q quit" if self.focus == "list" else " Esc back to list · Ctrl-N/P switch · F2 fyi · F3 decision · F1 help"
        right = f"{unread} unread " if unread else ""
        canvas.put(y, 0, clip(left, cols - len(right)), "reverse")
        canvas.put(y, cols - len(right), right, "reverse")

    def draw_overlay(self, canvas, rows: int, cols: int) -> None:
        title, builder, scroll = self.overlay
        w, h = min(cols - 2, 96), rows - 4
        x, y = (cols - w) // 2, 1
        inner_w, inner_h = w - 4, h - 4
        body = builder(inner_w)
        scroll = max(0, min(scroll, max(0, len(body) - inner_h)))
        self.overlay = (title, builder, scroll)
        for yy in range(y, y + h):
            canvas.fill(yy, x, w, "normal")
        canvas.put(y, x, "┌" + "─" * (w - 2) + "┐", "dim")
        canvas.put(y + h - 1, x, "└" + "─" * (w - 2) + "┘", "dim")
        for yy in range(y + 1, y + h - 1):
            canvas.put(yy, x, "│", "dim")
            canvas.put(yy, x + w - 1, "│", "dim")
        canvas.put(y, x + 2, f" {clip(title, w - 6)} ", "title")
        for i, (text, style) in enumerate(body[scroll:scroll + inner_h]):
            canvas.put(y + 2 + i, x + 2, clip(text, inner_w, ellipsis=False), style)
        more = " ↓ more " if scroll + inner_h < len(body) else ""
        footer = f" PgUp/PgDn scroll · any other key closes{more} "
        canvas.put(y + h - 1, x + 2, clip(footer, w - 4), "dim")

    # -- input -----------------------------------------------------------

    def show(self, title: str, text: str) -> None:
        lines = text.split("\n")
        self.overlay = (title, lambda width: [(piece, "normal") for line in lines for piece in wrap(line, width)], 0)

    def show_help(self) -> None:
        self.overlay = ("Help", help_lines, 0)

    def edit(self, key) -> None:
        """Apply one key to the message box."""
        if isinstance(key, str) and len(key) == 1 and key.isprintable():  # longer strings are key names, e.g. HOME
            self.text = self.text[:self.cursor] + key + self.text[self.cursor:]
            self.cursor += len(key)
            self.history_at = None
            return
        edits = {
            "BACKSPACE": lambda: self._delete(self.cursor - 1, self.cursor),
            "DELETE": lambda: self._delete(self.cursor, self.cursor + 1),
            "LEFT": lambda: self._move(self.cursor - 1),
            "RIGHT": lambda: self._move(self.cursor + 1),
            "HOME": lambda: self._move(0),
            "END": lambda: self._move(len(self.text)),
            "KILL_LINE": lambda: self._delete(0, len(self.text)),
            "KILL_END": lambda: self._delete(self.cursor, len(self.text)),
            "KILL_WORD": lambda: self._delete(self._word_start(), self.cursor),
            "WORD_LEFT": lambda: self._move(self._word_start()),
            "WORD_RIGHT": lambda: self._move(self._word_end()),
        }
        if key in edits:
            edits[key]()

    def _move(self, to: int) -> None:
        self.cursor = max(0, min(len(self.text), to))

    def _delete(self, a: int, b: int) -> None:
        a, b = max(0, a), min(len(self.text), b)
        if a < b:
            self.text = self.text[:a] + self.text[b:]
            self.cursor = a

    def _word_start(self) -> int:
        i = self.cursor
        while i > 0 and self.text[i - 1] == " ":
            i -= 1
        while i > 0 and self.text[i - 1] != " ":
            i -= 1
        return i

    def _word_end(self) -> int:
        i = self.cursor
        while i < len(self.text) and self.text[i] == " ":
            i += 1
        while i < len(self.text) and self.text[i] != " ":
            i += 1
        return i

    def recall(self, step: int) -> None:
        if not self.history:
            return
        at = len(self.history) if self.history_at is None else self.history_at
        at = max(0, min(len(self.history), at + step))
        self.history_at = None if at == len(self.history) else at
        self.text = self.history[at] if at < len(self.history) else ""
        self.cursor = len(self.text)

    def clear_input(self) -> str:
        text, self.text, self.cursor, self.history_at = self.text, "", 0, None
        if text.strip():
            self.history.append(text)
        return text


# -- turning keys into actions ---------------------------------------------------


class Controller:
    """Keys in, model calls out. `ask` is how it asks the user a one-key question (injected so tests can answer)."""

    def __init__(self, view: View, ask, ask_text=lambda prompt: None):
        self.view, self.model, self.ask, self.ask_text = view, view.model, ask, ask_text
        self.last, self.last_at = None, 0.0

    def key(self, key) -> None:
        v, m = self.view, self.model
        previous, since = self.last, time.monotonic() - self.last_at
        self.last, self.last_at = key, time.monotonic()
        if previous == "ESC" and key == "q" and since < 0.08:
            return  # an Alt-q arrives as Esc then q within a few milliseconds; it is not a request to quit
        m.notice = "" if key not in (None,) else m.notice
        if v.overlay:
            title, builder, scroll = v.overlay
            if key == "PPAGE":
                v.overlay = (title, builder, max(0, scroll - 8))
            elif key == "NPAGE":
                v.overlay = (title, builder, scroll + 8)
            else:
                v.overlay = None
            return
        try:
            self._key(key)
        except AcmError as e:
            m.notice = f"! {e.message}"

    def _key(self, key) -> None:
        v, m = self.view, self.model
        if key == "RESIZE":
            return
        if key == "F1":
            return v.show_help()
        if key == "F2":
            v.fyi = not v.fyi
            return
        if key == "F3":
            v.decision = not v.decision
            return
        if key == "F4":
            v.show_decisions = not v.show_decisions
            return
        if key == "NEXT":
            return self.switch(+1)
        if key == "PREV":
            return self.switch(-1)
        if key in ("PPAGE", "NPAGE"):
            return self.scroll(+1 if key == "PPAGE" else -1)
        if v.focus == "list":
            if key in ("UP", "k"):
                return self.switch(-1)
            if key in ("DOWN", "j"):
                return self.switch(+1)
            if key in ("ENTER", "TAB", "RIGHT", "l"):
                v.focus = "input"
                return
            if key == "?":
                return v.show_help()
            if key == "n":
                return self.new_conversation()
            if key == "d":
                return self.delete_conversation()
            if key == "q":
                v.quit = True
                return
            if key == "r":
                m.load_rooms()
                return
            if key == "/":
                v.focus = "input"
                v.edit("/")
            return
        # input focus
        if key in ("ESC", "TAB"):
            v.focus = "list"
        elif key == "ENTER":
            self.submit()
        elif key == "UP":
            v.recall(-1)
        elif key == "DOWN":
            v.recall(+1)
        else:
            v.edit(key)

    def go(self, change) -> None:
        """Change the selected conversation, keeping each one's half-typed message to itself."""
        v, m = self.view, self.model
        if m.current:
            m.current.draft = v.text
        change()
        v.text = m.current.draft if m.current else ""
        v.cursor = len(v.text)
        v.history_at = None
        v.scroll.pop(m.selected or "", None)

    def create(self, text: str) -> None:
        """Make a conversation from "NAME [topic]" and open it."""
        name, _, topic = text.strip().partition(" ")
        if not name:
            raise AcmError("bad_request", "usage: /new NAME [topic]")
        client.request("create_room", name=name, by=self.model.me, topic=topic.strip())
        self.model.load_rooms()
        self.go(lambda: self.model.select(name))
        self.view.focus = "input"
        self.model.notice = f"created {name}. /add AGENT brings an agent in"

    def new_conversation(self) -> None:
        text = self.ask_text("new conversation (NAME [topic]): ")
        if text and text.strip():
            self.create(text)

    def delete_conversation(self) -> None:
        v, m = self.view, self.model
        room = m.current
        if room is None:
            return
        state = "still OPEN, and its messages, usage and saved summary" if room.open else "its messages, usage and saved summary"
        if self.ask(f"delete {room.name}? {state} go for good [y/N]") != "y":
            return
        m.delete_room()
        v.text = m.current.draft if m.current else ""
        v.cursor = len(v.text)
        v.history_at = None
        v.scroll.pop(room.name, None)

    def switch(self, step: int) -> None:
        self.go(lambda: self.model.move(step))

    def scroll(self, direction: int, lines: int = 8) -> None:
        """Scroll the conversation `lines` lines: up (+1) towards older messages, or down (-1) towards the newest."""
        v, m = self.view, self.model
        room = m.current
        if room is None:
            return
        at = max(0, v.scroll.get(room.name, 0) + direction * lines)
        v.scroll[room.name] = at
        # reaching the top of what is loaded pulls in an earlier page
        if direction > 0 and at >= max(0, len(v.message_lines(room, 80)) - lines):
            if m.load_older():
                v.scroll[room.name] = at

    def wheel(self, direction: int, x: int, left: int) -> None:
        """The mouse wheel (up is +1). Over the conversation list it moves between conversations; anywhere
        else it scrolls the conversation three lines a notch."""
        if left and x < left:
            self.switch(-direction)
        else:
            self.scroll(direction, lines=3)

    def submit(self) -> None:
        v, m = self.view, self.model
        text = v.text.strip()
        if not text:
            return
        if text.startswith("/"):
            v.clear_input()
            return self.command(text)
        pre = m.preview(text, fyi=v.fyi)
        fyi, decision = v.fyi, v.decision
        if m.needs_confirm(pre):
            who = ", ".join(f"{w['name']} {w['tokens'] // 1000}k" + (" cold" if w["cold"] else "") for w in pre["wakes"])
            answer = self.ask(f"this wakes {len(pre['wakes'])} agents, about {pre['total']:,} tokens ({who}). send? [y/N/f=as fyi]")
            if answer == "f":
                fyi = True
            elif answer != "y":
                m.notice = "not sent"
                return
        v.clear_input()
        m.post(text, fyi=fyi, decision=decision)
        v.fyi = v.decision = False
        v.scroll[m.selected] = 0

    def command(self, text: str) -> None:
        v, m = self.view, self.model
        cmd, _, rest = text[1:].partition(" ")
        rest = rest.strip()
        if cmd in ("quit", "q", "exit"):
            v.quit = True
        elif cmd == "help":
            v.show_help()
        elif cmd == "new":
            self.create(rest)
        elif cmd == "close":
            room = m.current
            if room is None:
                raise AcmError("bad_request", "no conversation selected")
            if self.ask(f"close {room.name}? this cannot be undone [y/N]") == "y":
                res = m.close_room()
                v.show(f"Closed {room.name}", res["summary"])
        elif cmd == "members":
            m.refresh_members()
            room = m.current
            if room is None or room.members is None:
                raise AcmError("bad_request", "no conversation selected")
            lines = []
            for mem in room.members.values():
                extra = [t for t, on in (("muted", mem["muted"]), ("invited, not joined", not mem["joined"]),
                                         (f"strikes {mem['strikes']}", mem["strikes"])) if on]
                cost = mem.get("wake_cost")
                if cost and cost["n"]:
                    extra.append(f"wakes ~{cost['warm'] / 1000:.0f}k")
                lines.append(f"{mem['name']}  ({mem['kind']})" + (f"  {', '.join(extra)}" if extra else ""))
            v.show(f"Members of {room.name}", "\n".join(lines))
        elif cmd in ("mute", "unmute"):
            if not rest:
                raise AcmError("bad_request", f"usage: /{cmd} NAME")
            m.set_muted(rest, cmd == "mute")
        elif cmd == "add":
            if not rest:
                raise AcmError("bad_request", "usage: /add NAME...")
            m.invite(rest.replace(",", " ").split())
        elif cmd == "wrapup":
            if not rest or " " in rest:
                raise AcmError("bad_request", "usage: /wrapup AGENT (exactly one agent)")
            text = m.wrapup_text(rest)
            pre = m.preview(text)
            if m.needs_confirm(pre) and self.ask(f"asks {rest}, about {pre['total']:,} tokens. send? [y/N]") != "y":
                m.notice = "not sent"
                return
            m.post(text)
        elif cmd == "fyi":
            if not rest:
                raise AcmError("bad_request", "usage: /fyi TEXT")
            m.post(rest, fyi=True)
        elif cmd == "decision":
            if not rest:
                raise AcmError("bad_request", "usage: /decision TEXT")
            m.post(rest, decision=True)
        elif cmd == "summary":
            v.show("Summary", m.summary_text())
        else:
            raise AcmError("bad_request", f"unknown command: /{cmd} (F1 for help)")

    def click(self, y: int) -> None:
        for y0, y1, name in self.view.hit_boxes:
            if y0 <= y <= y1:
                self.go(lambda: self.model.select(name))
                return


# -- the terminal ----------------------------------------------------------------


KEYMAP = {
    "KEY_UP": "UP", "KEY_DOWN": "DOWN", "KEY_LEFT": "LEFT", "KEY_RIGHT": "RIGHT", "KEY_HOME": "HOME", "KEY_END": "END",
    "KEY_BACKSPACE": "BACKSPACE", "KEY_DC": "DELETE", "KEY_PPAGE": "PPAGE", "KEY_NPAGE": "NPAGE", "KEY_RESIZE": "RESIZE",
    "KEY_F(1)": "F1", "KEY_F(2)": "F2", "KEY_F(3)": "F3", "KEY_F(4)": "F4", "KEY_ENTER": "ENTER",
    # modified arrows, as terminfo names them: Ctrl-Left/Right and Alt-Left/Right move by word
    "kLFT5": "WORD_LEFT", "kRIT5": "WORD_RIGHT", "kLFT3": "WORD_LEFT", "kRIT3": "WORD_RIGHT",
}
# The same keys as raw escape sequences, for terminals whose terminfo does not describe them.
ESCAPE_KEYS = {
    "[1;5D": "WORD_LEFT", "[1;5C": "WORD_RIGHT", "[1;3D": "WORD_LEFT", "[1;3C": "WORD_RIGHT",
    "[5D": "WORD_LEFT", "[5C": "WORD_RIGHT", "Od": "WORD_LEFT", "Oc": "WORD_RIGHT",
    "[1;2D": "LEFT", "[1;2C": "RIGHT",
}


def decode_escape(sequence: str) -> list[str]:
    """Keys for the characters that followed an Esc: one key if they spell a known sequence, otherwise Esc and then
    each character as typed (so an Alt-q still arrives as Esc then q)."""
    key = ESCAPE_KEYS.get(sequence)
    return [key] if key else ["ESC", *sequence]
CONTROL = {
    "\n": "ENTER", "\r": "ENTER", "\t": "TAB", "\x1b": "ESC", "\x7f": "BACKSPACE", "\x08": "BACKSPACE",
    "\x01": "HOME", "\x05": "END", "\x15": "KILL_LINE", "\x0b": "KILL_END", "\x17": "KILL_WORD",
    "\x0e": "NEXT", "\x10": "PREV", "\x0c": "RESIZE",
}


def translate(raw) -> str | None:
    """A key from curses (a character or a key code) as a name this module understands."""
    import curses

    if isinstance(raw, int):
        return KEYMAP.get(curses.keyname(raw).decode(errors="replace"))
    return CONTROL.get(raw, raw)


class App:
    def __init__(self, stdscr, me: str):
        import curses

        self.curses, self.scr = curses, stdscr
        self.model = Model(me)
        self.view = View(self.model)
        self.controller = Controller(self.view, self.ask, self.ask_text)
        self.t0 = time.monotonic()
        path = os.environ.get("ACM_UI_LOG")  # a timing log for chasing slowness: key arrivals, redraws, daemon requests
        self.log = open(path, "w") if path else None
        if self.log:
            inner = self.model.request

            def timed(op, **kw):
                start = time.monotonic()
                try:
                    return inner(op, **kw)
                finally:
                    self.trace(f"request {op} {1000 * (time.monotonic() - start):.1f} ms")

            self.model.request = timed
        self.events: queue.Queue = queue.Queue()
        self.stopping = threading.Event()

    def ask(self, prompt: str) -> str:
        """Ask a one-key question on the status line. Returns the lowercase key, or '' for Enter/Esc."""
        self.view.prompt = prompt
        try:
            while True:
                self.redraw()
                try:
                    raw = self.scr.get_wch()
                except self.curses.error:
                    continue
                key = translate(raw)
                if isinstance(key, str) and len(key) == 1:
                    return key.lower()
                if key in ("ENTER", "ESC"):
                    return ""
        finally:
            self.view.prompt = None

    def ask_text(self, prompt: str) -> str | None:
        """Ask for a line of text on the status line. Enter accepts, Esc cancels (returns None)."""
        view = self.view
        text = ""
        try:
            while True:
                view.prompt = prompt + text + "▏"
                self.redraw()
                try:
                    raw = self.scr.get_wch()
                except self.curses.error:
                    continue
                key = translate(raw)
                if key == "ENTER":
                    return text
                if key == "ESC":
                    return None
                if key == "BACKSPACE":
                    text = text[:-1]
                elif isinstance(key, str) and len(key) == 1 and key.isprintable():
                    text += key
        finally:
            view.prompt = None

    def following_chars(self) -> str:
        """What arrives within a few milliseconds of an Esc: the rest of a key sequence, or nothing."""
        out = ""
        self.scr.timeout(15)
        try:
            while len(out) < 8:
                try:
                    ch = self.scr.get_wch()
                except self.curses.error:
                    break
                if not isinstance(ch, str):
                    break
                out += ch
                if ch.isalpha() or ch == "~":  # the final character of a sequence
                    break
        finally:
            self.scr.timeout(120)
        return out

    def trace(self, message: str) -> None:
        if self.log:
            self.log.write(f"{time.monotonic() - self.t0:9.3f}  {message}\n")
            self.log.flush()

    def redraw(self) -> None:
        started = time.monotonic()
        rows, cols = self.scr.getmaxyx()
        self.scr.erase()
        canvas = CursesCanvas(self.scr)
        self.view.draw(canvas, rows, cols)
        if canvas.cursor_at:
            self.curses.curs_set(1)
            self.scr.move(*canvas.cursor_at)
        else:
            self.curses.curs_set(0)
        self.scr.refresh()
        self.trace(f"redraw {1000 * (time.monotonic() - started):.1f} ms")

    def pump(self) -> None:
        """Follow the daemon's event stream in the background, reconnecting if the daemon restarts."""
        first = True
        while not self.stopping.is_set():
            try:
                stream = client.watch()
                if not first:
                    self.events.put({"event": "_reconnected"})
                first = False
                for ev in stream:
                    self.events.put(ev)
            except Exception:
                pass
            if self.stopping.is_set():
                return
            self.events.put({"event": "_disconnected"})
            self.stopping.wait(2)

    def drain(self) -> bool:
        changed = False
        while True:
            try:
                ev = self.events.get_nowait()
            except queue.Empty:
                return changed
            try:
                if ev["event"] == "_disconnected":
                    self.model.notice = "lost the daemon, reconnecting..."
                elif ev["event"] == "_reconnected":
                    self.model.load_rooms()
                    if self.model.current:
                        self.model.current.loaded = False
                        self.model.select(self.model.selected)
                    self.model.notice = "reconnected"
                else:
                    self.model.apply(ev)
                changed = True
            except AcmError as e:
                self.model.notice = f"! {e.message}"

    def run(self) -> None:
        c = self.curses
        if hasattr(c, "set_escdelay"):
            c.set_escdelay(25)  # a lone Esc must not wait a second to see whether more of a key sequence follows
        c.use_default_colors()
        if c.has_colors():
            c.start_color()
            for i, colour in enumerate((c.COLOR_CYAN, c.COLOR_GREEN, c.COLOR_YELLOW, c.COLOR_MAGENTA, c.COLOR_BLUE, c.COLOR_RED)):
                c.init_pair(1 + i, colour, -1)
        self.scr.keypad(True)
        self.scr.timeout(120)
        if not os.environ.get("ACM_NO_MOUSE"):  # the wheel scrolls; hold Shift to select text, as with any mouse-aware program
            c.mousemask(c.ALL_MOUSE_EVENTS)
        self.model.load_rooms()
        if self.model.order():
            first = next((r for r in self.model.order() if r.unread), self.model.order()[0])
            self.model.select(first.name)
        threading.Thread(target=self.pump, daemon=True).start()
        dirty = True
        while not self.view.quit:
            if self.drain():
                dirty = True
            if dirty:
                self.redraw()
                dirty = False
            try:
                raw = self.scr.get_wch()
            except c.error:
                continue
            if raw == "\x1b":  # a bare Esc, or the start of a key sequence the terminal info did not recognise
                keys = decode_escape(self.following_chars())
                self.trace(f"key ESC + {keys!r}")
                for key in keys:
                    self.controller.key(key)
                dirty = True
                continue
            self.trace(f"key {raw!r} -> {translate(raw) if not (isinstance(raw, int) and raw == c.KEY_MOUSE) else 'mouse'}")
            if isinstance(raw, int) and raw == c.KEY_MOUSE:
                try:
                    _, mx, my, _, state = c.getmouse()
                except c.error:
                    continue
                left, _ = self.view.widths(self.scr.getmaxyx()[1])
                if state & getattr(c, "BUTTON4_PRESSED", 0):
                    self.controller.wheel(+1, mx, left)
                elif state & getattr(c, "BUTTON5_PRESSED", 0):
                    self.controller.wheel(-1, mx, left)
                elif state & (c.BUTTON1_CLICKED | c.BUTTON1_PRESSED) and mx < left:
                    self.controller.click(my)
                dirty = True
                continue
            self.controller.key(translate(raw))
            dirty = True
        self.stopping.set()
        if self.log:
            self.log.close()


def run(me: str) -> None:
    import curses
    import locale

    locale.setlocale(locale.LC_ALL, "")
    os.environ.setdefault("ESCDELAY", "25")  # read when curses starts

    def main(stdscr):
        App(stdscr, me).run()

    curses.wrapper(main)
