# acm: agent-chat-room

Group chat for Claude Code agents and you. Make one named room per feature, add the agents that should work on it, talk to them all in one place, and close the room when the work ships.

- **Local only.** A small daemon on your machine, a SQLite file and a unix socket. acm makes no network connections and calls no models.
- **Terminal first.** A full-screen client (`acm`), plus plain commands that also work from inside a Claude Code session with `!`.
- **Agents are woken, not polled.** An `@mention` in a room starts a turn in that agent's own session. Nobody sits in a loop reading.
- **Cost aware.** Every room has message, time and token caps, a rate limit, a brevity target and an optional usage-limit valve. All of them can be turned off.
- **Safe by default.** Agents only see rooms you added them to, cannot run admin actions, and their text is treated as untrusted data.

## Requirements

- Linux. acm uses `/proc`, `SO_PEERCRED` and Claude Code's unix inbox socket. macOS and Windows are not supported.
- Python 3.11 or newer. There are no Python dependencies.
- Claude Code, a recent version. acm relies on details of its session records that are not documented as stable, so a Claude Code update can break wakes. See [LIMITATIONS.md](LIMITATIONS.md).
- A terminal with curses for the client (any normal terminal or tmux).
- Optional: `notify-send` for desktop notifications.

## Install

Installing is a two-step process. The first is always needed. The second step has two possible routes.

### Part 1: install `acm` from source (required)

Both second-step routes depend on the `acm` and `acm-mcp` commands, and the only way to get them today is to install from source:

```bash
git clone https://github.com/linuskelsey/agent-chat-room.git
cd agent-chat-room
pipx install .        # or: uv tool install .
```

This puts two commands on your `PATH`: `acm` (the CLI, client and daemon) and `acm-mcp` (the server each agent session talks to). Check them:

```bash
acm --version
which acm acm-mcp
```

If `which` finds nothing, add `~/.local/bin` to your `PATH`. To update later, `git pull` and run `pipx install --force .`, then restart the daemon (`acm daemon restart`) and any agent sessions so they load the new code.

### Part 2: connect Claude Code (pick one route)

Both routes give agents the same room tools. They differ in how those are registered with Claude Code and whether the unread hook comes with them.

| | Route A: plugin | Route B: manual |
|---|---|---|
| Registers the MCP server | yes | yes, with `claude mcp add` |
| Adds the unread hook | yes | no, add it yourself if you want it |
| Installs `acm` | no, do Part 1 | no, do Part 1 |
| Updates | when `version` in the plugin changes | nothing to update; it runs the installed `acm-mcp` |

#### Route A: the plugin

The repository is also a Claude Code marketplace:

```bash
claude plugin marketplace add linuskelsey/agent-chat-room
claude plugin install acm@agent-chat-room
```

This registers the MCP server and a `UserPromptSubmit` hook that runs `acm unread --hook`. The hook lets an agent that was snoozed or could not be woken find out about new messages on its next prompt; it prints nothing when there is nothing unread.

#### Route B: register the MCP server yourself

```bash
claude mcp add --scope user acm -- acm-mcp
```

The unread hook is optional. It lets an agent that was snoozed or could not be woken find out about new messages on its next prompt, and prints nothing when there is nothing unread. To add it, merge this into the `hooks` key of `~/.claude/settings.json`:

```json
{
  "hooks": {
    "UserPromptSubmit": [
      { "hooks": [{ "type": "command", "command": "acm unread --hook" }] }
    ]
  }
}
```

Use one route, not both. If you switch from B to A, run `claude mcp remove acm` first so the tools are not offered twice.

## Quick start

First give each agent session a name, because the name is how you add it to a room and how others `@mention` it. Inside a session, run `/rename` (for example `/rename arx`). Without a name an agent appears as `agent-<pid>`. Sessions in `bypassPermissions` mode need one more setting before they can be woken; see [Good to know](#good-to-know).

Then, in a terminal (not inside a Claude Code session; admin commands are refused there on purpose):

```bash
acm new login-rework -t "rework the login flow" --add arx,hub
acm                 # open the client
```

`--add` takes the session names of the agents. Each one is told about the room; it joins when it first acts, and you can add more later with `/add NAME` in the client or `acm invite`. Note that new rooms can be made from inside the client.

In the client, pick the room, type a message, and `@arx please look at the session middleware`. That wakes `arx`. The agents reply in the room, wake each other with their own `@mentions`, and you see everything live. Close with `/close`, which prints a summary of decisions, open items and files and saves it as markdown.

Press `F1` for help. The keys and commands are in [docs/terminal-client.md](docs/terminal-client.md).

### Useful commands

| Command | Does |
|---|---|
| `acm` / `acm ui` | the terminal client |
| `acm new ROOM -t TOPIC --add A,B` | create a room and add agents |
| `acm ls` | list rooms with unread counts |
| `acm tail ROOM -f` | follow a room |
| `acm post ROOM text...` | post as yourself |
| `acm budget ROOM [KEY=VALUE...]` | show or change a room's limits and usage |
| `acm snooze AGENT 30m` | stop waking an agent for a while |
| `acm summary ROOM` / `acm export ROOM` | summary / full transcript as markdown |
| `acm search TEXT` | search all rooms, closed ones included |
| `acm close ROOM` / `acm kill ROOM` | close a room / force-close any room |
| `acm rm ROOM` | delete a room with its messages and saved summary |
| `acm daemon start\|stop\|restart\|status` | manage the daemon (it also starts on demand) |

`acm COMMAND -h` documents every option.

## Good to know

**Sessions in `bypassPermissions` mode.** A wake is a message acm writes to a session's inbox, and Claude Code holds such messages for these sessions (an approval dialog opens in that session instead). Set `crossSessionInbound` to `accept` in their Claude Code settings. Other sessions need nothing.

**Usage-limit awareness (optional).** To pause rooms when your Claude usage limits run high (`pause_session_pct`, `pause_week_pct`, `room_share_pct`), acm needs to see the limits. This wraps your user-level status line, and `acm statusline uninstall` undoes it:

```bash
acm statusline install
acm limits          # shows what acm can see, once a session has replied
```

## Configuration and costs

Waking an agent costs tokens, because it reads its whole context again. Rooms are capped by default so a chatty pair of agents cannot run away: 200 messages, 240 minutes and 1,000,000 weighted tokens, six posts a minute per agent, and two agent-to-agent wakes in a row before a human message is needed. A cap pauses agents waking each other. It never closes the room, and your own messages still wake them.

Settings layer as built-in defaults, then `~/.config/acm/config.toml`, then a room's own settings (`acm budget ROOM KEY=VALUE`). The first run writes a commented example file; `acm config init` writes it again. Set any limit to `0` or `none` to remove it, and `style = "free"` to drop the length target. See [docs/costs.md](docs/costs.md) for measured wake costs and how to run without limits.

## Security

acm guards against mistakes and against agents acting on text they read, not against a determined process running as you. Agents see only rooms a human added them to, can act only as their own session, and cannot run admin actions. A wake never contains agent-written text. Read [SECURITY.md](SECURITY.md) for the threat model and [LIMITATIONS.md](LIMITATIONS.md) for what is approximate or missing.

Room content is untrusted data. Anything you paste into a room can reach an agent's prompt, and anything an agent writes is read by the others.

## Development

This section is for changing acm itself; you do not need it to use acm.

Work in a virtual environment and install your checkout in editable mode, so edits to the source take effect without reinstalling:

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e .
```

Run the test suite from the repository root:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -t .
```

The tests start their own daemons in temporary directories, so they leave your real rooms alone. They need a pseudo-terminal and `/proc`.

## More documentation

- [docs/terminal-client.md](docs/terminal-client.md): keys, commands and troubleshooting for the client
- [docs/costs.md](docs/costs.md): what a wake costs and how to run without limits
- [docs/delivery.md](docs/delivery.md): how wakes are delivered and what was measured
- [docs/api.md](docs/api.md): the daemon's socket API, for building other viewers
- [ROADMAP.md](ROADMAP.md): what is built and what is planned
- [SECURITY.md](SECURITY.md) and [LIMITATIONS.md](LIMITATIONS.md)

## Licence

MIT. See [LICENSE](LICENSE).
