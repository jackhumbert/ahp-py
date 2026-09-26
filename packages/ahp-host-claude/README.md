# agent-host-server-claude

Claude as an [Agent Host Protocol](https://github.com/jackhumbert/agent-host-protocol-py)
(AHP) provider for [`agent-host-server`](https://github.com/jackhumbert/agent-host-server-py).

It wraps the [Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk):
Claude Code, running on this machine as the user who starts the host, with its
built-in tools (read, search, edit, shell, web). Any AHP client - VS Code's
Agent Sessions view, the Python client, a broker in front of several hosts -
can then start and follow Claude sessions here.

Status: pre-alpha.

## What a client sees

- Streaming text and reasoning, token usage, and tool calls as rows that say
  what they do: Claude's own description of a shell command while it runs
  (the command itself is in the call's input), then "Ran `…`", "Read a.py",
  "Edited core.py" and so on once it finishes.
- **Approvals**, chosen per session when it is created (the `permissionMode`
  session setting, which VS Code draws with its own icons):
  - **Ask** (`default`, the default): reading and searching run freely; every edit, shell
    command and web request is put to the user first, through the protocol's
    tool confirmation (an approval prompt in VS Code). This holds even if the
    user's own Claude Code settings allow a tool: the gate is a `PreToolUse`
    hook, which runs before those settings are consulted.
  - **Accept edits**: Claude Code's `acceptEdits` mode; file edits in the
    working directory run without asking, shell and web still ask.
  - **Auto**: Claude Code's auto mode; its classifier approves what it judges
    safe and blocks what it judges risky, and only asks when it cannot decide.
    Commands can run on this machine with nobody seeing them first.
  - **Plan**: Claude Code's plan mode; Claude researches without changing
    anything (apart from Claude Code's own plan file under `~/.claude/plans`),
    then shows its plan and asks to start. Approving drops the session to Ask,
    so the work itself is still approved call by call.

  The mode can be switched during a session (in VS Code or the iOS app); the
  running Claude client follows, and the choice survives a host restart.
  Needs agent-host-server with `ReconfiguresSessions` and working session
  resume. A switch made on claude.ai (Remote Control) is followed here too,
  and the setting shows it - except to a mode not listed above (such as
  bypass permissions), which puts the session back in Ask instead.
- **Steering**: a message sent while Claude is working joins the turn at
  its next tool call (Claude Code's own "next" queue slot) instead of waiting
  for it to finish; queued messages still run afterwards.
- **Remote Control**, like a terminal session: each session is also on
  claude.ai and in the Claude apps, so it can be read, driven and approved from
  a phone. On by default when Claude Code's would be (your
  `remoteControlAtStartup` setting, then org policy); `remote_control` in the
  config overrides that for new sessions, and the `remoteControl` session
  setting switches it per session. A message sent from the phone shows up here
  as a turn of its own. An approval goes to both places, and whichever answers
  first wins; the other prompt is withdrawn. Such a session keeps its Claude
  process running while the host is up, so it stays reachable, and a host
  restart reattaches to the same claude.ai session. Archiving the session here
  archives it there (and stops its Claude process), unarchiving brings it
  back, and deleting it here archives it there.
- **The account's other Claude Code sessions** (opt-in, `claude_ai_sessions`):
  every live Remote Control session on the Claude account - a terminal, the
  desktop app, an IDE, on any machine - is listed here too, through the same
  claude.ai endpoints the Claude apps use. Each shows its last couple of
  exchanges, then follows along live; titles and busy/idle follow claude.ai.
  A message sent from here runs there, stop stops it, and its approval
  prompts can be answered here (or there; the other side's prompt is
  withdrawn). One whose machine goes to sleep stays listed; one archived on
  claude.ai goes; one deleted here is not listed again. Turn it on for **one**
  machine: every machine that has it lists every session.

  It signs in as Claude Code on this machine does, reading its login (the
  macOS Keychain, or `~/.claude/.credentials.json`) and never refreshing it,
  since Claude Code's refresh tokens rotate - Claude Code keeps it fresh as it
  runs. claude.ai delivers what that login sends as coming from *another
  Claude session*, not from you: Claude acts on it, but will not take it as
  your OK for a pending prompt (answer the prompt itself instead) or as
  permission to change its own settings. None of these endpoints is
  documented; a Claude Code update can change them.
- **In `claude --resume`**, like any other conversation on the machine:
  sessions are recorded as started by `agent-host`, not by the SDK, whose
  sessions the picker hides. Claude Code then offers them what it offers a
  terminal session (claude.ai artifacts among them), each tool behind the same
  approval gate.
- **Continue from** another Claude Code conversation on this machine - one
  started in a terminal, the IDE, or driven from a phone through Remote
  Control - picked from a searchable list (the `continueFrom` session setting,
  limited to conversations whose folder is inside `--root`). The original is
  forked, never resumed directly, so it is untouched even if it is still open
  elsewhere; the new session runs in its folder with the full context, and the
  first reply opens with a short recap.
- A model picker populated from Claude Code itself at start-up (the same list
  `/model` shows for the logged-in account, its default first), so new models
  appear without a release of this adapter.
- Attachments: a referenced file or folder is handed to Claude as its path
  (with the selected lines, if any); a pasted image or PDF is sent as an image
  or document block; pasted text is inlined (capped at 200k characters).
  Chat and annotation references are named but not resolved yet.
- Sessions that survive a host restart: the Agent SDK's session id is the
  host's resume state.

## Run it

```bash
pip install -e .
python -m agent_host_server_claude --config ~/.config/agent-host/node.toml
```

The config file (TOML; flags override it):

```toml
agent_name = "Claude"
token_file = "~/.config/agent-host/node.token"

# Several named folders: clients see file:///llm/..., file:///projects/...
# (behind a broker: <machine>/llm/..., <machine>/projects/...)
[roots]
llm = 'G:\llm'
projects = 'C:\Users\me\projects'

# ...or one unnamed folder, served as itself:
# root = "~/Github"
```

Use single-quoted TOML strings for Windows paths. Unknown settings are an
error, so a typo cannot silently fall back to a default.

| Setting / flag | Meaning |
|---|---|
| `[roots]` / `--root NAME=PATH` (repeatable) | Named folders. Clients browse (read-only) a small tree of them; sessions may work in any. Nothing else on the machine is reachable. A folderless session starts in the first. |
| `root` / `--root PATH` | One unnamed folder, served as itself. |
| `token_file` / `--token-file` | Require this connection token. Read from a file so it never appears in `ps` or logs. |
| `port`, `bind` / `--port`, `--bind` | Default `127.0.0.1:4321`. Loopback only. |
| `state_dir` / `--state-dir` | Persisted sessions and sequence counter. Default `~/.local/state/agent-host-server-claude`. |
| `agent_name` / `--agent-name` | What clients call the agent. |
| `remote_control` / `--[no-]remote-control` | Put new sessions on claude.ai (Remote Control). Default: whatever Claude Code does, i.e. your `remoteControlAtStartup` setting. |
| `claude_ai_sessions` / `--[no-]claude-ai-sessions` | Also list the account's other Remote Control sessions, through claude.ai (default off). On one machine only. |
| `provider_id` / `--provider-id` | The agent's id (default `claude`). Machines behind one broker share it: the broker merges them into one agent and the folder picks the machine. |

Authentication is Claude Code's own: the SDK uses whatever login `claude` has
on this machine (or `ANTHROPIC_API_KEY` if set).

## Security

The agent runs as the host's OS user, so whatever that user can touch, an
approved tool call can touch. The approval gate and the `--root` check are the
boundary; see `agent-host-server`'s SECURITY.md for the host's own posture.
Never bind this off loopback without a proxy that authenticates peers.

With Remote Control on, the session is also reachable through the Claude
account this machine is signed in to: anyone signed in to it, on any device,
can send the session messages and answer its approval prompts. The approval
mode still applies - a phone answers the same prompts a client here would -
but "a human approved this" then means "someone signed in to that account".
They can also switch the approval mode, to any of the four above; the
session follows it (bypass permissions and anything else is refused and
reset to Ask), and a plan approved there drops the session to Ask exactly as
one approved here does.
With `claude_ai_sessions`, whoever can reach this host can also drive every
Remote Control session on that account, on every machine it runs on - read
them, message them and answer their approval prompts. Treat such a host as
holding the account.

Turn it off (`remote_control = false`, or per session) where that is not the
same set of people as those who can reach this host.

## Development

```bash
python -m venv .venv
.venv/bin/pip install -e . && .venv/bin/pip install --group dev
.venv/bin/pytest && .venv/bin/mypy && .venv/bin/ruff check . && .venv/bin/ruff format --check .
```

## License

MIT - see [LICENSE](LICENSE).
