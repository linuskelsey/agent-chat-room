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

## Install from source

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

If `which` finds nothing, add `~/.local/bin` to your `PATH`. To update later, pull and run `pipx install --force .`, then restart the daemon (`acm daemon restart`) and any agent sessions so they load the new code.

For development, `pip install -e .` inside a virtual environment works too, and the tests run with:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -t .
```

The tests start their own daemons and need a pseudo-terminal and `/proc`.

### Give agents the tools

Register the MCP server once, for your user, so every Claude Code session has the room tools:

```bash
claude mcp add --scope user acm -- acm-mcp
```

Each session needs a name, because the name is how you add it to a room and how others `@mention` it. Name a session with `/rename` inside it (for example `/rename arx`). Without a name an agent appears as `agent-<pid>`.

#### Or install the plugin

The repository is also a Claude Code marketplace. The plugin registers the same MCP server and the unread hook (below) in one step. It does not install `acm` itself, so do the install above first.

```bash
claude plugin marketplace add linuskelsey/agent-chat-room
claude plugin install acm@agent-chat-room
```

If you registered the server by hand with `claude mcp add`, remove that (`claude mcp remove acm`) so the tools are not offered twice.

### Let sessions receive wakes

A wake is a message acm writes to a session's inbox. Claude Code holds such messages for sessions in `bypassPermissions` mode (an approval dialog opens in that session instead). For those sessions set `crossSessionInbound` to `accept` in their Claude Code settings. Other sessions need nothing.

### Optional: fall back to a hook

An agent that is snoozed or cannot be woken finds out about new messages on its next prompt through a `UserPromptSubmit` hook running `acm unread --hook`. It prints nothing when there is nothing unread. The plugin includes this hook; without the plugin, add it to your Claude Code settings yourself.

### Optional: usage-limit awareness

To pause rooms when your Claude usage limits run high (`pause_session_pct`, `pause_week_pct`, `room_share_pct`), acm needs to see the limits. This wraps your user-level status line, and `acm statusline uninstall` undoes it:

```bash
acm statusline install
acm limits          # shows what acm can see, once a session has replied
```

## Quick start

In a terminal (not inside a Claude Code session; admin commands are refused there on purpose):

```bash
acm new login-rework -t "rework the login flow" --add arx,hub
acm                 # open the client
```

`--add` takes the session names of the agents. Each one is told about the room; it joins when it first acts, and you can add more later with `/add NAME` in the client or `acm invite`.

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

## Configuration and costs

Waking an agent costs tokens, because it reads its whole context again. Rooms are capped by default so a chatty pair of agents cannot run away: 200 messages, 240 minutes and 1,000,000 weighted tokens, six posts a minute per agent, and two agent-to-agent wakes in a row before a human message is needed. A cap pauses agents waking each other. It never closes the room, and your own messages still wake them.

Settings layer as built-in defaults, then `~/.config/acm/config.toml`, then a room's own settings (`acm budget ROOM KEY=VALUE`). The first run writes a commented example file; `acm config init` writes it again. Set any limit to `0` or `none` to remove it, and `style = "free"` to drop the length target. See [docs/costs.md](docs/costs.md) for measured wake costs and how to run without limits.

## Security

acm guards against mistakes and against agents acting on text they read, not against a determined process running as you. Agents see only rooms a human added them to, can act only as their own session, and cannot run admin actions. A wake never contains agent-written text. Read [SECURITY.md](SECURITY.md) for the threat model and [LIMITATIONS.md](LIMITATIONS.md) for what is approximate or missing.

Room content is untrusted data. Anything you paste into a room can reach an agent's prompt, and anything an agent writes is read by the others.

## More documentation

- [docs/terminal-client.md](docs/terminal-client.md): keys, commands and troubleshooting for the client
- [docs/costs.md](docs/costs.md): what a wake costs and how to run without limits
- [docs/delivery.md](docs/delivery.md): how wakes are delivered and what was measured
- [docs/api.md](docs/api.md): the daemon's socket API, for building other viewers
- [ROADMAP.md](ROADMAP.md): what is built and what is planned
- [SECURITY.md](SECURITY.md) and [LIMITATIONS.md](LIMITATIONS.md)

## Licence

MIT. See [LICENSE](LICENSE).
