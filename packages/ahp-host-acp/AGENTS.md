# Agent guide

An `ahp-host` provider adapter, living in its own distribution as the
server's AGENTS.md requires ("Adding a provider adapter"). It is the ACP
*client*: it spawns an ACP agent per session and speaks JSON-RPC over stdio.

- The ACP transport and `session/update` parsing are the `ahp-acp` package
  (`AcpConnection`, `parse_update`); `AcpSession` talks to its agent through an
  `AcpClient` (initialize, capability checks, settling permissions on cancel);
  `jsonrpc.py` only re-exports its old names and is deprecated, so import from `ahp_acp`. Notifications are handled in
  order before the next line is read; requests from the agent run on their own
  tasks. Keep both, and see `ahp-acp`'s AGENTS.md before changing either.
- `provider.py` translates ACP `session/update`s into the host's neutral
  `TurnSink` events. Never emit AHP actions directly; the host owns ordering
  and the wire. A notification handler must never await an ACP *request*:
  the read loop is waiting on it, so the answer could never be read.
- Out-of-turn state goes through the session's `SessionPublisher`, built from
  small modules: `options.py` (ACP config options and modes <-> session
  config), `catalogue.py` (what the agent reported last, `agent.json`),
  `commands.py` (slash commands -> completions), `plan.py` (plan -> a row per
  update), `changes.py` (tool diffs -> approval previews, `fileEdit` results
  and the session changeset), `mcp.py` (configured MCP servers), `prompts.py`
  (a message's attachments and attached chats -> ACP prompt blocks). Each
  module's docstring says why it maps the way it does; keep that reasoning
  current when the spec moves.
- `permissions.py` is the approval policy: the user is offered the agent's
  own options, *allow always* included, and the agent gets exactly the one
  the user picked; a plain approve or deny is *once*, and nothing broader is
  ever picked for the user. Changing it is a security decision: say so in the
  commit, and test it. A cancel answers every open permission request as
  `cancelled` (`AcpSession._cancel`), as ACP requires.
- `client_tools.py` and `relay.py` give the agent a session's client tools (and
  the host's per-turn tools) as an MCP server, `ahp_client`. `relay.py` is a
  stdio server the agent launches (`sys.executable -m ahp_host_acp.relay`, or
  `relay_command` when set, which is not resolved on the host; standard library
  only, it imports nothing from this package); it connects back over a socket
  (loopback unless `relay_host` is set) to `ToolBridge`, which answers with the tool list
  and runs each call through `TurnSink.run_client_tool`. The socket is open to
  any local process, so a connection must open with the per-chat random token,
  passed in the server entry's environment and never in argv. ACP cannot change
  a live session's `mcpServers`: the entry is fixed at `session/new`, `resume`,
  `load` and `fork`, and only its tool *list* is dynamic (`tools/list` answers
  from current state, `list_changed` when it moves). An agent that caches its
  tools sees the set it first listed; keep that documented.
- **Security decision:** `permissions.relayed_answer` lets a call to a relayed
  tool through (the agent's first *allow once*, never *always*) instead of
  asking, because the client or host that runs the tool decides, as
  `ahp-host-claude` does. A call is relayed when `client_tools.looks_relayed` says so. Changing this is a security decision: say
  so in the commit, and test it (`tests/test_client_tools_acp.py`). A title
  is not proof, since the model often writes it: a call is relayed only when
  `client_tools.looks_relayed` agrees -- the exact title, a kind that is absent
  or `other`, and a raw input shaped like the tool's (only its declared
  properties). Never loosen that to the title alone.
- One agent process per AHP session, one ACP session per AHP chat
  (`provider._Chat`). Per-turn and per-conversation state lives on the chat;
  the session config, title and changeset are the session's. A fork is made
  with ACP's `session/fork` only when the source chat's turn count says the
  copy matches what the fork shows; keep that check if you touch it.
- `roots.py` and `paths.py` re-export `ahp_host.node`'s; fix bugs
  there.
- `agent.py` is the `acp` agent type for `ahp-node`; keep its options
  in step with `config.py`'s per-agent keys. Its `from_config` (a synchronous
  factory for `load = "ahp_host_acp:from_config"`) shares `provider_from_options`
  and `config.read_config_file`, so agent.py, config.py and `from_config` stay
  in step: a new per-agent key goes in `AGENT_OPTIONS`; a new server-only file
  key in `config.SERVER_ONLY_KEYS`, which `from_config` ignores in the file and
  refuses as a keyword (like any unknown option).
- **Security decision:** `ToolPolicy` is checked in `_request_permission` before
  the relayed shortcut and before `confirm_tool_call`, and both judge the call
  merged so far (`ToolCall.as_update`, the guessed kind included), never the
  request alone, which may carry only the call's id; a deny answers the reject
  option without a prompt and shows the reason on the call (`ToolCall.notice`); `permissions.POLICY_KINDS` is pinned by a test
  (`tests/test_acppolicy.py`).
- Decline reasons go to the agent as a text block on its next prompt in that
  ACP session (ACP has no reason field), behind `decline_feedback`; the
  `read_only` and `policy_asked` hints on a confirmation come from
  `permissions.read_only_hint` and the policy, and are security decisions too;
  `read_only_from_agent` stays true (the kind is the agent's word) and
  `locations` carries the call's paths. Reasons are dropped only after the
  prompt carrying them returned (or was cancelled in flight).
- `client_methods.py` serves fs/* and terminal/* only when `[policy] host_tools` is
  set; the roots jail is checked before the policy and is never relaxed; a denial's
  reason is the JSON-RPC error message. Each terminal runs in its own process
  group and kill/release kill the group (`ahp_acp.kill_tree`); import `AcpError` from `ahp_acp`, not
  `jsonrpc`.
- `launcher` and `env_allow` live in `AGENT_OPTIONS`, config `_KEYS` and
  `AgentSpec`; keep the three in step. The agent command after a launcher is
  never resolved on the host (only the launcher's program is).
- Tests run `tests/fake_agent.py` as a real subprocess; no network, no real
  agent. `tests/test_host.py` drives it through a real `Host` over an
  in-memory transport (`tests/hosting.py`).
- Conventional commits; `CHANGELOG.md` under `[Unreleased]`.
- `reference/` is an ACP agent, not a provider. It uses the standard library only and imports
  nothing else from this package (`test_refchat_reference_is_stdlib_only`). The API key comes
  only from the environment and is redacted from every log and error. It asks
  `session/request_permission` before every tool call so the host's policy always runs.
  Security decisions, each tested: MCP servers it starts never inherit `AHP_OPENAI_*` or
  secret-shaped names (`mcp.child_env`), and `write_file` checks confinement on `realpath`.
  A prompt's cancel token is made on the reading thread; never clear it at turn start. Its
  tests use the packaged loopback stub (`reference.stub`), never a real endpoint; that loopback
  socket is the one exception to "no network".

## Keep it generic

This repository is public. It is a library for anyone to embed or run against
their own deployment, so nothing tracked may name or depend on one particular
setup: no hostnames, machine names, domains, home-directory paths, IP
addresses, tokens, employers or internal projects. Use placeholders
(`example.com`, `my-mac-mini`, `/Users/me`) in code, tests, docs *and* commit
messages — a commit message is as public as the code.

Deployment glue (service files, reverse-proxy config, one fleet's layout)
belongs in the deployment, not here. A feature one setup needs is generalised
into an option or left out.

Anything an agent needs to know about the local setup lives in
`AGENTS.local.md`, gitignored by `*.local.*`. Read it if it exists; never copy
from it into a tracked file.
