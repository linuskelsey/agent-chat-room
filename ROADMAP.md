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
- Exit: two Claude sessions exchange messages via `room_read` polling.

## Phase 3 — Wake-ups and `@mentions`

- [X] Parse `@name`; only mentioned agents are woken.
- [X] Delivery via the Phase 0 mechanism, with the unread-count hook as fallback.
- [X] Unaddressed posts are passive (seen on next read, no wake).
- [X] Warn on undelivered wakes: if a woken session's status does not go busy and its read cursor does not advance within about 30 seconds, tell the human the session may be holding messages and name the fix (`crossSessionInbound: accept`).
- [X] `@all` and `@human`; `@human` raises a desktop notification.
- Exit: agent A mentions agent B and B responds without the human touching B.

## Phase 4 — Guardrails

- [ ] Per-room budget: max messages, max wall time, max tokens. All limits configurable globally (config file) and per room.
- [ ] Real token accounting: for each wake, sum the usage recorded in the woken session's transcript from wake to next idle, plus the tokens of the room messages themselves.
- [ ] Usage-limit budgets: cap a room at X% of the current session limit and Y% of the weekly limit. When a cap is hit, finish in-flight work, then block new posts and wakes until the limit window renews.
- [ ] Brevity: style preset sent once on join; posts over the soft target are accepted, and the post result tells the agent to be shorter next time.
- [ ] Verbosity strikes: repeated overlong posts count against the sender's rate limit and are shown to the human, and all post and wake tokens count against the room budget so verbosity has a visible cost.
- [ ] Hard ceiling set very high, only to stop runaway posts.
- [ ] Per-agent rate limit (N messages/min).
- [ ] Cooldown after 2 consecutive agent-only turns; a human post resets it.
- [ ] `no_reply_needed` flag; woken agents do not reply to flagged posts.
- [ ] Hard cap auto-closes the room and notifies the human.
- [ ] Kill switch: `acm kill <room>`, mute member, close.
- [ ] Human-only actions (approvals, `kill`, human-flagged posts) require something an agent's Bash tool cannot do, since agents can run `acm` and could otherwise post as the human.
- [ ] Wake payload is a pointer plus unread count, not full history.
- Exit: a deliberately looping pair of test agents is stopped by the daemon.

## Phase 5 — Lifecycle and summaries

- [ ] `acm close` (creator only) prints the summary to the terminal and writes it as the final system message.
- [ ] Summary is assembled deterministically, with no model call: pinned decisions, unanswered `@mentions` as open items, and files and commits taken from post refs.
- [ ] Final decision notification to the human on close (terminal output plus desktop notification).
- [ ] Summary exported to markdown in a configurable directory.
- [ ] Closed rooms are read-only, archived and searchable.
- [ ] Optional link from a room to a project directory/repo for relative refs.

## Phase 6 — Viewers and integrations

- [ ] Terminal reader: `acm tail <room>` with a color per member.
- [ ] Documented read API for viewers: `acm ls --json`, `acm read --json`, `acm watch` (event stream: new message, unread change, room closed).
- [ ] Desktop notifications via `notify-send`.
- [ ] Optional web or TUI viewer for long reading.
- [ ] `integrations/omarchy/`: bar widget with room count and unread badge; popup listing rooms, recent messages and a one-line input. Built on the public API only. If implemented as a Plugin Hub card: height capped ~400px, closes on outside focus, `hubOpen` gates polling.

## Phase 7 — Hardening and packaging

- [ ] Socket `0600`, per-room join tokens, no shared tool execution across sessions.
- [ ] Verify each session applies its own permission mode to room messages.
- [ ] Package as a Claude plugin marketplace entry (MCP server + hooks bundled, daemon auto-started by the MCP server).
- [ ] README and install docs, including the `crossSessionInbound: accept` requirement for sessions running in `bypassPermissions`.
- [ ] Security review.

## Later

- Shared rooms across machines (needs auth and hosting).
- Non-Claude agents via a plain-socket client.
- Moderated rooms: a per-room flag where an agent's post is relayed to the other agents only after the human approves it.
- Model-written summaries on close, as an opt-in extension of the pinned-decision summary.
- Room templates (feature, bugfix, review) with preset budgets and roles.
- Per-room cost dashboard using usage data from agents-monitor.
- AUR / npm packaging.
- Static binary (Go/Rust) if Python distribution proves awkward.

## Open questions

1. Where do session and weekly usage-limit figures come from, and are they precise enough to budget against? (Phase 4)
