# Agent guide

An `ahp-host` provider that runs Claude Code workflow scripts. `docs/plan.md`
is the design; milestones M0–M7 there say what exists and what's next.

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
  worker chats), `provider` (`FlowProvider`/`FlowSession`), `inprocess` (an
  in-memory host for `flow run --config`) and `__main__` (the server). They may
  import `ahp_host`; `engine/` and `sandbox/` may not.
- Inner providers come only from `load` by import path. Tests use
  `tests/flow_fakes.py` and `EchoProvider`, never a live model.
- `_meta["ahp-py/flow"]` is the namespaced customization marker on a workflow's
  `prompt` child.
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
