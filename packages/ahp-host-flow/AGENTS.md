# Agent guide

An `ahp-host` provider that runs Claude Code workflow scripts. `docs/plan.md`
is the design; milestones M0–M12 there say what exists and what's next.

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
  worker chats), `worktree` (git worktrees for `isolation: 'worktree'`), `redaction`
  (`flow redact`: `redact_step`, the request spool),
  `provider` (`FlowProvider`/`FlowSession`), `gates` (`MainChatGates`: ask, approve and canvas on the main chat), `inprocess` (an
  in-memory host for `flow run --config`) and `__main__` (the server). They may
  import `ahp_host`; `engine/` and `sandbox/` may not. `library` (`run`, `FlowRun`) and `verbs`
  are top-level modules too; `library` may not import `ahp_host`. `engine/gates.py` (`GateRunner`) is
  engine code and may not import `ahp_host`; so are `engine/verbs.py` (`VerbRunner`) and
  `engine/verbnames.py`. `gates.MainChatVerbs` is the provider's verb host.
- Security decision (plan §8 M15 item 3): `[policy]` is checked first and always wins. An
  `[approvals]` rule can never allow what the policy denies or asks, nor a call the inner
  provider's policy asked about (`policy_asked`); keep that order in `approvals.Approver.decide`.
  Remote relays get no `Approver`. The flow `[policy]` is also folded into each in-process
  provider's own policy (`loading._with_flow_policy`, for factories with a named `policy`
  keyword), so an approve-all cannot switch it off; never drop that. A `read_only` rule matches an
  agent-supplied hint (`read_only_from_agent`) only with `trust_agent_hints`.
- `approve()` returns a bool; with `opts.x.detail` it returns `{approved, reason, answeredBy}`
  (`engine.gates.wants_detail`). Without detail the gate key and journal record are
  byte-identical to before (no `x` key); `GateRunner` prefers `approve_outcome` when the gates have one.
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
  the journal and the `agent()` return. The run's own `canvas()` title and
  URL are masked before `canvas_set`. Not covered, and documented as known
  gaps (README, plan §4.9; M9 shipped the after-publishing layer): terminals
  and background work an inner session opens through `ScopedPublisher`, a secret split across a
  tool-call boundary, and anything the best-effort builtin list misses. New
  output paths must mask too.
- `redaction` (after publishing) may import `ahp_host`; `engine/` may not, so
  the journal rewrite lives in `engine/journal.py` (`redact_agent_record`).
  Redaction never touches what the inner agent was sent or holds. `flow redact`
  reaches a running host only through `<state_dir>/redact` (`queue_request`,
  `process_requests`); there is no AHP client path, redaction is
  host-initiated. A live run is refused (`live_runs`, and `journal.lock`, which a `JournalWriter`
  holds while open, for another process). `redact_step` takes a required `actor`. Journal agent records may carry
  `redacted` (`JOURNAL_VERSION` stays 1).
- `awaitingInput` and the step's `waiting` entries come from the session's
  `inputNeeded` filtered to the worker chat (`StepDispatcher._watch_loop`,
  polled every `WAIT_POLL`); `TurnTee.on_waiting` is used only when the
  publisher is not `ObservesSessionState`. Inner sessions never get the session
  view (`ScopedPublisher` has no `session_state`).
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
- Kept worktrees live in the resume state (`"worktrees"`, from
  `WorktreeChangesets.kept_for`; `RESUME_STATE_VERSION` stays 1) and
  `FlowSession.restore_worktrees` republishes them from `resume_session`.
  `FlowProvider` is `ResumesOnRestore` only for a session holding any, so the
  host resumes it eagerly and the changesets reappear without a client opening it.
- Continuing a run uses `RunOptions.continue_run`, never the `--resume` prefix
  rule (plan §3 is unchanged).
- Journal readers know run/workflow/agent/started/gate records and still
  ignore unknown types. Gate ordinals come from their own host-side counter,
  never the `agent()` one. A continued run's gate call ids use `gate-r<k>`.
  Gate prompts and answers are masked, and the masked answer is what the
  script and the journal get.
- `conformance/extensions` keeps `.py` twins; `conformance/shim/dual_target.js`
  has none (Claude Code runs only JS). `shim/flow-shim.js` must stay plain JS
  that runs in both runtimes, and its text must stay embedded verbatim in
  `dual_target.js`. The package ships a byte-identical copy,
  `src/ahp_host_flow/shim/flow-shim.js` (`ahp_host_flow.shim.source()`, `flow shim`);
  change both together (`test_flowext_shim_resource` checks it).
- Gate options are re-checked by the host (`engine/gates.options_error`, in
  `GateRunner.issue`): a script can make its runner send anything (`toJSON`, a
  replaced `JSON.stringify`), so a runner's own checks are never the authority.
- A continued run's tool-call ids use the prefix `agent-r<k>`.
- A `.py` workflow from a client is never read or loaded (plan §4.10 trust);
  `python_sources` refuses `client`.
- Client workflows reach the engine as plain data: `RunOptions.client_workflows`
  (`ClientWorkflow(name, source, filename)`, `.js` only; the registry drops any other) and
  the labeled `WorkflowDir`s in `workflow_dirs`. The engine never imports the catalog, and
  `workflow('source:name')` is resolved by `engine/registry._split`.
- A watched-dir change (`FlowSession.start_watching`, `[workflows] watch_interval`) schedules
  the same coalescing refresh with `read_clients=False`: watch refreshes never read a client,
  they use the cached client entries.
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
- The changeset operation ids are `APPLY_OPERATION`, `COMMIT_OPERATION` and
  `DISCARD_OPERATION` (`worktree.py`); keep them stable, clients and `FlowProvider.changeset_operations`
  share them.
- The subagent preamble (`ProviderConfig.preamble`, on by default) reaches only
  the inner agent: the in-process `UserMessage` text and `raw["text"]`, the fleet
  `start_session` prompt and the remote `chat.prompt`. Never `AgentCall.prompt`
  (journal keys), the main chat's `agent` tool-call input or the worker turn's
  published message. A follow-up in a named chat (`opts.x.chat`) is sent bare: only the
  chat's first message carries it. Test fakes that script by prompt strip it through
  `tests/preamble.py`; a flow.toml that loads the echo provider loads
  `tests.flow_fakes:StrippedEchoProvider`.
- The corpus scripts (`conformance/corpus`) run unmodified on
  `tests/corpus_fakes.py` and have no `.py` twins.
- Custom verbs: the host re-checks every verb name against the run's verbs, whatever a
  runner sent. `RESERVED_VERBS` are refused at config load and in `RunOptions`, and so are names that would
  shadow a sandbox global (`verbnames`: JS globals, names the sandbox keeps absent, Python builtins). Verbs
  have a counter of their own, never shared with `agent()` or gate ordinals. Args,
  results and errors are masked before publishing or journaling, and the masked result
  is what the script gets. `call_id` is derived from the key and the occurrence.
- Resume keys: with no `key_extra` every `call_key` is byte-identical to M11's, guarded
  by the pinned hashes in `test_keyhook_engine_pinned_keys`. Never change the key
  payload without that test.
- The run's principal is the turn's `UserMessage.principal` only; there is no
  fallback to `AgentSessionContext.principal` (the session creator), so a turn without one
  runs with `None`. It is kept on `RunEntry` and in the resume state only
  when set (`RESUME_STATE_VERSION` stays 1) and is what `VerbContext.principal` reports.
  A `clientId` is never a principal.
- Typed starts: a start in `_meta["ahp-py/flow"]` wins over the text. Args are validated
  against `argsSchema` for typed and text starts and fail as `flow.badArgs` before
  anything is dispatched. A client run id is used only when it is free.
- `__main__` uses ahp-host's `is_loopback` and `bearer_token_validator`, never a private
  copy.
- `opts.x.cwd` (`dispatch.call_directory`) is resolved strictly, must be a directory inside
  `_roots()` (a relative one against `roots[0]`), and replaces the inner session's working
  directories; with `isolation: 'worktree'` it is the worktree's base. Never loosen the
  containment check.
- Conventional commits, scope `ahp-host-flow`; `CHANGELOG.md` under
  `[Unreleased]`.

## Keep it generic

This repository is public: nothing tracked may name one particular setup (no
hostnames, home-directory paths, tokens). Use placeholders. Local notes go in
`AGENTS.local.md` (gitignored).
