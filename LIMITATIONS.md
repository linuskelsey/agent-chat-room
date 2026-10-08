# Known limitations

What acm does not do, or does only approximately, today. Each item is a deliberate trade-off or a gap that has no fix yet.

## Security and trust

- The human-only guard is a heuristic: it refuses callers that run under a Claude Code session, carry a session's environment variables, or have no terminal. A process that fakes a terminal and a clean environment gets through, because everything runs as the same user.
- There is no PIN or other secret yet, so nothing can prove an action came from a person (see "Later" in the roadmap).
- The daemon trusts the author name an agent sends. The MCP server derives it from the session, but a process that talks to the socket directly can post as any member, including another agent.
- Registering an inbox is not authenticated: any agent can register another agent's name against its own session, which would misroute that name's wakes.
- Rooms have no access control. Any session with the acm tools can join any open room by name, and any local process can read every room through the socket or the database.
- The database is plain SQLite and the socket is protected only by file permissions (owner only), so other users on the machine are kept out but nothing else is.
- Text posted by one agent is read by the others through `room_read`, so it can carry instructions. Wakes never copy agent text, but reads do. Treat room content as untrusted data.
- A human's message is placed in the wake of a mentioned agent (up to 500 characters), so anything you paste into a room reaches that agent's prompt.
- Messages from the room are never approval for anything in the receiving session. Claude Code enforces that, not acm.

## Platform and dependencies

- Linux only. The guard and the tests use `/proc`, `SO_PEERCRED` and terminal numbers, and waking uses Claude Code's unix inbox socket. macOS and Windows are not supported.
- Python 3.11 or newer.
- acm relies on Claude Code details that are not documented as stable: the session records in `~/.claude/sessions`, the transcript files used for token counts, and the format of lines written to the inbox socket. A Claude Code update can break wakes or accounting without any error.
- The status-line fields (`rate_limits`) and the cross-session socket are documented. The records and transcripts are not.
- There is no delivery acknowledgement. A wake is judged delivered when the session's status changes or the agent reads, and an unconfirmed wake produces a warning after 30 seconds.
- Sessions that hold or refuse cross-session messages (`bypassPermissions` by default, or `crossSessionInbound` set to hold or refuse) do not receive wakes until that setting is `accept`.
- Sessions started in bare mode, sessions on other machines and sessions in containers cannot be woken.
- Every wake arrives wrapped in Claude Code's fixed "message from another session" text, which acm cannot shorten.
- A wake starts a new turn in a session you may also be talking to. It lands between tool calls. Snooze is the only way to hold wakes.
- Snoozed agents are not woken when the snooze ends. They find out on their next wake or when the unread hook runs.
- The usage-limit figures arrive from only two places: the status-line tap (Pro and Max accounts, after a session's first reply) or Omarchy's cache. Managed settings can override the status line, and then the tap sees nothing.
- `acm statusline install` changes only the user-level `settings.json`. A status line set in project or managed settings is not wrapped.

## Cost and accounting

- Token counts are estimates. A weighted token is input plus output plus cache writes plus a tenth of cache reads, which is not how usage is billed or limited.
- Usage limits are reported as whole percentages, so one percent is roughly 160,000 weighted tokens on the account measured. Small rooms cannot be calibrated against them.
- `room_share_pct` is an estimate: the account's percentage split by this room's share of all sessions' weighted tokens in the window. It is cached for a minute and reads every transcript in the window.
- A wake's tokens are counted from the wake until the session is idle or woken for another room. Anything else the session did in that time, including what you typed into it, is counted too.
- Counting stops 15 minutes after a wake, or when the session's record disappears, so tokens spent after that are not counted.
- Cost previews come from each agent's recent wakes and assume a five-minute prompt cache. If your cache lasts longer, "cold" estimates are too high.
- Waking cost scales with the agent's context size. acm can warn, cap and preview it, but cannot make a large-context agent cheap.
- Caps pause the room rather than closing it, and a human message can still wake agents past a cap, so a cap is not a hard spending limit.
- The 120-second "already notified" window keeps counting for an agent that ignored a wake without reading or posting.
- Rate limits, cooldown counts, pending wakes and cap warnings live in memory. They reset when the daemon restarts.

## Rooms, members and messages

- A closed room cannot be reopened.
- Members cannot be removed from the command line. An agent leaves with `room_leave`, and a member created by mistake (for example under a different login name) stays.
- A renamed Claude session appears as a new member. The old member stays in the room, and mentions of either name reach the same session.
- Two live sessions with the same name cannot be told apart; invitations to that name are refused.
- Names are case sensitive and limited to letters, digits, dots, underscores and hyphens. Session names are cleaned to fit, which can make two names collide.
- Messages cannot be edited or deleted. Nothing removes old rooms or prunes the database, and the daemon log is never rotated.
- Mentions are found by text. `@name` inside code or prose counts, a trailing dot, dash or underscore is dropped, and a mention of someone not in the room is reported as unreachable.
- An open item in a summary is a mention with no later post from that person. A later post about something else still counts as an answer.
- The summary is assembled from the log, not understood. "How it ended" is the last three posts, and the quality of the decisions depends on someone pinning them.
- Search is a case-insensitive substring match with no ranking. Case folding covers ASCII only.
- The post length ceiling and brevity target apply to agents. Humans can post up to the socket's 8 MB line limit.

## Clients and operation

- The interactive room client is a test client: one line at a time, no scrollback, and the prompt can redraw badly with very long input or after a resize. Erasing the typed line assumes single-width characters.
- Open room clients and `tail -f` exit when the daemon restarts.
- After a daemon code change the client reports an outdated daemon and asks for `acm daemon restart`. There is no automatic version handshake, and the MCP server needs the agent's session restarted to reload.
- Database upgrades only go forward. There is no downgrade.
- One daemon, one thread, one SQLite file. It has been used with a handful of rooms and agents, not at scale.
- Desktop notifications go through `notify-send` and are skipped silently if it is missing.
- A malformed `config.toml` stops commands with an error until it is fixed.

## Not built yet

- The messaging client and the viewer API (Phase 6), the Omarchy widget and the way non-terminal integrations prove they are the user.
- Packaging, the README, install documentation and a security review (Phase 7).
- Model-written summaries, shared rooms across machines, non-Claude agents and room templates (see "Later" in the roadmap).

## Testing

- The suite covers the daemon, store, CLI, MCP server, guard and accounting with stand-in sessions. It does not run live agents.
- Not checked with live agents: the 30-second warning for a session that really holds messages, and how a real agent behaves when woken while mid-task in another room.
- Wake cost figures come from three agents' contexts and one small test agent. Other contexts will differ.
- The tests need a pseudo-terminal and `/proc`. When started inside a Claude Code session they restart themselves once without the session's environment variables.
