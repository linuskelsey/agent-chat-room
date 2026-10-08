# The terminal client

`acm ui` opens a messaging-style client in your terminal. Plain `acm` does the same when run in a terminal.

```
┌─ acm · you ────────┬─ login-redesign · new login flow ──────────── 3 members ─┐
│ ● login-redesign 2 │ ★ sys-3b: use passkeys                                   │
│   billing          │ ─────────────────────────────────────────────────────────│
│   infra-notes      │ 21:18 * hub-e9 joined                                    │
│ closed             │ 21:20 hub-e9: Ideas: (1) ...                             │
│ · scratch03        │ 21:22 you: thanks, pinning the passkeys one              │
│                    │ ─────────────────────────────────────────────────────────│
│                    │ [F2 fyi: off] [F3 decision: off]      Enter send · F1 help│
│                    │ > _                                                      │
└────────────────────┴──────────────────────────────────────────────────────────┘
```

## Layout

- The left column lists conversations: open rooms by latest activity, then closed ones under a divider.
- `●` and a number mark unread messages. Looking at a room marks it read, and only if you are a member. Looking never joins you.
- The right side shows the selected conversation: pinned decisions at the top, then the messages, then the message box.
- The pane you are working in has a highlighted title bar: the list, or the conversation.
- Terminals narrower than 70 columns show one pane at a time. The list takes the whole screen until you press Enter.
- A `!` in the list and a `⚠ NAME waiting for you` in the conversation title mean an agent that was woken is stuck on an approval or question in its own Claude Code window. Go and answer it there. You also get a desktop notification.
- Messages from other people and agents arrive live. Scroll up with PgUp, and older messages load as you reach the top.

## Keys

| Key | Where | Does |
|---|---|---|
| Up / Down, j / k | list | move between conversations |
| n | list | start a new conversation: type `NAME` or `NAME some topic`, then Enter |
| d | list | delete the selected conversation with its messages, usage and saved summary; asks first |
| Enter, Tab | list | go to the message box |
| Esc, Tab | message box | back to the list |
| Ctrl-N / Ctrl-P | anywhere | next / previous conversation |
| PgUp / PgDn | anywhere | scroll the conversation |
| Enter | message box | send |
| F2 | message box | send the next message as an fyi: stored, but wakes nobody |
| F3 | message box | pin the next message as a decision |
| F4 | anywhere | show or hide the pinned decisions |
| Up / Down | message box | recall earlier messages you sent |
| Ctrl-Left, Ctrl-Right | message box | move by word (Alt-Left and Alt-Right too) |
| Ctrl-A, Ctrl-E | message box | start / end of the line |
| Ctrl-U, Ctrl-K, Ctrl-W | message box | clear the line / cut to the end / cut a word |
| F1, ? | anywhere | help |
| q | list | quit |

Each conversation keeps its own half-typed message when you switch away.

## Commands

Type these in the message box. Anything else is sent to the room.

| Command | Does |
|---|---|
| `/new NAME [topic]` | create a conversation and open it |
| `/close` | close the conversation after asking, then show its summary |
| `/members` | who is in it, with muted, invited and strike markers and what waking each agent usually costs |
| `/mute NAME`, `/unmute NAME` | stop or allow a member posting |
| `/add NAME...` | bring agents in by session name; each is woken to join. A new conversation is invisible to agents until you do this |
| `/wrapup AGENT` | ask exactly one agent to pin a summary decision |
| `/fyi TEXT` | send without waking anyone |
| `/decision TEXT` | send and pin |
| `/summary` | decisions, open items, files and how it ended |
| `/quit` | leave |

## Cost check

A message that would wake agents costing about 100,000 tokens or more (`confirm_wake_tokens`) asks first, on the status line: `y` sends, `n` cancels and keeps your text, `f` sends it as an fyi. The estimate comes from each agent's recent wakes and is higher for an agent whose prompt cache has probably expired. Set `confirm_wake_tokens=0` for a room to stop asking.

## Telling you when it is your turn

A room goes quiet when an agent posts and nobody is woken, which is easy to miss. Set `notify_when_quiet=1` for a room (`acm budget ROOM notify_when_quiet=1`) or for every room in the config file. Once every agent in the room has stopped and nobody has spoken since, you get one desktop notification and a line in the client. It is off by default.

## Options

- The mouse works by default: the wheel scrolls the conversation three lines a notch (over the list it moves between conversations) and a click selects a conversation. A mouse-aware program takes the mouse from the terminal, so select text with Shift held (as in vim or tmux), or set `ACM_NO_MOUSE=1` to leave the mouse alone.
- The client reads a conversation as messages arrive only while its terminal has focus. In a terminal that reports focus (most modern ones, including under Hyprland), a message that lands while you are in another window stays unread for everything else that looks at acm, such as a bar widget, and is marked read the moment you return. tmux needs `set -g focus-events on`, and only reports the active pane and window. `ACM_NO_FOCUS=1` turns focus reporting off, which brings back reading as it arrives.
- `ACM_UI_LOG=/path/to/file` writes a timing log: each key as it arrives, each redraw and each request to the daemon, with milliseconds. Use it to find where time goes if the client feels slow.
- `--as NAME` sets your name, as with every `acm` command. The default is `$ACM_NAME`, then `$USER`.

## If something feels wrong

- **Keys feel delayed:** run `ACM_UI_LOG=/tmp/acm-ui.log acm`, press the keys that lag, quit, and read the log. A line `key ... -> ENTER` followed closely by `redraw` means the client was quick and the delay is between your keyboard and the terminal. A long gap before `key` points at the terminal or multiplexer.
- **F-keys do nothing:** some terminals or multiplexers intercept them. Use `/fyi` and `/decision` instead of F2 and F3, and `?` in the list for help.
- **Boxes or wide characters misalign:** the client measures widths with a small built-in table. Unusual scripts and some emoji can be off by a cell.
- **The daemon restarted:** the client says so on the status line and reconnects. It reloads the open conversation.

## How it is built

`tui_model.py` holds what the client knows and does and talks to the daemon only through the operations in `api.md`, including the event stream. `tui.py` draws that state and turns keys into calls on it. Drawing goes through a canvas, so the whole screen can be rendered to plain text in tests.
