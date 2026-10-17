# Script format

The semantics of Claude Code workflow scripts that `ahp-host-flow` copies, as
implemented through milestone M2 (see [plan.md](plan.md) §3 and §8). Each
section names the conformance script that checks it, under `conformance/`.

A script is a strict-mode module body run in a fresh `node:vm` context. It may
use top-level `await` and `return`; its return value is the run result.

## meta

Script: `conformance/meta.js`.

`export const meta = {...}` must be a pure literal: `name`, `description`,
optional `whenToUse`, and `phases`, a list of `{title, detail?, model?}`. It is
read with a parser, without running the script. Unknown keys, at the top level
and inside `phases[]`, are accepted and ignored.

## agent

Script: `conformance/agent.js`.

`agent(prompt, opts)` returns the reply text. It returns `null` when the
dispatcher skips the call or fails; a failure also writes a `log()` line.
Ordinals are 0-based and taken when the call is issued. `opts` reach the
dispatcher verbatim (JSON-cloned), and the dispatcher also gets `label` and
`phase` as fields of the call. Unknown opts keys are passed on, not rejected.

When `opts.schema` is an object, a non-null result is validated against it
(JSON Schema, draft from `$schema`, 2020-12 when absent; references only inside
the schema). A mismatch or an unusable schema makes `agent()` return `null` and
logs `agent() #N failed: <reason>`; a non-object `opts.schema` is ignored.

Script: `conformance/agent_schema.js`.

## parallel

Script: `conformance/parallel.js`.

`parallel(thunks)` calls every thunk at once and waits for all of them (a
barrier). It never rejects. A thunk that throws, synchronously or by
rejecting, resolves to `null` at its position. At most 4096 items per call;
more fails with `parallel() takes at most 4096 items, got N`.

## pipeline

Script: `conformance/pipeline.js`.

`pipeline(items, ...stages)` runs each item through the stages in order with
no barrier between stages: an item moves on as soon as its previous stage
finishes. A stage is called as `(prev, item, index)`. If a stage throws, that
item's result is `null` and its later stages are skipped. With no stages the
items come back unchanged. At most 4096 items per call; more fails with
`pipeline() takes at most 4096 items, got N`.

## phase, log and args

Script: `conformance/phase_log_args.js`.

`phase(title)` sets the current phase; later `agent()` calls carry it.
`opts.phase` overrides it for one call, and `opts.label` names the call.
`log(line)` emits a narrator line. `args` is the value passed to the run,
verbatim (JSON, including `null`s).

## budget

Scripts: `conformance/budget.js`, `conformance/budget_unset.js`.

`budget` has `total`, `spent()` and `remaining()`. `spent()` is the sum of
`output_tokens` reported in the usage of each dispatcher result. `total` comes
from the run options. When it is unset, `total` is `null` and `remaining()` is
`Infinity`. `spent()` and `remaining()` are synchronous; the sandbox mirrors
the host's total on each `agent()` reply. Once `spent >= total`, `agent()`
throws `budget exhausted: S of T output tokens spent`.

## workflow

Script: `conformance/workflow.js`.

`workflow(nameOrRef, args)` runs another script inline in the same run. The
child shares the concurrency cap, the `agent()` counter, the budget and the
abort signal. The child sees `args` as its own `args`, and its return value is
the call's result. `nameOrRef` is a name, looked up in the engine registry (an
ordered list of directories, first match wins), or `{scriptPath}`, resolved
relative to the run root. An unknown name throws `unknown workflow: 'name'`; an
unreadable path or a child syntax error also throws in the parent. Nesting is
one level: a `workflow()` call inside a child throws `workflow() can only nest
one level: a child workflow cannot call workflow()`.

## determinism

Script: `conformance/determinism.js`.

`Date.now()`, `Math.random()`, `new Date()` and `Date()` without arguments
throw, as do `Intl.DateTimeFormat().format()` and `formatToParts()` without a
date. The message contains `is not allowed in a workflow script`. Dates built
from explicit values work. There is no `require`, `process`, `console` or timer.
The sandbox enforces determinism; it is not a security boundary.

## caps

Scripts: `conformance/caps.js`, `conformance/caps_concurrency.js`.

At most min(16, CPUs - 2) agents run at once (at least 1; configurable). At
most 1000 `agent()` calls per run; the next one throws `agent() limit reached:
at most N agent() calls per run`. At most 4096 items per `parallel()` or
`pipeline()` call.

## resume

Script: `conformance/resume.js`.

A run with a state dir writes `<state_dir>/runs/<runId>/journal.jsonl`. It is
append-only, flushed per record, and a torn last line is ignored on read. Record
kinds: a run header (script snapshot, filename, args, the call cap and budget);
one workflow snapshot per ref, written when the ref is first resolved; one
start record per live call (ordinal, key, and the dispatch handle: the provider
name and worker chat URI, or null when the dispatcher cannot say), written when
the call is dispatched and before its agent record; and one
agent record per completed call (ordinal, key, status `ok`, `null` or `error`,
value and output tokens). The key is the sha256 of the canonical JSON (sorted
keys, no whitespace) of `{prompt, opts, phase, label}`.

Resume serves `agent()` calls from the journal by content: the n-th call
issued with a given key gets the n-th record with that key, whatever its
ordinal, and the record is re-stamped with the new call's ordinal. Matching by
key rather than ordinal matters because ordinals are deterministic only for
calls issued synchronously. A later pipeline stage issues in completion order,
and cached calls complete at once, so on resume those calls arrive in a
different order. The first call with no unconsumed record of its key (a changed
prompt or opts, or a call the old run never made) makes that call and every
call issued after it live, and logs `resume: cache ended at agent() #N (no
cached call matches); K calls replayed, this call and later ones run live`.
This keeps Claude Code's longest-unchanged-prefix rule for calls whose order is
deterministic. Calls with identical keys are interchangeable, since they are
the same request. Cached calls count toward the 1000-call cap and
add their recorded output tokens to `budget.spent()`, but take no concurrency
slot, never reach the dispatcher or progress listeners, and log nothing. A
recorded null or error replays as null. Calls in flight when a run is killed keep
a start record without an agent record: they are not replayed and rerun, and
resume logs `resume: agent() #N started on <provider> (<chat>) but never
finished; not replayed` for each (`resume: agent() #N started but never
finished; not replayed` when the handle is null). Cached calls write no start
record.

The resumed run uses the old run's args, call cap and budget, and runs the
journal's script snapshot unless an edited script is given. Child workflows
resolve from the snapshots, not the filesystem. It writes a new journal under a
new run id that copies the reused records and, as soon as it opens, every
child snapshot of the old run, including any it has not reached yet. So it can
be resumed in turn without re-reading a file. A run with no script snapshot cannot be resumed without a script.

## Choices not yet checked against Claude Code

- `pipeline`'s first stage gets `prev` equal to the item.
- A non-function element of `parallel()` resolves to `null`, because calling it throws.
- The agent and budget caps are checked when `agent()` is issued, so spend may
  overshoot by the calls already issued. A refused call consumes no ordinal.
- `phase()` is global to the run, so a child workflow's `phase()` carries into
  the parent.
- A child workflow runs in a `node:vm` context of its own. It has its own copy
  of the hooks over the run's shared state (agent counter, spent tokens,
  phase), and its own `workflow` always throws. No object crosses between the
  two contexts: args go in and the result comes back as JSON. So no route
  through a shared hook's function or return value leads back to the parent's
  `workflow`. A child's globals are its own. The sandbox enforces determinism
  and the format's rules; it is not a security boundary against a hostile
  script.
- Abort makes in-flight and later `agent()` calls return `null` while the
  script runs on to its end, bounded by the timeout. Cancelling the run task
  kills Node.
- Ordinals are deterministic only for calls issued synchronously (`parallel`
  thunks, a pipeline's first stage, sequential awaits). A call in a later
  pipeline stage, or one issued after awaiting a call that runs concurrently
  with others, is numbered in completion order. Resume matches by key, so this
  changes only the ordinals in the new journal, not what is cached. Whether
  Claude Code's resume matches by order or by content is unchecked.
- Cached calls complete at once, so near the budget ceiling a resumed run's
  issue-time budget check can refuse a call the original run made.
- A recorded null or error is replayed, not retried.
