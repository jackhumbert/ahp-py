# Changelog

## [Unreleased]

### Security

- Credential masking before publishing (plan §4.9): a secret printed or returned by an inner agent is masked in the worker chat's text, reasoning, tool calls and results, structured calls (the `StructuredOutput` input and accepted result), approval and input prompts (`ToolConfirmation`, `InputRequest`), step results, `log()` lines, run errors, the `agent()` return and the journal (`[redact]` in `flow.toml`). Values under secret-named keys (`password`, `token`, `api_key`...) are masked at any depth. The inner agent's own view (prompts sent, replies, tool results and answers returned to it) is untouched. Known gaps: terminals, canvases and background work opened through `ScopedPublisher`, a secret split across a tool-call boundary mid-stream, and the best-effort builtin list (no `key=value` values under 8 characters, URL userinfo or `sk-` keys); the first two wait for the after-publishing layer (§4.9, M9).
- `ScopedPublisher` contains an inner session: a `chat=` it passes is honoured only for its worker chat or a tool chat it opened itself. A foreign chat (the run's main chat, another worker) is never reached: config changes, external turns and changesets naming one are dropped, and canvases, terminals, background work and tool chats are redirected to the worker chat. `dropped` is a bounded deque (`dropped_counts` keeps totals by kind), so progress spam cannot grow it.

### Fixed

- A config overlay that reaches a worker chat before its inner session is registered is applied once it is, instead of waiting for the next overlay.
- Customizations are re-published when a workflow source directory changes between runs (re-scanned at each run start and on describe; no file watching).
- Workflow discovery skips symlinks and non-regular files.
- `agent()` no longer hangs when the host cancels the worker turn before it starts: the call returns `null` (skipped) and its inner session is cancelled and closed.
- A failure opening the worker chat, creating the inner session or running the turn now completes the main chat's `agent` tool call as failed instead of leaving it `running`.
- `flow run --config` fails at the first confirmation or input an inner session asks for, naming the call and suggesting `approval_mode`, instead of hanging; it is never auto-approved.
- Inner providers are now really loaded on first use. A provider that fails to load fails only the calls routed to it (`null` plus a log line) and is left out of the model list, instead of stopping startup.
- `flow run --config` rejects `--budget 0` and `--timeout 0` like any other engine flag.
- `python -m ahp_host_flow` refuses a non-loopback `--bind` without `--token-file`.

### Added

- Docs for M4b's first part: README (typed progress, masking, worktrees, start records, corpus test), AGENTS.md (`worktree`, the shared meta key, the redaction invariant) and `docs/plan.md` (status, `[redact]` in §5, the M4b result note).
- Fan-out corpus script (`conformance/corpus/fanout.js`), run on fakes in the suite and, opt-in, through the configured providers (`AHP_FLOW_LIVE_CONFIG`).
- `agent()` `isolation: 'worktree'`: the inner session runs in a git worktree under `<state_dir>/runs/<runId>/worktrees/<ordinal>`, removed afterwards when unchanged, kept and logged otherwise; outside a git repository the call returns `null` with a log line. An invalid `effort` option logs one line and is ignored.
- Typed progress on the main chat (plan §4.7), built with `ahp_protocol.ext.flow` under `_meta["ahp-py/flow"]`: system notifications `Run <id>` (`runStarted`), `Phase: <title>` (`phaseStarted`), `Phase finished: <title>` (`phaseFinished`) and `Workflow finished` / `Workflow failed: <error>` (`runFinished` ok/error); each `agent` tool call carries `stepStarted` (ordinal, label, phase, worker chat, model, provider), a subagent link and `awaitingInput` while its worker chat has an open confirmation or input request, and `stepFinished` (ok, null, error or skipped, the result, usage) on its result. `log()` stays a plain notification. Stock clients render the same text; `catalog.FLOW_META_KEY` is now imported from the protocol package.
- `ahp_host_flow.worktree`: git worktree helper for `isolation: 'worktree'` (`create_worktree`, `has_changes`, `release_worktree`, `worktree_path` under `<state_dir>/runs/<runId>/worktrees/<ordinal>`); an unchanged worktree is removed, one with uncommitted or committed changes is kept.
- Journal start records: a `started` record (ordinal, key, dispatch handle with provider name and worker chat URI) precedes each live call's agent record; `ProviderDispatcher` reports its handle through `report_dispatch`. Resume logs `resume: agent() #N started on <provider> (<chat>) but never finished; not replayed` for calls in flight when the old run was killed. `JOURNAL_VERSION` stays 1.
- `TurnTee` masks what it forwards to a worker chat when given a `Redactor` (text and reasoning a line at a time, tool input, output and results, notifications, failures, and the published copies of confirmations and input requests) and reports awaiting input through `on_waiting` while a confirmation or input request is open (`awaitingInput` is derived from this, an approximation of `inputNeeded`); `reply` stays raw and nothing handed back to the inner session changes. `MaskingSink` wraps the turn sink handed to ahp-host's `StructuredTurn`, so its `StructuredOutput` call and result are masked while validation runs on the raw value.
- `[redact]` in `flow.toml` (`patterns`, and `builtin = true` by default for AWS keys, GitHub/GitLab/Slack tokens, PEM private keys, bearer tokens and `key=value` secrets); `Redactor`/`StreamMasker` in the engine; journal values and errors are masked through `RunOptions.redact`.
- `docs/plan.md` §8 splits M4 into M4a (this) and M4b and records the M4a result; README documents the provider and flow.toml.
- An opt-in live smoke test (`AHP_FLOW_LIVE_CONFIG=<flow.toml>`) runs a two-call script against the providers it configures.
- `python -m ahp_host_flow --config flow.toml` serves the provider over WebSocket (loopback, port 4323); `flow run --config flow.toml` runs a script through `ProviderDispatcher` on an in-process host.
- `FlowProvider`/`FlowSession`: a run is a session; workflows from workspace, user and host sources are customizations; a run's `log()`/`phase()` are system notifications, its result the final ```json message, its journal under `state_dir`; cancelling the main turn aborts the run; `permissionMode` and `effort` are `chatMutable`, and a worker chat's overlay is forwarded to its inner session; models from every configured provider are advertised with their vendor.
- `ProviderDispatcher`: each `agent()` call routes to a configured provider, opens a worker chat titled by `label` (or the prompt's first line), runs an inner session with a chat-scoped publisher, the model alias resolved and approval mode and effort in its config; `opts.schema` makes it a structured turn; cancelling the worker chat skips the call (null); the main chat shows the call with a subagent link; usage feeds the budget.
- A run starts from `/<workflow> [json args]`, `/workflow <name> [json args]` or a message starting with a fenced ```js script; anything else gets a short help reply.
- Workflow discovery: workspace (`.claude/workflows` in each working directory), user (`~/.claude/workflows`) and host (`[workflows] dirs`) sources, first match wins, `source:name` to choose; published as customization containers whose `prompt` children carry `_meta["ahp-py/flow"]` (kind, phases, source, shadowedBy), with `meta` read by parsing, never by running.
- `ahp_host_flow.bridge`: `ScopedPublisher` (an inner session's publisher scoped to its worker chat; session-wide calls are dropped, config changes land on the worker chat) and `TurnTee`, promoted from the M0 spike.
- Inner providers load lazily from `load = "module:attr"`: called with no arguments, with a `config` table as keyword arguments, or with `config=Path` for a config path.
- `ahp_host_flow.config`: `flow.toml` (state_dir, node, `[providers.<name>]` with `load`, `config`, `models`, `approval_mode`, `effort`; `[routing]` default and first-match rules on model alias, phase and label glob; `[workflows]` dirs and sources); unknown keys are errors.
- `agent()` re-validates a result against `opts.schema` (an object): a mismatch, an invalid schema or a reference outside it returns `null` with a `log()` line (`conformance/agent_schema.js`); `jsonschema` is now a direct dependency.
- `docs/plan.md` §8 records the M3 result (structured turns live in the host and providers; this package uses them from M4).
- `docs/plan.md` §8 records the M2 result.
- `conformance/resume.js`: a run killed with calls in flight resumes with identical results, its completed calls served from the journal and only the rest dispatched; `docs/format.md` pins the resume rules.
- `flow run --state-dir DIR --run-id ID --resume RUN_ID`: journal a run, name it, or resume one (the script path is optional with `--resume`); the run id is printed to stderr as `[run] <id>`.
- `Sandbox.run`/`run_script` write a journal when `RunOptions.state_dir` is set, accept `source=None` to resume from the journal's snapshot, and report the run id through `on_run_start` before the script starts.
- Resume (`engine/resume.py`): `RunOptions.resume_from` serves `agent()` calls from an earlier run's journal by key: the n-th call with a key gets the n-th record with it, re-stamped with the new ordinal, so later pipeline stages that issue in completion order still replay. The first call with no match logs where the cache ended and runs that call and every later one live. A resumed run takes the script, args, call cap and budget from the old journal (an edited script may be passed instead), resolves child workflows from its snapshots, and writes a new journal under a new run id that copies what it reused and every inherited child snapshot. A journal whose header cannot be written leaves no run directory behind.
- Engine journal (`engine/journal.py`): `<state_dir>/runs/<runId>/journal.jsonl`, append-only and flushed per record. A run header holds the script snapshot, args and the options that affect semantics; each child workflow's source is recorded when first resolved; each completed `agent()` call is recorded with its ordinal, key (sha256 of the canonical JSON of prompt, opts, phase and label), status (`ok`, `null` or `error`), value and output tokens. A torn final line is ignored on read.
- `RunOptions.state_dir`, `run_id` and `resume_from`: where a run's journal goes, the new run's id, and the run to resume from.
- Package skeleton and plan (`docs/plan.md`).
- Node discovery for the script sandbox: `AHP_FLOW_NODE`, else `node` on `PATH`; Node >= 20.
- acorn 8.19.0 (MIT), vendored under `sandbox/vendor/` to read `meta` without running a script.
- `engine.Dispatcher` (one `agent()` call in, its result out) and `EchoDispatcher`, which answers each call with its prompt.
- Script sandbox: a Node subprocess speaking line-delimited JSON-RPC; `meta` is read with acorn and must be a pure literal, without running the script.
- Scripts run in a fresh `node:vm` context per run. `agent()`, `log()` and `phase()` cross to Python, `args` passes through verbatim, and the context has a null-prototype global, so no Node API or host-realm object is reachable through constructors. This enforces determinism and is not a security boundary for hostile scripts. Messages to Node are ASCII-only JSON, so U+2028/U+2029 cannot split a frame.
- `Intl.DateTimeFormat` `format()` and `formatToParts()` without a date throw inside a script, like `Date.now()`.
- `Date.now()`, `Math.random()`, `new Date()` and `Date()` throw inside a script; dates built from explicit values still work.
- `flow run <path> [--args JSON] [--node PATH]`: runs a script with an echo agent and prints its return value as JSON.
- Spike: an inner provider session's turn runs in a worker chat (`open_tool_chat`), with its publisher scoped to that chat (integration test over the in-memory host).
- `docs/plan.md` §8 records the M0 spike result.
- Engine run options (`RunOptions`): concurrency cap (default min(16, CPUs − 2), at least 1), 1000 `agent()` calls per run, an output-token budget, a wall-clock timeout, workflow directories and a run root.
- `Budget` (summed from `AgentResult.usage["output_tokens"]`) and `AbortSignal`.
- Engine `Scheduler`: a concurrency semaphore on dispatch, the per-run `agent()` cap and the budget ceiling (both make `agent()` throw), an abort that cancels in-flight dispatches, and `null` on a dispatcher error, skip or abort. `AgentCall` carries `phase` and `label` for progress listeners.
- `agent()` dispatch goes through the scheduler: `RunOptions` caps concurrency and the number of calls, `budget` (`total`, `spent()`, `remaining()`) is readable synchronously in a script and `agent()` throws once it is spent, an `AbortSignal` (or cancelling the run) makes in-flight `agent()` calls return `null`, and `on_agent_start`/`on_agent_end` see each call with its `phase` and `label`.
- `parallel(thunks)` (a barrier; a throwing thunk resolves to `null`) and `pipeline(items, ...stages)` (no barrier between stages; a stage gets `(prev, item, index)`; a throw drops that item to `null`), at most 4096 items per call.
- Engine `Registry`: resolves `workflow(name)` over an ordered list of directories (first match wins) and `workflow({scriptPath})` inside a run root.
- A host-side exception while handling a script's `agent()` or `workflow()` request (a raising progress listener, say) is answered as an error, which the script sees as a throw, instead of leaving the call waiting forever.
- `workflow(nameOrRef, args)`: runs another script inline in the same run, sharing the concurrency cap, `agent()` counter, budget and abort; one level of nesting, enforced by running each child in its own `node:vm` context whose `workflow` always throws, with the run's counter, spent tokens and phase shared as host-held primitives, so no object crosses between the contexts; an unknown name, unreadable path or child syntax error throws in the parent.
- `RunOptions.timeout`: a per-run wall-clock limit; a script that overruns it (even `while (true) {}`) is stopped by killing Node and the run fails with `RunTimeoutError`.
- `flow run --concurrency N --budget TOKENS --timeout SECONDS --workflows DIR --root DIR`.
- `conformance/`: one script per plan §3 row covered so far (meta, agent, parallel, pipeline, phase/log/args, budget, workflow, determinism, caps), each with its expected result, run against a fake dispatcher.
- `docs/format.md` pins the script semantics M1 implements and lists the choices still to be checked against Claude Code; `docs/plan.md` §8 records the M1 result.

### Fixed

- An unexpected exception in the sandbox output reader now fails every pending request instead of leaving them waiting.
