# Agent guide

An `ahp-host` provider adapter, living in its own distribution as the
server's AGENTS.md requires ("Adding a provider adapter"). It is the ACP
*client*: it spawns an ACP agent per session and speaks JSON-RPC over stdio.

- `jsonrpc.py` is the transport. Notifications are handled in order before the
  next line is read; requests from the agent run on their own tasks. Keep both.
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
  commit, and test it.
- One agent process per AHP session, one ACP session per AHP chat
  (`provider._Chat`). Per-turn and per-conversation state lives on the chat;
  the session config, title and changeset are the session's. A fork is made
  with ACP's `session/fork` only when the source chat's turn count says the
  copy matches what the fork shows; keep that check if you touch it.
- `roots.py` and `paths.py` re-export `ahp_host.node`'s; fix bugs
  there.
- `agent.py` is the `acp` agent type for `ahp-node`; keep its options
  in step with `config.py`'s per-agent keys.
- Tests run `tests/fake_agent.py` as a real subprocess; no network, no real
  agent. `tests/test_host.py` drives it through a real `Host` over an
  in-memory transport (`tests/hosting.py`).
- Conventional commits; `CHANGELOG.md` under `[Unreleased]`.

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
