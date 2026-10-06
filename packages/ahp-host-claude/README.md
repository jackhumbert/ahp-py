# ahp-host-claude

Claude as an [Agent Host Protocol](https://github.com/jackhumbert/ahp-py/tree/main/packages/ahp-protocol)
(AHP) provider for [`ahp-host`](https://github.com/jackhumbert/ahp-py/tree/main/packages/ahp-host).

It wraps the [Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk):
Claude Code, running on this machine as the user who starts the host, with its
built-in tools (read, search, edit, shell, web). Any AHP client - VS Code's
Agent Sessions view, the Python client, a gateway in front of several hosts -
can then start and follow Claude sessions here.

Status: pre-alpha.

## What a client sees

- Streaming text and reasoning, token usage, and tool calls as rows that say
  what they do: Claude's own description of a shell command while it runs
  (the command itself is in the call's input), then "Ran `…`", "Read a.py",
  "Edited core.py" and so on once it finishes.
- **Token usage per turn**, shaped for a context gauge: `inputTokens` is the
  prompt of the turn's last request, cached parts included - how full the
  context window is - and `cacheReadTokens` the cached part of it;
  `outputTokens` is what the turn generated. `_meta` carries the rest:
  `cacheCreationTokens`, the turn's summed totals (`turnTotals`), the turn's
  estimated cost (`costUsd`) and the session's running total (`totalCostUsd`),
  and the model's `contextWindow` and `maxOutputTokens` as Claude Code reported
  them.
- **Questions**: when Claude asks you something with its multiple-choice tool
  (`AskUserQuestion`), the questions are put to you as an input request -
  single or multiple choice, with room to type your own answer, or a text or
  number field - and your answers go back to Claude. Declining or dismissing
  them tells Claude so, and the turn carries on. Under Remote Control the
  questions are asked on claude.ai too; answered there, the request here is
  closed with the answers given there, so the transcript shows them and no
  one can answer twice.
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

  An approval prompt also shows:
  - **what an edit will do**, as a diff to open before deciding: `Write`,
    `Edit` and `MultiEdit` against the file as it is on disk, when the edit
    can be applied exactly as Claude Code would (inside the served folders,
    up to 2 MiB);
  - **choices beyond yes and no**, from Claude Code's own suggestions for the
    call: allow a rule (`Bash(npm test:*)`) for the rest of the session, or
    switch to Accept edits or Auto from now on. Nothing is applied unless you
    pick it; every rule is narrowed to the session, so a client here never
    writes your Claude Code settings files; rules are not offered in Ask,
    where every change is put to you whatever the rules say; and bypass
    permissions and extra directories are never offered.

  Saying no can carry a reason and a suggestion of what to do instead; both
  go to Claude with the denial, so it does not try the same thing again.

  The mode can be switched during a session (in VS Code or the [iOS app](https://github.com/jackhumbert/ahp-client-ios)); the
  running Claude client follows, and the choice survives a host restart.
  Needs ahp-host with `ReconfiguresSessions` and working session
  resume. A switch made on claude.ai (Remote Control) is followed here too,
  and the setting shows it - except to a mode not listed above (such as
  bypass permissions), which puts the session back in Ask instead.
- **Effort**, a session setting (`effort`: Default, Low, Medium, High, Extra
  high, Max), passed to Claude Code at start-up. Claude Code cannot change it
  on a running client, so a change mid-session restarts Claude on the same
  conversation - straight away if idle, else after the turn in flight. Which
  levels a model takes is in its `supportedEffortLevels` metadata on the model
  list, for a client to offer only those.
- **Background shells**: a command Claude leaves running in the background is
  listed on the chat (AHP 1.0.0 background work) with its command line until it
  finishes or is stopped, so a client can show what is still going after the
  turn has ended.
- **Subagents** get a read-only worker chat of their own, in the foreground or
  the background: their messages, tool calls and approval prompts are there,
  not inline in the turn that started them, and the spawning call's result
  links to the chat. One running in the background is also listed as
  background work pointing at its chat, and its messages keep streaming there
  after the turn that started it has ended. Stopping a worker chat's turn stops
  that subagent alone.
- **Diffs and a changeset.** Each completed `Write`, `Edit`, `MultiEdit` and
  `NotebookEdit` call carries its diff (`fileEdit`): the file just before the
  call ran and just after, read from disk inside the served folders (up to
  2 MiB). The session lists one reviewable changeset of every file Claude
  edited with them, from before its first edit to after its latest; another
  chat's edits are also in a changeset of that chat's own. A file a shell
  command changes is not shown: nothing says which files a command touched.
- **Chats**: a session can have several (`createChat`), each its own Claude
  Code conversation on its own Claude process, with its own history,
  rewinds and resume state; stopping one stops only that one, and a background
  task reporting back after its turn ends opens a turn in the chat that
  started it. A **fork** starts
  from a copy of the source chat's conversation cut at the turn it forked at,
  so the source is untouched; a **side chat** the same, without the source's
  turns in its own history. A source that cannot be cut there (its turns
  predate this adapter's marks, or it is not running here) is handed to Claude
  as its transcript instead. Forking a whole session (`createSession.fork`)
  works the same way. The approval mode, effort, folders and customizations
  are the session's and apply to every chat; Remote Control is the default
  chat's only, and a session listed through claude.ai cannot open a second
  chat. A chat can be narrowed to some of the session's folders, or none: with
  none it gets no file or shell tools, and one that leaves out the session's
  own folder (where Claude Code works from) asks before every change whatever
  the approval mode.
- **Resuming** a turn that failed on something transient - an overloaded or
  rate-limited API after Claude Code's own retries, a server error, the Claude
  process ending - is offered (`chat/turnResume`). Resuming starts Claude Code
  again on the conversation if need be and asks Claude to carry on; what it
  says is added to the same turn. A failure the same request would hit again
  (a refused login, a bad request) is not offered.
- **Notes in the transcript** (system notifications) for what the harness did
  rather than Claude: a compaction, a message sent from another device, a
  background shell finishing while a turn runs.
- **Customizations**: the session's skills, agents, plugins and MCP servers,
  as Claude Code reports them once its process is up, published as the
  protocol's customization tree - a `plugin` per plugin, `directory` entries
  for your skills and agents (`~/.claude/skills`, `~/.claude/agents`) and the
  project's (`.claude/skills`, `.claude/agents`), and MCP servers with their
  state, which follows Claude Code's. Claude Code's built-in skills and agents
  are not listed. A client can:
  - start or stop an MCP server, or switch it on or off - which, as with
    Claude Code's own `/mcp`, applies to the project in every session;
  - switch a skill, or a plugin's skills, off: they become deny rules
    (`Skill(name)`) the Skill tool refuses, and Claude restarts on the same
    conversation to take them in (after the turn in flight, if any). Agents
    and directories cannot be switched off in Claude Code; the toggle is put
    back;
  - pick a custom agent for a message (`AgentSelection`): Claude Code runs as
    that agent (`--agent`), restarting on the same conversation when the pick
    changes. Whatever permission mode the agent's own file sets, the session's
    approval mode is put back.
- **Edit-and-resend** (`chat/truncated`): the turns a client takes back are
  forgotten by Claude too. Each turn's place in Claude Code's transcript is
  kept with the session, and Claude restarts at the turn kept
  (`resume_session_at`) on a branch of the same conversation. Files are not
  rewound - Claude Code can only undo its own edits, not a shell command's, and
  would also undo changes made by hand since - so the next prompt tells Claude
  the conversation was rewound and the files were not. A turn from before this
  was kept cannot be found, so the whole conversation is forgotten instead.
  A session listed through claude.ai (`claude_ai_sessions`) runs elsewhere and
  cannot be rewound from here, so the host refuses to truncate it.
- **`/` and `@` completions**: `/` at the start of a message offers Claude
  Code's slash commands and skills for the session (not those bound to its
  terminal); `@` offers files and folders from Claude Code's own file index,
  inside the served folders, as resource attachments. The provider declares
  its trigger characters (`completion_trigger_characters`), which ahp-host
  advertises, under `python -m ahp_host_claude` and `ahp-node` alike.
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
- **This machine's other Claude Code sessions** (opt-in, `claude_ai_sessions`):
  every live Remote Control session running on this machine - in a terminal,
  the desktop app, an IDE - is listed here too, in its own folder, through the
  same claude.ai endpoints the Claude apps use. Each shows its last five
  exchanges, then follows along live; titles and busy/idle follow claude.ai.
  A message sent from here runs there, stop stops it, and its approval
  prompts can be answered here (or there; the other side's prompt is
  withdrawn). One whose Claude Code quits stays listed; one archived on
  claude.ai goes; one deleted here is not listed again.

  Turn it on for every machine that runs this host: each lists the sessions
  running on it, so a gateway files them under the right machine and none is
  listed twice. A session is this machine's if Claude Code's registry here
  names it (`~/.claude/sessions`), or if it was started with
  `claude --remote-control` and its claude.ai environment names this machine.
  For machines that run no host, `claude_ai_sessions = "all"` on *one*
  machine lists every session on the account (without their folders, which
  are on other machines).

  It signs in as Claude Code on this machine does, reading its login (the
  macOS Keychain, or `~/.claude/.credentials.json`) and never refreshing it,
  since Claude Code's refresh tokens rotate - Claude Code keeps it fresh as it
  runs. claude.ai delivers what that login sends as coming from *another
  Claude session*, not from you: Claude acts on it, but will not take it as
  your OK for a pending prompt (answer the prompt itself instead) or as
  permission to change its own settings. None of these endpoints is
  documented; a Claude Code update can change them.
- **In `claude --resume`**, like any other conversation on the machine:
  sessions are recorded as started by `ahp-host`, not by the SDK, whose
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
- **Chats, and folders added later.** A session created with no folder is a
  chat: Claude runs in an empty directory of its own (`<state_dir>/chat`) with
  no file, shell or MCP tools - only web search and fetch, behind the approval
  gate. A client can add a folder to any session while it runs
  (`multipleWorkingDirectories`); Claude restarts on the same conversation
  with that folder granted, once any turn in flight is over. The first folder
  is fixed for the session's life (`immutablePrimary`), because Claude Code
  keeps a conversation under the directory it started in.
- **Client tools.** Tools a client publishes for the session (its
  `activeClient.tools`) become Claude's tools, and each call runs in that
  client. An editor's own tools, or a gateway's tools for other machines, work
  this way. They are offered to chats as well, since they run nowhere here. The
  client that runs a tool decides whether it may, so these calls do not pass
  this host's approval gate.
- A model picker populated from Claude Code itself at start-up (the same list
  `/model` shows for the logged-in account, its default first), so new models
  appear without a release of this adapter. Each model carries its context
  window (`maxContextWindow`, `maxPromptTokens`), which the start-up probe asks
  Claude Code for model by model, and its output limit (`maxOutputTokens`)
  once a session has used it and Claude Code has said - published at once
  (`root/agentsChanged`) and kept in `<state_dir>/claude-models.json` for the
  next start. Every model takes images (`supportsVision`): all the ones Claude
  Code offers do. If Claude Code could not say at start-up (not signed in
  yet), it is asked again with backoff, and the picker fills in when it can.
- Attachments: a referenced file or folder is handed to Claude as its path
  (with the selected lines, if any); a pasted image or PDF is sent as an image
  or document block; pasted text is inlined (capped at 200k characters).
  Another chat attached to a message reaches Claude as its transcript, up to
  the turn the attachment names (capped the same, keeping its end).
  Annotation references are named but not resolved yet.
- Sessions that survive a host restart: the Agent SDK's session id is the
  host's resume state.
- **Automations**: a saved prompt that runs as a new session on a schedule
  (AHP cron in a named time zone) or on request, kept under
  `<state_dir>/automations`. The host evaluates schedules itself, so they fire
  with no client connected. Nobody is watching a scheduled run, so an approval
  prompt waits until someone answers it: give the automation's session
  template `config: {"permissionMode": "acceptEdits"}` or `"auto"` for work
  that should finish on its own. Needs ahp-host with automations.

## Run it

```bash
pip install -e .
python -m ahp_host_claude --config ~/.config/ahp/node.toml
```

The config file (TOML; flags override it):

```toml
agent_name = "Claude"
token_file = "~/.config/ahp/node.token"

# Several named folders: clients see file:///llm/..., file:///projects/...
# (behind a gateway: <machine>/llm/..., <machine>/projects/...)
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
| `[roots]` / `--root NAME=PATH` (repeatable) | Named folders. Clients browse (read-only) a small tree of them; sessions may work in any. Nothing else on the machine is reachable. A session with no folder is a chat, with no file or shell tools. |
| `root` / `--root PATH` | One unnamed folder, served as itself. |
| `token_file` / `--token-file` | Require this connection token. Read from a file so it never appears in `ps` or logs. |
| `port`, `bind` / `--port`, `--bind` | Default `127.0.0.1:4321`. Loopback only. |
| `state_dir` / `--state-dir` | Persisted sessions, automations, the sequence counter and the model limits sessions have learned. Default `~/.local/state/ahp-host-claude`. |
| `agent_name` / `--agent-name` | What clients call the agent. |
| `remote_control` / `--[no-]remote-control` | Put new sessions on claude.ai (Remote Control). Default: whatever Claude Code does, i.e. your `remoteControlAtStartup` setting. |
| `claude_ai_sessions` / `--claude-ai-sessions [all]` | Also list this machine's other Remote Control sessions, through claude.ai (default off; safe on every machine). `"all"`: every machine's, on one machine only. |
| `chat_tools` / `--chat-tools A,B` | The web tools a session with no folder may use. Default `WebSearch` and `WebFetch`; may only narrow that list (empty leaves none). For a host whose sessions cannot reach the internet, `["WebSearch"]`. |
| `provider_id` / `--provider-id` | The agent's id (default `claude`). Machines behind one gateway share it: the gateway merges them into one agent and the folder picks the machine. |

Authentication is Claude Code's own: the SDK uses whatever login `claude` has
on this machine (or `ANTHROPIC_API_KEY` if set).

## Security

The agent runs as the host's OS user, so whatever that user can touch, an
approved tool call can touch. The approval gate and the `--root` check are the
boundary - Claude Code's shell is not confined to the session's folders - which
is why a session with no folder gets no file or shell tools at all; see `ahp-host`'s SECURITY.md for the host's own posture.
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
With `claude_ai_sessions`, whoever can reach this host can also drive this
machine's other Remote Control sessions - read them, message them and answer
their approval prompts - and with `"all"`, every one on the account, on every
machine. Treat such a host as holding the account.

Turn it off (`remote_control = false`, or per session) where that is not the
same set of people as those who can reach this host.

Whoever can reach this host can also switch the machine's Claude Code MCP
servers on and off for a project (Claude Code keeps that in its own config, as
`/mcp` does), and pick any custom agent the session lists - but not loosen the
session's approval mode by picking one: what an agent's file sets is put back.
A subagent's tool calls pass the same approval gate as the session's own,
asked in the subagent's worker chat.

The choices on an approval prompt widen what runs without asking only when
someone picks one, and only for the session: a rule Claude Code suggests is
applied with its destination narrowed to the session (never your settings
files), a mode only if it is Accept edits or Auto, a directory never. Picking
a mode moves the session's approval mode with it, for every chat.

## Development

```bash
python -m venv .venv
.venv/bin/pip install -e . && .venv/bin/pip install --group dev
.venv/bin/pytest && .venv/bin/mypy && .venv/bin/ruff check . && .venv/bin/ruff format --check .
```

## License

MIT - see [LICENSE](LICENSE).
