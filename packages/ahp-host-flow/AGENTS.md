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
  import `ahp_host`; `engine/` and `sandbox/` may not. `library` (`run`, `FlowRun`) and `verbs`
  are top-level modules too; `library` may not import `ahp_host`.
- Dispatch is split by provider kind (`ProviderConfig.kind`: `inprocess`,
  `fleet`, `remote`). `dispatch.StepDispatcher` is the shared base (the main
  chat's `agent` tool call and its typed progress); `ProviderDispatcher`
  (in-process worker chats) and `RemoteWorkerChats` (the thin worker chat of a
  remote call) live in `dispatch`, `FleetDispatcher` and `FleetClients` in
  `fleet` (fleet calls through a gateway's fleet tools, called from the main
  turn's `run_client_tool`), and `RoutingDispatcher` picks one per call.
  `engine/remote.py` holds `ClientDispatcher`, which dials another host with
  `ahp_client` (so it is engine code: no `ahp_host`). `agent` is the `flow`
  agent type for `ahp-node` (the `ahp_host.agents` entry point).
- Only an abort (`cancel_chat`/`cancel`, the run's `AbortSignal`) or a skip
  stops a fleet call's node session. A cancellation without an abort is the
  host stopping, and the node session is left running for the resumed run to
  reattach to (`FleetDispatcher(aborted=...)`). Keep it so: a stop sent at
  shutdown makes reattach impossible.
- Every fleet tool call goes through `FleetDispatcher._call`, which watches it
  (the offering client leaving, `reply_timeout`) and retries once on the
  rejoined gateway. `start_session` is never retried.
- **`ahp-gateway` is a test-only dependency**, a deliberate exception to the
  layering rule (a provider may not import the gateway, which sits beside the
  host): `tests/fleet_hosting.py` boots a real in-memory gateway so the fleet
  tests run end to end. It is in the `dev` dependency group only, never in
  `dependencies`, and nothing under `src/` imports it (the import-linter
  contract "the package never imports the gateway"). The import-linter
  contracts cover `src` only (`root_package = ahp_host_flow`), never `tests`;
  keep them so. The fleet tool names are duplicated in
  `fleet.FLEET_TOOLS` for that reason.
- Inner providers come only from `load` by import path. Tests use
  `tests/flow_fakes.py` and `EchoProvider`, never a live model. Fleet tests use
  `tests/fleet_fakes.py` (a fake gateway offering the fleet tools) or
  `tests/fleet_hosting.py` (a real in-memory gateway, worker nodes and a flow
  node); remote tests dial a second in-memory host (`remote_dialer`). No
  sockets.
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
- `sandbox/pyrunner.py` and `verbs.py` are standard library only: they import
  only `ahp_host_flow.sandbox.protocol` and each other, and must never import
  `ahp_host`, `ahp_client`, `ahp_protocol`, the engine or third-party code. The
  import-linter contract "the Python runner and verbs use the standard library
  only" and `test_pyrunner_stdlib_only` enforce it.
- `ahp_host_flow/__init__.py` and `sandbox/__init__.py` stay lazy (PEP 562), so
  the runner loads nothing else.
- The Python runner starts as `python -I -m ahp_host_flow.sandbox.pyrunner`, and `-I`
  ignores `PYTHONPATH`: `ahp-host-flow` must be installed in that interpreter (an
  editable install counts), not merely importable through `PYTHONPATH`.
- `RunOptions.python_dirs=None` means a trusted caller (the CLI and library) and
  allows any `.py`. Every provider path must set it, to the trusted sources'
  workflow directories only (`catalog.python_dirs`: for the workspace that is
  `.claude/workflows`, never the working directory, and nothing when `[workflows]
  sources` excludes it).
- Ordinals and the current phase are assigned by the host (`Scheduler.issue`)
  when a request line arrives, never in a runner.
- Every `.js` conformance script keeps a `.py` twin, checked against the same
  sidecar.
- `FlowProvider` is a `ResumableAgentProvider` and `FlowSession` implements
  `ResumesTurns`: `resume_turn` relaunches a run a host restart cut off, for
  main run chats only; it declines worker chats (fails them, not resumable).
  Whatever it needs about the run is in the resume state, because it gets only
  the chat and turn id.
- Continuing a run uses `RunOptions.continue_run`, never the `--resume` prefix
  rule (plan §3 is unchanged).
- Journal readers ignore unknown record types, so M10 gate records stay
  readable. Keep it so.
- A continued run's tool-call ids use the prefix `agent-r<k>`.
- A `.py` workflow from a client is never read or loaded (plan §4.10 trust);
  `python_sources` refuses `client`.
- Never read from a client inside `active_clients_changed` or `describe`: the
  client's reply arrives on the connection that is delivering the notification,
  so it deadlocks. `active_clients_changed` only schedules a (coalescing)
  refresh task; `describe` reads the cache.
- Client workflows are listed in the provider's own container
  (`ahp-host-flow.client.<plugin id>`), never under the client's plugin id,
  which the client owns.
- Worker chat interactivity follows `ProviderConfig.interactive`: `full` when
  set, else read-only. Steering is forwarded only if the inner session
  `SteersTurns`, otherwise rejected with a reason.
- The changeset operation ids are `APPLY_OPERATION` and `DISCARD_OPERATION`
  (`worktree.py`); keep them stable, clients and `register_worktree_operations`
  share them.
- The corpus scripts (`conformance/corpus`) run unmodified on
  `tests/corpus_fakes.py` and have no `.py` twins.
- Conventional commits, scope `ahp-host-flow`; `CHANGELOG.md` under
  `[Unreleased]`.

## Keep it generic

This repository is public: nothing tracked may name one particular setup (no
hostnames, home-directory paths, tokens). Use placeholders. Local notes go in
`AGENTS.local.md` (gitignored).
