# Changelog

## [Unreleased]

### Added

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
