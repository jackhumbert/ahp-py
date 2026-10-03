# Changelog

## [Unreleased]

### Added

- `chat_tools` (`--chat-tools`): narrow the web tools a session with no folder
  is offered, e.g. drop WebFetch where sessions cannot reach the internet. It
  can only remove tools from the default (`WebSearch`, `WebFetch`), never add
  one, so a chat still gets nothing that touches the machine.

- Client tools. Tools a client publishes on `activeClient.tools` are offered to
  Claude as an in-process MCP server (`mcp__client__<name>`), and each call runs
  in that client through the host's `run_client_tool`, under Claude's own tool
  call id. Chats get them too, because they touch nothing on this machine. The
  session follows clients joining, leaving and republishing
  (`FollowsActiveClients`; needs ahp-host with it). Who runs a tool changes at
  once. When the set of tools itself changes, Claude restarts on the same
  conversation, straight away if it is idle, otherwise after the current turn.
  Security: these calls skip this host's approval gate. The client that runs a
  tool decides whether it may, which is what AHP means by `confirmed:
  'not-needed'` for client-provided tools.

- Automations: saved prompts that run as new sessions on a schedule or on
  request (AHP 0.9.0), kept under `<state_dir>/automations`. Needs
  ahp-host with `Host(automations=...)`.
- Chats, and folders added later. A session with no folder runs as a chat, in
  an empty directory of the agent's own (`<state_dir>/chat`), with no file,
  shell or MCP tools (`tools` is web search and fetch only,
  `strict_mcp_config`) and a line in the system prompt saying why. The agent
  advertises `multipleWorkingDirectories` (`immutablePrimary`) and follows a
  client adding, removing or replacing a folder mid-session
  (`FollowsWorkingDirectories`, needs ahp-host with it): Claude
  restarts on the same conversation, straight away if idle, else after the
  turn in flight, with the folders granted (`add_dirs`). A session's `cwd` is
  now fixed at its first start and kept in its resume state (`cwd`), because
  Claude Code keeps a conversation under the directory it started in.
  Security: a folderless session used to run in the first served folder with
  every tool (the host filled it in); it now has none that touch the machine.

- The account's other Claude Code sessions, through claude.ai
  (`claude_ai_sessions`, off by default; one machine only). Every live Remote
  Control session - terminal, desktop app, IDE, any machine - is listed, with
  its last exchanges, and followed live; messages, stop and approval answers
  go back through claude.ai's Remote Control endpoints (`claude_ai.py`),
  behind the same client interface as the Agent SDK's, so a mirrored session
  is an ordinary `ClaudeSession`. Uses Claude Code's own login, read from the
  Keychain or `~/.claude/.credentials.json` and never refreshed here.
  claude.ai delivers messages sent with that login as from another Claude
  session. Needs ahp-host with `OpensSessions`.

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
  ahp-host with it). Security: anyone signed in to the machine's
  Claude account can now drive and approve the session; see README.

- `type = "claude"` for `ahp-node` (ahp-host): the package
  registers a `claude` entry in the `ahp_host.agents` group, so one
  node can serve Claude beside other agents (goose, opencode) from one host,
  port and folder tree. Options: `provider_id` (default `claude` -- keep it the
  same on every machine; the gateway merges them and the folder picks the
  machine) and `agent_name`.

- The approval mode follows a switch made on claude.ai (Claude Code's
  `system/status`), and the session setting shows it
  (`SessionPublisher.config_changed`, needs ahp-host with it). A mode
  this adapter does not offer (`bypassPermissions`, `dontAsk`) is not
  followed: the client is put back in Ask. A plan approved on claude.ai drops
  the session to Ask like one approved here, and either way the setting now
  shows Ask rather than still Plan.

- Sessions show up in `claude --resume`: Claude Code records them as
  started by `ahp-host` (`CLAUDE_CODE_ENTRYPOINT`) rather than `sdk-py`,
  which the picker hides. As with a terminal session, Claude Code then also
  offers its claude.ai artifact tools and guide agent; they pass the same
  approval gate as every other tool.

- Deleting a session archives it on claude.ai too (`disposed`, needs
  ahp-host with `DisposesSessions`); shutting the host down still
  leaves it there, offline, for the next start to reattach to.

- Archiving a session in a client (`session/isArchivedChanged`) archives it
  on claude.ai too and stops its Claude process; unarchiving starts it again
  and brings the same claude.ai session back. An archived session is not
  started at restart, which would unarchive it (needs ahp-host with
  `ArchivesSessions`).

### Changed

- **Renamed from `agent-host-server-claude` to `ahp-host-claude`** (import `agent_host_server_claude` → `ahp_host_claude`), and moved into the `ahp-py` monorepo as `packages/ahp-host-claude`. Command: `agent-host-server-claude` → `ahp-host-claude`; the default state directory follows the name. Tags are now per package: `ahp-host-claude/v<version>`.
- `claude_ai_sessions = true` now lists only the sessions running on this
  machine - found in Claude Code's registry here, or by the machine a
  `claude --remote-control` environment names - each in its own folder. Every
  machine running this host can turn it on: each lists its own, a gateway
  files them under the right machine, and none is listed twice. `"all"`
  (`--claude-ai-sessions all`) keeps listing every session on the account,
  for machines that run no host. A mirror found to run elsewhere is closed.
  Mirrors skip this host's folder checks: their folders are their own
  machine's.

- One reader per Claude client: Claude Code's output is read continuously
  rather than per turn, so output nobody here asked for (a turn from
  claude.ai, the aborted result of a stopped turn) can no longer be read as
  the next turn's. Every message sent carries its own uuid, which is how the
  CLI's replays are told apart from messages typed elsewhere.

- The folder tree (`roots`, `paths`) moved to `ahp_host.node`; the
  modules here re-export it. `python -m ahp_host_claude` still runs a
  Claude-only host as before.

- Steering: a message sent while Claude works joins the running turn
  (`ClaudeSession.steer`, sent with Claude Code's `next` priority, so it
  arrives at the next tool boundary). The CLI's `--replay-user-messages` echo
  says when it was taken in; if Claude had already made its last tool call it
  answers straight after, and the turn stays open for that answer.

### Fixed

- **One claude.ai sync at a time.** `sync_claude_ai` is public and the poller
  calls it too; two overlapping calls each saw a restored mirror as not yet
  started and opened it twice. Calls now queue.
- A restart no longer un-archives what was archived on claude.ai. Every
  session with Remote Control on reattached at start-up, and reattaching
  un-archives, so each restart brought back this host's sessions archived in
  the Claude apps. One archived there is now left alone until it is used here
  (a message sent here reattaches, as it should), and a restored mirror
  starts only once claude.ai says it is still active.
- A claude.ai session listed here shows its conversation even after sitting
  idle: its history is paged back past the control and system traffic an idle
  session fills its recent events with (hundreds of events), where only the
  last 100 were read before and often held no message at all. A new listing
  now shows the last five exchanges, not two.
- A session that goes on claude.ai and then sits idle keeps its claude.ai
  session across restarts: its bridge id is saved as soon as it has one.
  Before, the host only saved it on the next turn, so every restart gave such
  a session a new claude.ai session and left the old one behind.
- A session with Remote Control on is reachable again right after a restart:
  the host restores sessions lazily, on their first turn, so until something
  here touched one it was offline on claude.ai. They are now brought back
  when the host hands over its session list.
- Sessions survive a restart: `python -m ahp_host_claude` now calls
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
  small tree - each through ahp-host's jail - with sessions allowed in
  any of them.
- Folder browsing on Windows, using ahp-host's Windows jail when the
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
- `python -m ahp_host_claude`: serve it on loopback with a token read
  from a file.
- Attachments on a user message reach Claude: local files and folders by path
  (with selections), images and PDFs as content blocks, text inline.
- The model picker is read from Claude Code's own list at start-up instead of
  being hard-coded; picking `default` means the account's default.
- `--provider-id`, so several machines can sit behind one gateway as distinct agents.
- Windows support: `file:///C:/...` URIs map to drive paths, start-up no longer
  needs POSIX signal handlers, and folder browsing is disabled (with a warning)
  where the host's jail cannot run. CI covers Linux, macOS and Windows.
