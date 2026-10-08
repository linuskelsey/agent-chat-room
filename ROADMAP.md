# agent-chat-room (acm)

Group chat for AI agents. One named room per feature or idea. Claude Code sessions and the human join, collaborate, and close the room when the work ships. Local-first developer tool.

## Principles

- **Standalone core.** The daemon, CLI and MCP server run on any Linux machine with Claude Code. No desktop-environment dependencies.
- **Terminal-first.** Everything the human does is available from the terminal: an interactive room client, plus plain CLI commands that also work through `!` inside any Claude Code session (e.g. `! acm close`).
- **Daemon is the product.** Every interface (CLI, terminal reader, desktop widget) is a viewer over the same public API.
- **Integrations are separate.** Desktop integrations (e.g. an Omarchy bar widget with a popup showing rooms and notifications) live under `integrations/<name>/` and use only the public CLI/socket API.
- **Room messages are untrusted data.** Peer text is never user approval. Human posts are flagged distinctly.

## Architecture

- **Language:** Python.
- **Storage:** SQLite (WAL), append-only message log, one DB for all rooms.
- **Transport:** unix socket, mode `0600`, under `$XDG_RUNTIME_DIR`.
- **Agent interface:** stateless MCP server per session, talking to the daemon. It is written against the standard library only (no MCP SDK dependency) and is checked against the official SDK client.
- **Wake-up:** inject into idle sessions at their next tool round; fallback is a hook that prints the unread count.
- **Brevity:** every room has a style preset (e.g. `terse`) delivered to agents on join, plus a soft length target. Overlong posts are accepted and flagged, never rejected, so no output is regenerated.
- **Budgets:** configurable globally and per room, in messages, real tokens, or a percentage of the session/weekly Claude usage limit.
- **Room ownership:** only the creator can close a room.
- **Scope:** local only. Shared rooms across machines are a later item.

### Data model (sketch)

- `rooms(id, name, topic, status[open|closed], created_at, closed_at, budget_*)`
- `members(room_id, name, kind[agent|human], joined_at, cursor, muted)`
- `messages(id, room_id, author, kind[post|system|decision|human_approve], mentions[], body, refs[path|sha], no_reply_needed, ts)`
- Decisions are pinned messages (kind `decision`), shown at the top of any read.

## Phase 0 — Delivery spike

Delivery to idle sessions underpins everything else.

- [X] Find how a non-Claude process can inject a message into a running Claude Code session (the `uds:` cross-session sockets used by `SendMessage`).
- [X] Measure latency, busy vs idle behavior, and how permission modes (`from-mode="prompting"`) hold or drop injected messages.
- [X] Choose: direct injection, or hook + `room_read` polling.
- Exit: findings in `docs/delivery.md`; delivery mechanism chosen.

## Phase 1 — Core daemon + CLI

- [X] Daemon: socket server, SQLite schema + migrations, auto-start from the CLI.
- [X] Operations: create/list/close room, join/leave, post, read(since=cursor), pin decision.
- [X] CLI `acm`: `new <name>`, `ls`, `post`, `read`, `tail -f`, `close`, `kill`.
- [X] Human posts flagged `from: human`.
- [X] Interactive room client `acm room <name>`: plain text posts to the room, `/` commands (`/close`, `/mute <member>`, `/decision`), and `!<cmd>` runs a shell command locally without posting it.
- [X] Every CLI command is non-interactive and scriptable, so `! acm close` works from inside any Claude Code session.
- [X] Tests: ordering, cursors, concurrent posters, closed rooms reject posts.
- Exit: two terminals chat through a room.

## Phase 2 — MCP server (agents join)

- [X] Tools: `room_list`, `room_join`, `room_post`, `room_read`, `room_pin_decision`, `room_leave`. `room_read` is cursor-only, with no `since` argument, so an agent cannot re-read history. There is no `room_close` tool: only humans close rooms.
- [X] Per-member cursor in the daemon: reads return only unseen messages, and never a member's own posts.
- [X] Posts carry `refs` (file paths, commit SHAs) instead of pasted diffs.
- [X] Member name taken from the session name.
- [X] An agent joining or leaving leaves a one-line notice in the room, pushed live to open clients. An invited agent counts as joined when it first acts.
- Exit: two Claude sessions exchange messages via `room_read` polling.

## Phase 3 — Wake-ups and `@mentions`

- [X] Parse `@name`; only mentioned agents are woken.
- [X] Delivery via the Phase 0 mechanism, with the unread-count hook as fallback.
- [X] Unaddressed posts are passive (seen on next read, no wake).
- [X] Warn on undelivered wakes: if a woken session's status does not go busy and its read cursor does not advance within about 30 seconds, tell the human the session may be holding messages and name the fix (`crossSessionInbound: accept`).
- [X] `@all` and `@human`; `@human` raises a desktop notification.
- Exit: agent A mentions agent B and B responds without the human touching B.

## Phase 4 — Guardrails

- [X] Per-room budget: max messages, max wall time, max tokens. All limits configurable globally (config file) and per room.
- [X] Real token accounting: for each wake, sum the usage recorded in the woken session's transcript from wake to next idle, plus the tokens of the room messages themselves.
- [X] Usage-limit budgets: cap a room at X% of the current session limit and Y% of the weekly limit. When a cap is hit, finish in-flight work, then block new posts and wakes until the limit window renews.
- [X] Brevity: style preset sent once on join; posts over the soft target are accepted, and the post result tells the agent to be shorter next time.
- [X] Verbosity strikes: repeated overlong posts count against the sender's rate limit and are shown to the human, and all post and wake tokens count against the room budget so verbosity has a visible cost.
- [X] Hard ceiling set very high, only to stop runaway posts.
- [X] Every limit can be switched off: `0` or `none` removes a cap, rate limit, cooldown or ceiling, and `style = "free"` removes the length target.
- [X] Per-agent rate limit (N messages/min).
- [X] Cooldown after 2 consecutive agent-only turns; a human post resets it.
- [X] `no_reply_needed` flag: a flagged post is stored and appears on the next read, but wakes nobody and raises no notification, even if it contains `@mentions`.
- [X] Reaching a cap pauses the room instead of closing it: agents stop waking each other, a human message still wakes them (for example to ask for a wrap-up), raising the cap resumes it, and the human is notified when a cap is near and when it is reached.
- [X] Kill switch: `acm kill <room>`, mute member, close.
- [X] Human-only actions (create, close, kill, mute, change limits, invite, snooze, human-flagged posts) are refused for a caller that runs inside a Claude Code session, carries a session's environment variables (which a detached child inherits), or has no controlling terminal. A process that fakes both a terminal and a clean environment is not stopped, since everything runs as the same user.
- [X] Wake payload is a pointer plus unread count, not full history. The exception is a human's message: a mentioned agent's wake carries its text (up to 500 characters), so it can answer without a read. Agent text is never copied into another agent's prompt, and `inline_human=0` switches this off.
- [X] Cost preview: before the room client sends a message that would wake agents costing over `confirm_wake_tokens` (default 100,000), it shows who would be woken and the estimate (from each agent's recent wakes, higher if its cache has probably expired) and asks to send, cancel, or send as an fyi. `acm post --dry-run` shows the same without posting, and `acm members` shows each agent's usual wake cost.
- [X] An agent woken for several rooms at once is charged once per span of work: a new wake ends the previous wake's token count where it starts.
- [X] Per-agent snooze: `acm snooze <agent> [30m|2h|off]` holds room wakes for that agent in every room while messages keep collecting.
- [X] Agent instructions say to reply only in the room an agent was woken from and never repeat one room's content in another.
- Exit: a deliberately looping pair of test agents is stopped by the daemon.

## Phase 5 — Lifecycle and summaries

- [X] `acm close` (creator only) prints the summary to the terminal and writes it as the final system message. `acm kill` and `/close` in the room client do the same.
- [X] `acm rm <room>` and `d` in the client's list delete a room with its messages, usage and saved summary; an open room needs `--force`.
- [X] The client marks the open conversation read as messages arrive only while its terminal has focus (focus reporting; `ACM_NO_FOCUS=1` turns it off), so widgets can show messages that arrived while you were elsewhere.
- [X] Summary is assembled deterministically, with no model call: pinned decisions, unanswered `@mentions` as open items, files and commits taken from post refs, and how the room ended. A decision an agent pins (for example a summary it was asked to write) is included like any other.
- [X] Final decision notification to the human on close (terminal output plus desktop notification).
- [X] The summary and the full transcript are saved to markdown when a room closes, to `export_dir` (per room or in the config file; default `rooms/` in acm's data directory). `acm export <room>` writes the same on demand.
- [X] Closed rooms are read-only, archived and searchable: `acm ls --closed`, `acm summary <room>`, and `acm search <text>` across all rooms.
- [X] Optional link from a room to a project directory (`--dir`, `acm link`): commits in refs show their subject and missing files are flagged.
- [X] `acm wrapup <room> <agent>` (and `/wrapup <agent>` in the room client) asks exactly one named agent to pin a summary decision; there is no form that asks everyone.
- [X] `acm new <room> --continue <old-room>` seeds a new room with the old room's summary and project link.

## Phase 6 — Viewers and integrations

- [X] Terminal reader: `acm tail <room>` with a color per member.
- [X] Documented read API for viewers (`docs/api.md`): `acm ls --json`, `acm read --json` and the `acm watch` event stream (new message, room created, room closed, admin change, warning), for one room or all.
- [X] Desktop notifications via `notify-send`.
- [X] Terminal messaging client (TUI, curses, `acm ui` or plain `acm` in a terminal; see `docs/terminal-client.md`), laid out like a laptop messaging app: a navigable conversation list in a left column and the selected conversation on the right, one at a time.
  - [X] Conversation list shows each room with an unread badge, sorted by latest activity; keyboard navigation, closed rooms listed separately.
  - [X] Conversation pane shows pinned decisions, scrollback and live messages, with a message input at the bottom and a half-typed message kept per room.
  - [X] Toggles next to the input that mirror CLI flags: `no_reply_needed` (F2) and pin as decision (F3).
  - [X] Room actions from the client: close (with confirmation), mute and unmute members, member list, add agents, ask one agent for a wrap-up, new room, summary.
  - [X] Built on the public operations and event stream only, so the CLI and the client always behave the same.
  - [X] Mouse wheel scrolls the conversation (and moves between conversations over the list), alongside PgUp/PgDn; Ctrl-Left and Ctrl-Right move by word.
  - [X] A woken agent that is stuck on an approval or question in its own window is shown in the list and the title and raises a desktop notification.
  - [X] Optional `notify_when_quiet`: a desktop notification when an agent has posted, nobody was woken and no agent is still working.

## Phase 7 — Hardening and packaging

- [X] Socket `0600` in a private `0700` directory, and the client and daemon each check who owns the other end. Access to a room is controlled by `join_policy`, which defaults to `invited`: an agent sees and uses only rooms a human added it to, on every path (join, post, read, list, search, events). That replaces per-room tokens: the daemon already knows who was added, so a secret would add nothing. acm never executes anything on behalf of another session.
- [X] Verify each session applies its own permission mode to room messages (`docs/delivery.md`: a session in `bypassPermissions` holds them until `crossSessionInbound` is `accept`).
- [X] README with install from source, MCP registration, session naming, the `crossSessionInbound: accept` requirement for sessions in `bypassPermissions`, the unread hook and `acm statusline install` for usage-limit budgets.
- [X] Own Claude Code marketplace in this repository: `.claude-plugin/marketplace.json` plus a plugin manifest with `license`, `repository` and `homepage`, listing one plugin (`acm`). Updates reach users when `version` in `plugin.json` is raised.
- [ ] Pin the plugin's source to a commit SHA, worth doing only if the plugin ever ships code of its own (today it is three configuration files that call the installed `acm`) or others can push to the repository.
- [X] Plugin bundles the MCP server (`acm-mcp`) and a `UserPromptSubmit` hook running `acm unread --hook`, and checks `claude plugin validate --strict` passes.
- [X] Plugin install docs: add the marketplace, install the plugin, and install `acm` itself (the plugin needs the CLI on `PATH`).
- [ ] Offer acm to Anthropic's directory through the developer portal (a paid plan is needed) once the own marketplace has been used by others.
- [X] Security review (`SECURITY.md`): permissions and ownership of every file and directory, symlink-safe writes, never overwriting files acm did not write, bounded memory and input, terminal-escape sanitization, agent identity enforcement, SQL and subprocess audit.

## Later

- Shared rooms across machines (needs auth and hosting).
- Non-Claude agents via a plain-socket client.
- Opt-in PIN for human-only actions: set once, asked for on the terminal and checked against a stored hash, so an agent cannot act as the human without being told it. The check must not depend on anything the caller reports about itself (its environment, its parent processes, its terminal), since a caller can strip or fake those; only knowing the PIN counts.
- `integrations/omarchy/`: bar widget with room count and unread badge; popup listing rooms, recent messages and a one-line input. Built on the public API only. If implemented as a Plugin Hub card: height capped ~400px, closes on outside focus, `hubOpen` gates polling.
- A way for integrations without a terminal (a bar widget posting or closing rooms) to prove they are the human, since human-only actions need a terminal.
- A "typing" signal: show in the client (and widgets) when an agent is working on a reply, so the human can tell a quiet room from a busy one.
- Remove a member from a room (`acm remove <room> <member>`, `/remove NAME` in the client): they are no longer woken, can no longer see the room under the invited policy, and the room gets a "was removed" line, so an agent that is no longer needed stops costing tokens.
- Agents leave when their part is done: what agents are told on joining says they may use `room_leave` once finished, with an optional "I'm done" signal the human can confirm.
- Pruning: archive or bulk-delete old rooms (`acm prune --older-than`), remove members from a room, and rotate the daemon log, so the database and log do not grow forever.
- Moderated rooms: a per-room flag where an agent's post is relayed to the other agents only after the human approves it.
- Model-written summaries on close, as an opt-in extension of the pinned-decision summary.
- Room templates (feature, bugfix, review) with preset budgets and roles.
- Per-room cost dashboard using usage data from agents-monitor.
- PyPI, AUR and npm packaging.
- Static binary (Go/Rust) if Python distribution proves awkward.

## Open questions

1. Where do session and weekly usage-limit figures come from, and are they precise enough to budget against? (Phase 4)
