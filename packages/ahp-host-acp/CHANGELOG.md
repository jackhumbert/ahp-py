# Changelog

## [Unreleased]

### Added — answering an agent's extension requests

- `AgentSpec.ext_handlers` (and the `ext_handlers` keyword of `from_config`): a mapping of
  extension method name (starting with `_`, e.g. `_vendor/workspace`) to an async
  `handler(ctx: ExtRequestContext, params: dict) -> dict`. `ExtRequestContext` carries
  `method`, `session_id`, `cwd`, `roots`, `policy`, `resolve_path` (the same roots jail
  `fs/*` uses) and `log`. A handler that raises is answered with a generic JSON-RPC error
  (`extension request failed`, no exception text). A `_` method with no handler is still
  refused as not found.

## [0.1.0] - unreleased

### Fixed — the client-tool bridge's close

- `ToolBridge.aclose` closes every accepted connection, including one still in its
  token handshake, and waits at most `CLOSE_TIMEOUT` (5 s) for the server to finish
  closing, logging if it does not. On Windows the proactor loop could miss a
  connection's close, and `Server.wait_closed()` (which from Python 3.12 waits for
  every accepted connection) then never returned, hanging the host's teardown.

### Added — release preparation (M18)

- `scripts/smoke_wheel.py` checks an installed wheel from outside the checkout (CI's wheels job and release.yml run it).
- `RELEASING.md`: the release procedure and the release order.

### Changed — release preparation (M18)

- The version is 0.1.0 (was 0.1.0.dev0).

### Added — a reference agent on an OpenAI-compatible endpoint (M18)

- `ahp_host_acp.reference` (standard library only, an ACP *agent* rather than a provider):
  `reference.chat` is a blocking streaming `/v1/chat/completions` client on `http.client`
  (text, reasoning in both spellings, tool-call drafts, usage, `429` reported as rate limited,
  the API key redacted from errors, and `ChatClient.abort` to cancel from another thread);
  `reference.stub` is a scripted loopback chat-completions server (`StubServer`, `say`,
  `call`, `refuse`, `Reply`) for tests and demos. `tests/test_refchat.py`.
- `reference.mcp` is a stdio MCP client (`McpClient`: `start`, `list_tools` following
  `nextCursor`, `call_tool` with no timeout, `close`; `McpError`; `from_acp_entry` turns an ACP
  `McpServerStdio` entry into a client and anything else into `None`). It answers the server's
  `ping`, refuses its other requests, and fails every waiter when the process exits.
  `tests/fake_mcp_server.py`, `tests/test_refmcp.py`.
- `reference.agent` and the console script `ahp-acp-openai` (`python -m ahp_host_acp.reference`)
  are the agent itself: ACP over stdio (`initialize`, `session/new` with stdio MCP servers,
  `session/prompt`, `session/cancel`) in front of a streaming chat-completions endpoint, with
  text and reasoning streamed as message and thought chunks and token usage reported on the
  prompt response (cached input counted apart, as the host expects). It is configured from
  `AHP_OPENAI_BASE_URL`, `AHP_OPENAI_MODEL`, `AHP_OPENAI_API_KEY`, `AHP_OPENAI_TIMEOUT`,
  `AHP_OPENAI_MAX_STEPS` and `AHP_OPENAI_SYSTEM`; the key is read only from the environment
  (there is no option for it) and is redacted from everything it logs. Every tool call, its
  own `write_file` (confined to the session folder) and the MCP tools, asks the client for
  permission first; a decline or a failed endpoint call is reported to the model or the
  client, and `session/cancel` aborts the request in flight. `tests/test_refagent.py`.
- Review fixes: an MCP server the agent starts gets its environment minus every `AHP_OPENAI_*`
  variable and any name containing `KEY`, `TOKEN`, `SECRET` or `PASSWORD` (the entry's own `env`
  still sets any of them; `reference.mcp.child_env`); `write_file` checks confinement on
  `realpath`, so a symlinked file or directory inside the folder that points outside is refused,
  and the permission prompt, diff and write use the resolved path; a prompt's cancel token is
  made when the request is read, so a `session/cancel` read before the turn starts still cancels
  it; `ChatClient.abort` during connect or send takes effect once the socket exists; and
  `McpClient.call_tool(cancel=, timeout=)` gives up a relayed call on cancel (`McpCancelled`,
  with `notifications/cancelled` to the server). `tests/test_refmcp.py`, `tests/test_refchat.py`,
  `tests/test_refagent.py`.
- `tests/test_refhosted.py` hosts the reference agent through `AcpProvider` against the stub:
  text and reasoning, the client-tool relay, `StructuredOutput`, decline reasons on the next
  prompt, a policy denial with its reason, cancel mid-stream and a `429`. No source change was
  needed. `tests/test_refhosted_live.py` is the opt-in live lane (`AHP_OPENAI_BASE_URL` and
  `AHP_OPENAI_MODEL`). README section "The reference agent".

### Added — serving off loopback with a token (M17)

- `--bind` (or `bind` in the config file) may name a non-loopback address. It needs
  `--token-file` (`token_file`): without one, or with an empty one, `python -m ahp_host_acp`
  refuses before starting; with one, the host accepts remote connections (`allow_remote`) and
  takes the token as `?tkn=` or as an `Authorization: Bearer` header
  (`ahp_host.ws.bearer_token_validator`), as `python -m ahp_host_flow` does since its M13.
  It is how ahp-host-flow's "Running on sandboxed hosts" serves an agent from inside a
  container. `__main__` now has `build_host`, `read_token` and `serve`.
  `tests/test_acpbind.py`.

### Changed — input gauge marked (M16)

- A turn report with no `inputTokens`, whose input is the context size standing in, says so in its `_meta`: `acpUsage.inputGauge = true`. It is how full the context is, not what the turn consumed, so a reader that sums usage (ahp-host-flow's budgets and quotas) counts it as no input. `tests/test_session_updates.py`.

### Added — decline feedback and confirmation hints (M15)

- `decline_feedback` (default true; config file, `acp` agent options,
  `AgentSpec.decline_feedback`): why a call was declined (the policy's reason,
  or the user's reason and suggestion) is sent to the agent as one text block
  at the end of the chat's next `session/prompt`, since ACP's outcome has no
  reason field. `permissions.decline_line` caps each reason at 300 characters.
- **Security-relevant:** a `ToolConfirmation` now carries `policy_asked` (the
  host policy marked the call *ask*) and `read_only`
  (`permissions.read_only_hint`: true for `read` and `search`, false for
  `edit`, `delete`, `move`, `execute`, none for other kinds), which an
  automatic answerer may trust. Pinned by tests.
- **Security-relevant:** confirmations are marked `read_only_from_agent` (the
  hint comes from the agent's `kind`) and carry the call's `locations` paths.
- `from_config` takes a named `policy` keyword (a table), so a host composing
  policies (ahp-host-flow) can see it; it is combined with the file's own
  `[policy]` (`ahp_host.provider.policy.combine_tables`): a deny from either wins.

### Fixed

- Decline reasons are dropped only once the prompt carrying them was delivered:
  an agent that exits on that prompt gets them again on the retry.

### Added — [policy] host_tools (M14, security)

- `[policy] host_tools = true` (config file and `acp` agent options;
  `AgentSpec.client_methods`) offers the agent `fs/read_text_file`,
  `fs/write_text_file` and `terminal/*`, served by the host in the new
  `client_methods.py`. **A security decision:** every request is checked
  against the roots jail first, never relaxed, and then the `ToolPolicy`; a
  denial's reason is the JSON-RPC error message, and an *ask* goes to the
  turn's user. Terminal output is capped at 1 MiB. An agent that ignores these
  methods is bound only by the launcher and `env_allow`.
- Each terminal command runs in its own process group (POSIX session); kill,
  release and the session's end kill the group, grandchildren included
  (Windows: `taskkill /T`).
- A policy deny in `_request_permission` is shown to the user: the reason is
  the call's invocation message and the first part of its result.

### Added — host tool policy, advisory (M14, security)

- `[policy]` (the `ahp-host` `ToolPolicy`: `deny_commands`, `deny_paths`,
  `ask_paths`, `allow_paths_outside_roots`, `check`) in the config file, the
  `acp` agent options and `AgentSpec.policy`. **A security decision in
  `permissions.py`:** `_request_permission` asks the policy first, before the
  relayed shortcut and before `confirm_tool_call`. A deny answers the agent's
  reject option (`reject_once`, else `reject_always`, else `cancelled`) without
  a prompt; an *ask* skips the relayed shortcut. `permissions.POLICY_KINDS`,
  `tool_request` and `rejected_answer` are new; the kinds are pinned by a test.
- Advisory only: an agent in its own allow-always mode never asks, and the
  denial's reason cannot reach the agent through ACP; `host_tools` (below) fixes that.

### Added — relay_command and relay_host (M14)

- `relay_command` (the relay's command inside a container, such as
  `["python3", "-m", "ahp_host_acp.relay"]`; not resolved on the host) and
  `relay_host` (the address the tool bridge listens on, one the container can
  reach; loopback when unset) in the config file, the `acp` agent options and
  `AgentSpec`. `ToolBridge` takes `host` and `relay_command`; `McpServer` has
  `resolve`; the relay takes `--host`. The token still travels only in the MCP
  entry's environment.

### Added — launcher and env_allow (M14)

- `launcher` (a program and arguments the agent command runs behind, such as
  `docker run -i --rm -v {cwd}:{cwd} -w {cwd} img`; `{cwd}`, `{uid}` and `{gid}`
  are filled in per session) and `env_allow` (host variable names or globs the
  agent may inherit) in the config file, the `acp` agent options and
  `AgentSpec`. Paths map by identity only: mount each root at the same path.
- `provider.launch_argv(spec, cwd)`; `AcpConnection.spawn` and `Connector` take
  `inherit_env`.
- Unset `env_allow` keeps today's behaviour (the agent inherits the host's whole
  environment); setting it is recommended.

### Changed

- The ACP transport and `session/update` parsing moved to the new `ahp-acp`
  dependency. `ahp_host_acp.jsonrpc` is a deprecated re-export of its old names.
  The provider reads updates as typed events; a known kind missing a required field
  is now logged and ignored, like an unknown kind. Agents start in their own
  process group, so shutdown reaches grandchildren.

### Fixed

- A cancel now answers any open permission request as `cancelled`, so the
  user's question is withdrawn and the agent is told, instead of leaving it
  waiting until the turn is torn down.

### Added

- `from_config(config=None, *, roots=None, state_dir=None, **options)`: build an
  `AcpProvider` from Python, with no node, for a flow.toml
  `load = "ahp_host_acp:from_config"`. It reads this package's config file
  (server-only keys like `port` are ignored in the file, and refused as keywords
  like any unknown option) and keyword options override it.
  `config.read_config_file`, `config.roots_option`, `config.SERVER_ONLY_KEYS` and
  `agent.provider_from_options` are public; `agent._OPTIONS` is now `AGENT_OPTIONS`.
- `ReturnsStructuredOutput`: every ACP session offers the host's per-turn tools,
  so a turn that asks for a structured result gets a `StructuredOutput` tool in
  the relay's list for that turn only (the list is refreshed when the turn
  starts and ends). The call is run by the host, so it is let through like a
  client tool. A follow-up message inside one AHP turn (`continues_turn`) does
  not count as a turn for the fork rule.
- An opt-in live smoke test (`tests/test_live_structured.py`), skipped unless
  `AHP_ACP_LIVE_AGENT` names an ACP agent command.
- Client tools for ACP agents: a session's client tools (kept current as
  clients come and go, `FollowsActiveClients`) and the host's per-turn tools
  are given to the agent as an `ahp_client` MCP server, served by the stdio
  relay and run through `TurnSink.run_client_tool`. The server is fixed when a
  chat's ACP session is opened (ACP cannot change it later); only its tool
  list is dynamic, so an agent that caches tools and ignores
  `tools/list_changed` sees the set it first listed.
- **Security-relevant:** a call to one of these tools is allowed once without
  asking the user (`permissions.relayed_answer`: the agent's *allow once*,
  never *always*), because the client that runs the tool decides. The agent's
  own row for such a call is dropped; the client's replaces it. A permission
  request counts as such a call only when its title names a relay tool
  exactly, its kind is absent or `other`, and its raw input (if any) is an
  object using only the properties the tool's schema declares
  (`client_tools.looks_relayed`); values are left for the tool's owner to
  refuse. A title alone is not enough,
  because the model often writes a call's title: a shell command titled after
  a relay tool is asked about like any other call.
- A stdio MCP relay (`python -m ahp_host_acp.relay`) and the loopback
  `ToolBridge` behind it, so any ACP agent can be given a session's client
  tools (and the host's per-turn tools) as an MCP server; the relay
  authenticates with a per-session random token passed in the server entry's
  environment. The relay's stdin and stdout are UTF-8 with "\n" line ends
  whatever the locale, as MCP's stdio transport requires (on Windows the
  defaults are the locale code page and "\r\n").
- Message attachments in the prompt, per the agent's `promptCapabilities`:
  served files embedded (`resource`, small text files) or linked
  (`resource_link` to the real path), web links, selections named in a text
  block, inline images and audio, other inline data as embedded resources,
  `simple` attachments' `modelRepresentation`, and attached chats
  (`UserMessage.attached_chats`) as a markdown transcript -- embedded, or as
  text. Anything the agent cannot take is left out with a log line.
- Forked sessions (`createSession.fork`) through `session/fork` on the source
  session's agent, reopened in the new session's own process, under the chat
  fork's latest-turn rule. Otherwise the new session's first message carries
  the copied transcript as context, with a system notification saying so.
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
