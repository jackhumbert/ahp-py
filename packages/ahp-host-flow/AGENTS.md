# Agent guide

An `ahp-host` provider that runs Claude Code workflow scripts. `docs/plan.md`
is the design; milestones M0–M10 there say what exists and what's next.

- `engine/` and `sandbox/` never import `ahp_host` (an import-linter
  contract), so the engine can move into its own distribution later. They
  reach remote agents through `ahp_client` only.
- The top-level package is the provider: a run is one session, and each
  `agent()` call is a worker chat (`ChatOrigin` kind `tool`). Inner providers
  are loaded by import path from config and never imported statically.
- The top-level modules are `config` (flow.toml), `routing` (rule matching),
  `loading` (inner providers by import path), `bridge` (`ScopedPublisher`,
  `TurnTee`), `catalog` (workflow sources as customizations), `commands`
  (parsing the message that starts a run), `dispatch` (`ProviderDispatcher`,
  worker chats), `worktree` (git worktrees for `isolation: 'worktree'`),
  `provider` (`FlowProvider`/`FlowSession`), `inprocess` (an
  in-memory host for `flow run --config`) and `__main__` (the server). They may
  import `ahp_host`; `engine/` and `sandbox/` may not.
- Inner providers come only from `load` by import path. Tests use
  `tests/flow_fakes.py` and `EchoProvider`, never a live model.
- `_meta["ahp-py/flow"]` is both the customization marker on a workflow's
  `prompt` child and the typed-progress descriptor on notifications and `agent`
  tool calls. The descriptor shapes are owned by `ahp_protocol.ext.flow`; build
  them with its helpers and never redefine them here.
- What a worker prints or returns is masked by the `Redactor`
  (`engine/redact.py`) before ahp-host-flow publishes or stores it, and nothing
  changes what the inner agent is sent or gets back. Covered: worker chat text,
  reasoning and tool calls (`TurnTee`), structured calls (`MaskingSink`, handed
  to `StructuredTurn`), approval and input prompts, step results, log lines,
  the journal and the `agent()` return. Not covered, and documented as known
  gaps (README, plan §4.9 / M9): terminals, canvases and background work an
  inner session opens through `ScopedPublisher`, a secret split across a
  tool-call boundary, and anything the best-effort builtin list misses. New
  output paths must mask too.
- `awaitingInput` is derived from the `TurnTee`'s confirmations and input
  requests, an approximation of the session's `inputNeeded`.
- `agent()` results are re-validated against `opts.schema` in the engine
  (`conformance/agent_schema.js`), whatever the provider claims.
- Hosting tests use `tests/hosting.py` over `memory_pair`.
- Script semantics must match Claude Code's workflow runtime exactly
  (`docs/plan.md` §3). Change them only with a conformance test.
- Tests needing Node carry the `node` marker and skip when no Node ≥ 20 is
  found (`AHP_FLOW_NODE`, else `PATH`). No test calls a live model.
- Conventional commits, scope `ahp-host-flow`; `CHANGELOG.md` under
  `[Unreleased]`.

## Keep it generic

This repository is public: nothing tracked may name one particular setup (no
hostnames, home-directory paths, tokens). Use placeholders. Local notes go in
`AGENTS.local.md` (gitignored).
