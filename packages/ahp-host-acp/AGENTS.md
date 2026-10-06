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
  update), `changes.py` (tool diffs -> the session changeset), `mcp.py`
  (configured MCP servers). Each module's docstring says why it maps the way
  it does; keep that reasoning current when the spec moves.
- `_request_permission` is the approval policy (grant once, never always).
  Changing it is a security decision: say so in the commit, and test it.
- `roots.py` and `paths.py` re-export `ahp_host.node`'s; fix bugs
  there.
- `agent.py` is the `acp` agent type for `ahp-node`; keep its options
  in step with `config.py`'s per-agent keys.
- Tests run `tests/fake_agent.py` as a real subprocess; no network, no real
  agent.
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
