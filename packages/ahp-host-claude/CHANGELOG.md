# Changelog

## [Unreleased]

### Added

- Remote Control: sessions are also on claude.ai and in the Claude apps, like
  terminal sessions, when Claude Code's own would be (`remoteControlAtStartup`,
  read from the CLI at start-up). Override it with `remote_control` in the
  config, `--[no-]remote-control`, the node's `remote_control` option, or per
  session with the `remoteControl` setting (changeable mid-session). A session
  with it starts its Claude client at once, so it is reachable before its first
  message here, and a restart reattaches to the same claude.ai session
  (`bridgeSessionId` in the resume state). A message sent from the phone opens
  a turn on the host (`external_turn`); an approval answered on the phone
  withdraws the prompt here (`TurnSink.tool_call_confirmed`, needs
  agent-host-server with it). Security: anyone signed in to the machine's
  Claude account can now drive and approve the session; see README.

- `type = "claude"` for `agent-host-node` (agent-host-server): the package
  registers a `claude` entry in the `agent_host_server.agents` group, so one
  node can serve Claude beside other agents (goose, opencode) from one host,
  port and folder tree. Options: `provider_id` (default `claude` -- keep it the
  same on every machine; the broker merges them and the folder picks the
  machine) and `agent_name`.

### Changed

- One reader per Claude client: Claude Code's output is read continuously
  rather than per turn, so output nobody here asked for (a turn from
  claude.ai, the aborted result of a stopped turn) can no longer be read as
  the next turn's. Every message sent carries its own uuid, which is how the
  CLI's replays are told apart from messages typed elsewhere.

- The folder tree (`roots`, `paths`) moved to `agent_host_server.node`; the
  modules here re-export it. `python -m agent_host_server_claude` still runs a
  Claude-only host as before.

- Steering: a message sent while Claude works joins the running turn
  (`ClaudeSession.steer`, sent with Claude Code's `next` priority, so it
  arrives at the next tool boundary). The CLI's `--replay-user-messages` echo
  says when it was taken in; if Claude had already made its last tool call it
  answers straight after, and the turn stays open for that answer.

### Fixed

- Sessions survive a restart: `python -m agent_host_server_claude` now calls
  `Host.restore()` at start-up. They were saved to `--state-dir` but never read
  back, so every restart emptied the session list.

### Added

- The approval mode (`permissionMode`) can change mid-session: it is now
  `sessionMutable`, and `ClaudeSession.config_changed` moves the `PreToolUse`
  gate first and then the running client (`set_permission_mode`), so a failed
  switch leaves the stricter behaviour in force. Resuming prefers the
  session's current config over the resume state.
- A TOML config file (`--config`); flags override it, unknown settings are an
  error.
- Several named folders per host (`[roots]` / `--root NAME=PATH`), served as a
  small tree - each through agent-host-server's jail - with sessions allowed in
  any of them.
- Folder browsing on Windows, using agent-host-server's Windows jail when the
  installed server has it.
- Tool calls say what they do. The line under a running call is Claude's own
  description for shell commands ("Count tracked files") or e.g. "Read file: a.py",
  instead of the host's fallback "Running Run command"; a finished call reads
  "Ran `git ls-files | wc -l`" or "Failed: Read file: a.py" instead of "Done".
- A `continueFrom` session setting: continue any of this machine's Claude Code
  conversations (terminal, IDE, Remote Control) by forking it; searchable, and
  confined to `--root`.
- A `permissionMode` session setting: Ask (`default`), Accept edits, Auto
  (Claude Code's auto mode) or Plan (plan first; approving drops to Ask), named as Claude Code names them so VS Code shows
  its icons. Kept across host restarts in the resume state.
- `ClaudeProvider`: Claude Code (via the Claude Agent SDK) as an AHP provider,
  with streamed text and reasoning, tool calls, usage, and resumable sessions.
- Approval policy: read-only tools run freely; edits, shell and web tools are
  confirmed by a client first, regardless of the user's Claude Code settings.
- `python -m agent_host_server_claude`: serve it on loopback with a token read
  from a file.
- Attachments on a user message reach Claude: local files and folders by path
  (with selections), images and PDFs as content blocks, text inline.
- The model picker is read from Claude Code's own list at start-up instead of
  being hard-coded; picking `default` means the account's default.
- `--provider-id`, so several machines can sit behind one broker as distinct agents.
- Windows support: `file:///C:/...` URIs map to drive paths, start-up no longer
  needs POSIX signal handlers, and folder browsing is disabled (with a warning)
  where the host's jail cannot run. CI covers Linux, macOS and Windows.
