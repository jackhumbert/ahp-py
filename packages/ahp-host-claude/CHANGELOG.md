# Changelog

## [Unreleased]

### Added

- Chats (`HostsChats`, `CancelsChats`; `multipleChats` with `fork` and
  `sideChat`). Each chat is its own Claude Code conversation on its own
  process, routed by `UserMessage.chat_uri`, with its own transcript marks,
  rewinds and resume state (`chats` in the resume state); restored chats
  resume lazily, a closed chat's process stops, stopping one chat stops only
  it. A fork or side chat starts from the source chat's conversation cut at
  its turn and copied (`resume_session_at`, `fork_session`); a source that
  cannot be cut is given as its transcript. `createSession.fork` the same.
  Approval mode, effort, folders, clients and customizations are the
  session's; Remote Control is the default chat's. A claude.ai mirror refuses
  `chat_opened` (`PermissionDenied`).
- Chat folder subsets (`FollowsChatWorkingDirectories`). Security: a chat
  narrowed to no folder gets no file or shell tools; one that leaves out the
  session's own folder runs its gate in Ask whatever the approval mode,
  because Claude Code's looser modes act in its `cwd` unasked.
- Approval prompt choices. Claude Code's suggestions for a call become
  `ConfirmationOption`s - allow a rule for the session, switch to Accept edits
  or Auto - applied (`updated_permissions`) only when the user picks one.
  Security: rules are narrowed to the session, never the settings files;
  rules are not offered in Ask, where they would not take; bypass permissions
  and directories are never offered. A picked mode moves the gate at once.
- Edit previews on approval prompts (`ToolConfirmation.edits`) for `Write`,
  `Edit` and `MultiEdit`, computed only when exact; per-call diffs
  (`TurnSink.file_edit`) for all four editing tools, from disk before and
  after the call; a reviewable changeset of everything Claude edited, for the
  session and for each other chat. Shell-made edits are not tracked.
- Resumable turns (`ResumesTurns`): a failure on an overloaded, rate-limited
  or failing API, or the Claude process ending, is offered for resuming;
  `resume_turn` asks Claude to carry on in the same turn.
- Live agent info (`UpdatesAgentInfo`): learned model limits are published
  when learned, and start-up discovery that found no models is retried with
  backoff (`rediscover=`).
- System notifications for compactions, messages from another device, and
  background work ending mid-turn.
- Chat attachments (`UserMessage.attached_chats`) reach Claude as the
  referenced chat's transcript.

- `AskUserQuestion`. Claude's multiple-choice question tool is offered again:
  its questions become one input request (`TurnSink.request_input`,
  `chat/inputRequested`) - single or multiple choice with free-form answers
  allowed, text, or number - and the answers go back as the tool's
  `updatedInput.answers`, the way Claude Code's own dialog returns them.
  Declining or dismissing denies the tool with a message saying which.
  Security: the `PreToolUse` hook now sends this tool to the approval callback
  in every mode, so an `allow` rule cannot run it with nobody asked; it asks
  more, never less.
- Model limits and vision. Each picker entry carries `maxContextWindow` and
  `maxPromptTokens`, which start-up discovery asks Claude Code for, model by
  model (`get_context_usage` in its `summary` form, on the idle probe), and
  `maxOutputTokens` once a session's results have reported it (kept in
  `<state_dir>/claude-models.json`). Every entry has `supportsVision`. No
  table of models is kept here.
- Customizations: the session's skills, agents, plugins and MCP servers are
  published as the protocol's two-level customization tree once the Claude
  process is up (`get_server_info`, `get_context_usage`, `get_mcp_status`,
  `system/init`), and kept current (`system/commands_changed`, MCP status after
  each turn, sent as `mcp_server_changed` when only a server's state moved).
  `DescribesSession`, `ManagesMcpServers` (start reconnects, or switches back
  on, a server; stop switches it off, as `/mcp` does, for the project) and
  `HandlesCustomizations` (a skill or a plugin's skills switched off become
  `Skill(name)` deny rules, kept with the session; what Claude Code cannot
  switch off is put back).
- Custom agents. A message's `AgentSelection` runs Claude Code as that agent
  (`--agent`), restarting on the same conversation when the pick changes.
  Security: the permission mode Claude Code reports at `system/init` is put
  back to the session's own if anything (an agent's file) changed it.
- Edit-and-resend (`TruncatesHistory`). Each turn's prompt and last
  transcript entry are recorded (`turns` in the resume state); truncating
  restarts Claude at the turn kept (`resume_session_at`, with
  `resume_drops_turn` when exactly one turn goes, retried without it if Claude
  Code refuses), on a branch of the same conversation; a pending cut survives a
  restart. Truncating everything, or to a turn not recorded, starts a new
  conversation. Files are not rewound; the next prompt tells Claude so.
- Completions (`Completes`): `/` at the start of a message for Claude Code's
  slash commands and skills, `@` for files from its own file index
  (`file_suggestions`), inside the served folders. `python -m
  ahp_host_claude` advertises the trigger characters
  (`ClaudeProvider.completion_trigger_characters`).

- Background shells (AHP 1.0.0 chat background work). A `Bash` command left
  running in the background is listed on the chat with its command line while
  it runs, and withdrawn when Claude Code reports it finished, failed or
  stopped, or when the Claude process goes.
- Background subagents. A subagent running in the background gets a worker
  chat of its own: what it says and the tools it calls stream there, even
  after the turn that started it has ended -- they used to be dropped. It is
  listed as background work pointing at that chat, the spawning call's result
  links to it, and stopping the chat's turn stops that subagent alone.

- `effort`, a session setting (Default, Low, Medium, High, Extra high, Max)
  passed to Claude Code at start-up; changing it mid-session restarts Claude
  on the same conversation. Kept in the resume state when not the default.

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

- A denial's reason and suggestion reach Claude (`reason_message`,
  `user_suggestion`), instead of a fixed "the user declined".
- `AskUserQuestion` answered on claude.ai is withdrawn here with those answers
  (`ResolvesInput`, `InputRequest.key`).
- Turn ids come from `IdentifiesTurn`; the host sink's private attribute is no
  longer read.
- Every customization states whether it is on, so the host never keeps a
  client's toggle over what the agent is actually held to.
- Completion trigger characters are the provider's own
  (`DeclaresCompletionTriggers`), so `ahp-node` advertises them too;
  `python -m ahp_host_claude` no longer passes them to `Host`.
- Usage is reported per turn the way a context gauge reads it: `inputTokens`
  is the prompt of the turn's last request with its cached parts (it was the
  uncached part, summed over every request), `cacheReadTokens` the cached
  part of it, and `_meta` has the cache-creation tokens, the turn's summed
  totals, its estimated cost and the running total.
- Subagents in the foreground get a worker chat too, like background ones:
  their messages, tool calls and approvals are in it, no longer inline in the
  parent turn, and the spawning call's result links to it. A subagent's
  approval prompts are asked in its own chat, so a background one can be
  approved with no turn running in the parent. Subagents' text reaches their
  chat (`forward_subagent_text`).

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
