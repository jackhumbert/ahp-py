# Changelog

## [Unreleased]

### Added

- One ACP session per chat (`HostsChats`, `CancelsChats`; `multipleChats` is
  advertised): each chat a client creates gets its own `session/new` in the
  session's agent process, turns route by `UserMessage.chat_uri`, stopping a
  chat sends `session/cancel` for that chat only, a closed chat's session is
  `session/close`d when the agent offers it, and each chat's ACP session id
  is kept in the resume state (`chats`) and reattached after a restart.
- Chat forks through ACP's (unstable) `session/fork`, advertised as
  `multipleChats.fork` only when the agent declares `sessionCapabilities.fork`,
  and made only at the source chat's latest turn -- ACP forks a whole
  conversation, so anything earlier is refused rather than copied wrong. Side
  chats are not offered.
- Per-call diffs: a completed edit's result carries a `fileEdit` item per
  file (`TurnSink.file_edit`), from the file before that call to after it, in
  place of the diff's text summary.
- Approval prompts offer the agent's own permission options
  (`ConfirmationOption`s, its ids and names) and preview a call's diff
  (`ToolConfirmation.edits`).
- The agent's entry on the root channel follows what it reports
  (`UpdatesAgentInfo`): a model list from its `model` option, a model's
  context window, and whether it can fork, without a restart.
- `completion_trigger_characters = ("/",)` on the provider
  (`DeclaresCompletionTriggers`), so `ahp-node` offers slash commands too.
- Session config from the agent: its ACP config options (`select`,
  `boolean`) or, failing those, its session modes become the session's config
  schema (`ConfiguresSessions`), with the agent's starting values as defaults.
  A client's choice, at creation or later (`ReconfiguresSessions`), goes to the
  agent with `session/set_config_option` / `session/set_mode`; the agent's own
  changes (`config_option_update`, `current_mode_update`) and its refusals are
  published back (`SessionPublisher.config_changed`). The model option is left
  to the model picker when there is one.
- `agent.json` in the state directory (an `ahp-node` agent's own): what the
  agent's last `session/new` reported (options, modes, models), its slash
  commands and each model's context window, so a session's schema and the
  model picker can be built before the agent runs.
- A model picker from the agent's own `model` option (or the removed
  session-model API) when the config file has no `[[models]]`; a model's
  context window from the agent's `usage_update.size` when the config gives
  none.
- `session_info_update` titles rename the session.
- `available_commands_update`: slash commands as completions after a `/` at
  the start of a message, from the chat's own ACP session.
- `plan` updates as one finished "Update plan" row per change, the task list
  as its result, and the entry in progress as the session's activity.
- Tool-call `diff`s as the session's changeset ("Session changes"), with
  +/- counts, checked against the files on disk inside the served folders, so
  an agent that sends edit fragments (Claude Code's adapter) still gets whole
  files.
- `usage_update`'s `size` and `cost` on the turn's usage, as
  `_meta.acpUsage` (`used`, `size`, `cost`).
- `[[mcp_servers]]` (and `mcp_servers` for `ahp-node`): MCP servers given to
  the agent in `session/new`, `session/resume` and `session/load`; `http` and
  `sse` ones only to an agent that advertises them.
- `type = "acp"` for `ahp-node` (ahp-host): the package
  registers an `acp` entry in the `ahp_host.agents` group, so one node
  can serve several ACP agents (goose, opencode) beside Claude from one host,
  port and folder tree. Options are the config file's per-agent settings:
  `provider_id`, `agent_name`, `description`, `command`, `env`, `models`,
  `model_command`, `config_options`.
- An ACP client provider: one agent process per session, streaming text,
  reasoning, tool calls and usage; permission requests as approval prompts
  (granted once, never "always"); session resume via `session/resume` or
  `session/load`.
- A model picker from the config file, switched with `session/set_model`, or
  with a `model_command` prompt for agents without ACP model support
  (OpenClaw: `/model {model} -s`).
- Models switched through a `model` session config option when the agent
  offers one (opencode).
- `[config_options]`: ACP session config options set on every session
  (OpenClaw: `thought_level`).
- A TOML config file and named roots, as in ahp-host-claude.

### Changed

- **Approval policy (a security decision):** the user is offered the agent's
  options as-is, *allow always* included, and the agent gets exactly the one
  picked. Before, approving always sent *allow once* (or *allow always* when
  that was all the agent offered). A plain approve or deny is now *once* only;
  if the agent offers no such option the answer is `cancelled`, so nothing
  broader than the user's answer is ever sent. ACP's answer has no field for
  a denial's reason or a suggestion, so neither reaches the agent.
- `[config_options]` are now shown to clients, read-only, and may be booleans.
- Updates an agent sends right behind its `session/new` answer, before the
  session id is known, are no longer dropped.
- The doc of the text summary of a tool's diff no longer claims the host has
  no diff content: AHP 1.0.0 has `fileEdit`; the summary stays until the
  provider API can store one.
- **Renamed from `agent-host-server-acp` to `ahp-host-acp`** (import `agent_host_server_acp` → `ahp_host_acp`), and moved into the `ahp-py` monorepo as `packages/ahp-host-acp`. Command: `agent-host-server-acp` → `ahp-host-acp`. Tags are now per package: `ahp-host-acp/v<version>`.
- `roots` and `paths` re-export `ahp_host.node`'s instead of keeping
  copies. `python -m ahp_host_acp` still runs a one-agent host.
