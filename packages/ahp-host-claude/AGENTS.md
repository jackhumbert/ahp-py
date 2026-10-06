# Agent guide

An `ahp-host` provider adapter, living in its own distribution as the
server's AGENTS.md requires ("Adding a provider adapter").

- `provider.py` translates the Claude Agent SDK's message stream into the
  host's neutral `TurnSink` events. Never emit AHP actions directly; the host
  owns ordering and the wire.
- `permissions.py` is the approval policy. Changing what runs without asking is
  a security decision: say so in the commit, and test it.
- The rest, each with the evidence for its choices in its docstring:
  `questions.py` (`AskUserQuestion` <-> input requests), `usage.py` (per-turn
  `UsageInfo`), `models.py` (picker limits: probed at start-up, learned from
  results), `customizations.py` (the customization tree), `completions.py`
  (`/` and `@`), `history.py` (turn marks, edit-and-resend cuts, fork points),
  `edits.py` (edit previews, per-call diffs, changesets), `transcripts.py`
  (another chat's turns as text), `background.py` (tasks and subagent worker
  chats), `client_tools.py`, `remote_control.py` (control requests the SDK
  does not wrap), `claude_ai.py` (claude.ai mirrors). `LocalClaudeSession` in
  `provider.py` is a session this host runs, and also each of its other chats
  (`_parent` set, held in the default chat's `_chats`); a mirror is a plain
  `ClaudeSession`, which lacks what only a local Claude Code can do (truncate,
  MCP start/stop, customization toggles, more chats, resuming).
- Security decisions recorded in the code, each tested: the question tool
  always reaches the approval callback; Claude Code's permission suggestions
  are offered only as explicit choices, narrowed to the session, never a
  bypass mode or a directory (`permissions.choices`); a chat narrowed away
  from the session's folder runs its gate in Ask (`ClaudeSession._gate`); the
  permission mode Claude Code reports at init is put back to the session's.
- SDK behaviour not documented by the SDK was read from the CLI it bundles
  (`claude_agent_sdk/_bundled/claude`): `strings` it and search for the
  control request or message schema (`subtype:R("...")`).
- Tests use a fake SDK client (`tests/fakes.py`); no network, no subprocess.
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
