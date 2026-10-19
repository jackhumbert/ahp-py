# Script format

The semantics of Claude Code workflow scripts that `ahp-host-flow` copies, as
implemented through milestone M5 (see [plan.md](plan.md) §3 and §8). Each
section names the conformance script that checks it, under `conformance/`.

A script is a strict-mode module body run in a fresh `node:vm` context. It may
use top-level `await` and `return`; its return value is the run result.

## meta

Script: `conformance/meta.js`; Python twin: `conformance/meta.py`.

`export const meta = {...}` must be a pure literal: `name`, `description`,
optional `whenToUse`, and `phases`, a list of `{title, detail?, model?}`. It is
read with a parser, without running the script. Unknown keys, at the top level
and inside `phases[]`, are accepted and ignored. `meta.x.schedule` is an ahp-py extension: see [Extensions](#extensions-ahp-py-only).

## agent

Script: `conformance/agent.js`; Python twin: `conformance/agent.py`.

`agent(prompt, opts)` returns the reply text. It returns `null` when the
dispatcher skips the call or fails; a failure also writes a `log()` line.
Ordinals are 0-based and taken when the call is issued. `opts` reach the
dispatcher verbatim (JSON-cloned), and the dispatcher also gets `label` and
`phase` as fields of the call. Unknown opts keys are passed on, not rejected.

When `opts.schema` is an object, a non-null result is validated against it
(JSON Schema, draft from `$schema`, 2020-12 when absent; references only inside
the schema). A mismatch or an unusable schema makes `agent()` return `null` and
logs `agent() #N failed: <reason>`; a non-object `opts.schema` is ignored.

Script: `conformance/agent_schema.js`; Python twin: `conformance/agent_schema.py`.

## parallel

Script: `conformance/parallel.js`; Python twin: `conformance/parallel.py`.

`parallel(thunks)` calls every thunk at once and waits for all of them (a
barrier). It never rejects. A thunk that throws, synchronously or by
rejecting, resolves to `null` at its position. At most 4096 items per call;
more fails with `parallel() takes at most 4096 items, got N`.

## pipeline

Script: `conformance/pipeline.js`; Python twin: `conformance/pipeline.py`.

`pipeline(items, ...stages)` runs each item through the stages in order with
no barrier between stages: an item moves on as soon as its previous stage
finishes. A stage is called as `(prev, item, index)`. If a stage throws, that
item's result is `null` and its later stages are skipped. With no stages the
items come back unchanged. At most 4096 items per call; more fails with
`pipeline() takes at most 4096 items, got N`.

## phase, log and args

Script: `conformance/phase_log_args.js`; Python twin: `conformance/phase_log_args.py`.

`phase(title)` sets the current phase; later `agent()` calls carry it.
`opts.phase` overrides it for one call, and `opts.label` names the call.
`log(line)` emits a narrator line. `args` is the value passed to the run,
verbatim (JSON, including `null`s).

## budget

Scripts: `conformance/budget.js`, `conformance/budget_unset.js`; Python twins: `conformance/budget.py`, `conformance/budget_unset.py`.

`budget` has `total`, `spent()` and `remaining()`. `spent()` is the sum of
`output_tokens` reported in the usage of each dispatcher result. `total` comes
from the run options. When it is unset, `total` is `null` and `remaining()` is
`Infinity`. `spent()` and `remaining()` are synchronous; the sandbox mirrors
the host's total on each `agent()` reply. Once `spent >= total`, `agent()`
throws `budget exhausted: S of T output tokens spent`.

Script: `conformance/nonfinite.js`; Python twin: `conformance/nonfinite.py`.

A value that crosses to the host (the return value, `agent()` opts, `workflow()`
args) goes through JSON, so `Infinity`, `-Infinity` and `NaN` become `null`:
returning `budget.remaining()` with no budget set gives `null`.

## workflow

Script: `conformance/workflow.js`; Python twin: `conformance/workflow.py`.

`workflow(nameOrRef, args)` runs another script inline in the same run. The
child shares the concurrency cap, the `agent()` counter, the budget and the
abort signal. The child sees `args` as its own `args`, and its return value is
the call's result. `nameOrRef` is a name, looked up in the engine registry (an
ordered list of directories, first match wins), or `{scriptPath}`, resolved
relative to the run root. An unknown name throws `unknown workflow: 'name'`; an
unreadable path or a child syntax error also throws in the parent. Nesting is
one level: a `workflow()` call inside a child throws `workflow() can only nest
one level: a child workflow cannot call workflow()`.

## workflow sources

Script: `conformance/workflow_sources.js`; Python twin: `conformance/workflow_sources.py`.

`workflow()` also takes a source-qualified name, `source:name`, where `source` is
one of `client`, `workspace`, `user` or `host`: it reaches only that source's
entries. A bare name tries the sources in that order, so a client-published
workflow wins over a workspace one, then user, then host. An unknown source
throws `unknown workflow source 'bogus' in 'bogus:child': use one of client,
workspace, user, host`; a qualified name with no entry throws `unknown workflow:
'workspace:child'`; a malformed name part throws `invalid workflow name`. A
client supplies `.js` workflows only.

## determinism

Script: `conformance/determinism.js`; Python twin: `conformance/determinism.py`.

`Date.now()`, `Math.random()`, `new Date()` and `Date()` without arguments
throw, as do `Intl.DateTimeFormat().format()` and `formatToParts()` without a
date. The message contains `is not allowed in a workflow script`. Dates built
from explicit values work. There is no `require`, `process`, `console` or timer.
The sandbox enforces determinism; it is not a security boundary.

## caps

Scripts: `conformance/caps.js`, `conformance/caps_concurrency.js`; Python twins: `conformance/caps.py`, `conformance/caps_concurrency.py`.

At most min(16, CPUs - 2) agents run at once (at least 1; configurable). At
most 1000 `agent()` calls per run; the next one throws `agent() limit reached:
at most N agent() calls per run`. At most 4096 items per `parallel()` or
`pipeline()` call.

## resume

Script: `conformance/resume.js`; Python twin: `conformance/resume.py`.

A run with a state dir writes `<state_dir>/runs/<runId>/journal.jsonl`. It is
append-only, flushed per record, and a torn last line is ignored on read. Record
kinds: a run header (script snapshot, filename, args, the call cap and budget);
one workflow snapshot per ref, written when the ref is first resolved; one
start record per live call (ordinal, key, and the dispatch handle: the provider
name and worker chat URI, or null when the dispatcher cannot say), written when
the call is dispatched and before its agent record; and one
agent record per completed call (ordinal, key, status `ok`, `null` or `error`,
value and output tokens); ahp-py adds a verb record per custom verb call and
the header's `keyMode` (see Extensions). The key is the sha256 of the canonical JSON (sorted
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

## Python workflows (ahp-py extension)

A `.py` workflow runs the same semantics in a `python -I` runner. Each
conformance script has a `.py` twin (listed under each section above), checked
against the same sidecar.

| Surface | JavaScript | Python |
|---|---|---|
| meta | `export const meta` literal, read via acorn | top-level `meta = {...}`, read with `ast.literal_eval`, assigned once, a dict of JSON values, never executed |
| entry | module body | `async def main(args)` |
| agent | `opts` object | keyword arguments; returns an awaitable issued at call time; errors surface on await |
| parallel | array of thunks | list of zero-argument callables, called at once in order; a plain return value is used as is |
| pipeline | stages `(prev, item, index)` | the same, always three positional arguments; the first stage is called at once |
| args | `undefined` when absent | `None` when absent or null |
| budget.remaining() | `Infinity` | `math.inf` |
| non-finite numbers | `JSON.stringify` turns `Infinity` and `NaN` into `null` | the same wherever a value crosses (the return value, `agent()` opts, `workflow()` args, a child's value), so `return {'left': budget.remaining()}` gives `{"left": null}`; `log()` and `phase()` print them as `Infinity`, `-Infinity`, `NaN` |
| cancellation | an escaping rejection is the script's error | a `CancelledError` the script raises itself (it awaits a task it cancelled) is a script error, "the script was cancelled", in the run and in an in-process child; cancelling the run itself still cancels it |
| errors | `Error`, `TypeError`, `RangeError` | `RuntimeError`, `TypeError`, `ValueError`, same messages |
| determinism | `Date.now`, `Math.random` banned | nothing banned: Python cannot ban `time.time()` or `random`; resume matches calls by key, so a nondeterministic script costs cache hits, not wrong results (the determinism row's sidecar has a `py_result`) |
| workflow() | a name resolves to `.js` or `.py`, JS wins a tie in one directory; a child in the other language runs in a runner of its own sharing counter, phase, budget, caps and abort; one level of nesting | the same |
| trust | any source | `.py` only from the host and user sources unless `[workflows] python_sources` includes `workspace` (then only the workspace's `.claude/workflows` directories, and only while `[workflows] sources` includes it); never from a client; fenced message scripts are JS only |

The runner is a separate `python -I` process, not a security boundary. `-I`
ignores `PYTHONPATH`, so `ahp-host-flow` must be installed in that interpreter
(an editable install counts).
`print()` goes to stderr; stdout carries the protocol.

## Extensions (ahp-py only)

Claude Code has none of these. It ignores `opts.x` and leaves `ask`,
`approve` and `canvas` undefined (the plan §3 probe), so a script that uses
them runs unchanged under Claude Code only through the shim below. A Python
workflow runs on ahp-py only. Everything here is opt-in: a script that uses
none of it behaves exactly as described above.

### ask, approve and canvas

Scripts: `conformance/extensions/gates.js`, `conformance/extensions/gates.py`,
`conformance/extensions/no_gates.js`, `conformance/extensions/no_gates.py`,
`conformance/extensions/gate_errors.js` and `conformance/extensions/gate_errors.py`.
`gates` runs them with answers, `no_gates` with no way to answer, `gate_errors`
checks the argument errors.

- `ask(question, {choices?, schema?})` pauses the run until a client answers
  and returns the answer, or `null` when it is declined or the run is aborted.
  `choices` is a non-empty array of strings; `schema` is a JSON Schema object;
  not both. A `null` or absent option counts as not given. On the run's main
  chat an answer that does not fit `choices` or `schema` is asked again, with
  the reason shown, up to 3 times in all (`ASK_ATTEMPTS`); after the third the
  gate gives `null` with a `log()` line. The engine re-validates whatever comes
  back, whoever answered: an answer that does not fit becomes `null` with a
  `log()` line. With neither option the answer is a string.
- `approve(summary, {diff?})` returns `true` or `false`, and `false` when it is
  declined or aborted. `diff` is a string or an array of
  `{path, before?, after?}`.
- `canvas(url, {title?})` shows a live view of an http(s) URL on the run. It
  returns nothing, never waits, and is ignored with a log line where no client
  can show it.
- Errors are `TypeError`s (JavaScript and Python alike; in Python `ask()` and
  `approve()` raise on await, and the options are keyword arguments), with these messages:
  `ask() needs a question string`, `ask() choices must be a non-empty array of
  strings`, `ask() schema must be an object`, `ask() takes choices or schema,
  not both`, `approve() needs a summary string`, `approve() diff must be a
  string or an array of {path, before?, after?}`, `canvas() needs an http(s)
  URL`, `canvas() title must be a string`. In JavaScript, an `opts` that is not
  an object gives `<verb>() opts must be an object`. Arguments are checked in
  the order above, before anything reaches the host. The host checks the same
  shapes again, and refuses unknown option keys, whatever the runner sent (a
  `toJSON` or a replaced `JSON.stringify` can change what is sent): a refused
  gate throws an `Error` with the host's reason and takes no gate ordinal.
- Gates have a counter of their own, apart from the `agent()` ordinals and the
  1000-call cap. The host assigns it when the request line arrives, and it is
  shared with `workflow()` children, like the other counters.
- The answer is masked once before the script sees it (credential masking,
  plan §4.9), and the masked answer is what the journal records and a resumed
  run replays. The prompt shown to the client is masked too. A script that asks
  for a secret therefore gets `[redacted]` back.
- Journal and resume: every gate writes a `gate` record before it asks (status
  `open`) and after the answer (status `answered`). Its key is a hash of the
  kind, the question and the options. On resume an answered gate whose key
  matches replays its answer without asking; a gate that was open is asked
  again. Gates never end the `agent()` cache.
- A run with nobody to answer (a headless `flow run`, the library API) fails an
  `ask()` or `approve()` with an error that says so, and the verb throws it.

### Custom verbs

Scripts: `conformance/extensions/verbs.js` and `conformance/extensions/verbs.py`.
This is an ahp-py extension outside Claude Code's format: Claude Code has no such
globals, so a stock script is unaffected.
A deployment may declare verbs (`RunOptions.verbs`: a name and a handler each).
The script calls one as a global, `await tool('search', {q})`, with JSON arguments
and gets the handler's JSON value; no model is involved. A handler that raises, or
returns something that is not JSON, makes the call throw with its (masked)
message. A name that is not declared stays undefined. A verb may replace `ask`,
`approve` or `canvas` for that run; the replaced built-in is then not defined in
that run. Verbs have a counter of their own, take no agent slot or budget, are
journaled, and a finished call replays on resume. In Python the same call is
`ahp_host_flow.verbs.verb(name, *args)`.

### [resume] keys

An ahp-py extension outside Claude Code's format. `[resume] key` in flow.toml
(`"default"`, `"model"` or an import path) adds a JSON `extra` to each `agent()`
call's key, so a cached call replays only for the same value. With the default
the keys are exactly those described under resume above, and a stock script is
unaffected. The journal header records the mode as `keyMode`.

### opts.x.provider

`agent(prompt, {x: {provider: 'name'}})` sends that one call to the configured
provider of that name, ahead of any routing rule and the default. A non-object
`x` or a provider that is not a non-empty string is ignored. An unconfigured
name fails the call: `null` plus a `log()` line that lists the configured names.

### opts.x.chat

`agent(prompt, {x: {chat: 'name'}})` runs the call as a follow-up turn in the
named worker chat and its inner session, created by the first call that names
it. Calls that name one chat are serialized, so each sees the earlier turns.
The chat closes when the run ends. It works for in-process providers only: a
fleet or remote provider, and `isolation: 'worktree'`, fail the call. The inner
session is created by the first call, so the chat keeps that call's config
(`effort`, and the approval mode): a later call to the same name that asks for
another one runs with the first, and a `log()` line says so. `model` is sent
with each message, so it may change from call to call. On resume
the chat and its session are created again; the inner agent remembers earlier
turns only if its provider can resume sessions.

### opts.x.attach

`agent(prompt, {x: {attach: [...]}})` hands the call attachments on its
message. An entry is `{kind: 'file', path, label?}` or
`{kind: 'result', value, label?}` (a prior result, as an embedded JSON
resource). The prompt text is not changed. A file path is resolved with symlinks
followed and must be a regular file inside the run's working directories;
anything else fails the call before a worker chat opens, as does a bad entry
(`null` plus a log line). In-process providers only.

### opts.x.cwd

`agent(prompt, {x: {cwd: 'packages/api'}})` runs the call in that folder. It is an
ahp-py extension, not an `isolation` value (those are Claude Code's). In-process,
the path is resolved with symlinks followed and must be a directory inside the
run's working directories; a relative path is taken against the first. With
`isolation: 'worktree'` it is the worktree's base. A named chat keeps its first
folder (a log line says so). Fleet and remote need an absolute path or a `file:`
URI (a relative one fails the call): fleet passes it as `folder` to `start_session`,
and the node's roots decide; remote sends it as the session's working directory. A bad folder fails the call (`null` plus a log
line). An unknown `isolation` value logs a line, and fleet and remote log that
`isolation: 'worktree'` is not applied.

Scripts: `conformance/extensions/callcwd.js` and `conformance/extensions/callcwd.py`.

### opts.x.detail (approve)

`approve(summary, {x: {detail: true}})` (Python: `approve(summary, x={'detail': True})`)
resolves to `{approved, reason, answeredBy}` instead of a bool: `reason` is the
human's explanation for a denial (masked) and `answeredBy` the answering person's
principal, each `null` when unknown. A plain `approve()` still returns a bool, and
its gate key and journal record are unchanged. `x` must be an object and
`x.detail` a boolean, else `approve()` throws `approve() x must be an object` or
`approve() x.detail must be a boolean`. Claude Code ignores `opts.x`, so there
`approve()` stays a bool.

Scripts: `conformance/extensions/approve_detail.js` and `conformance/extensions/approve_detail.py`.

### meta.x.schedule

`meta.x.schedule` is a cron string or `{expression, timeZone}`; the time zone
is `UTC` when absent. The host offers it on the workflow's customization and
never creates an automation itself. A client applies it with
`ahp_protocol.ext.flow.with_offered_schedule`. An invalid schedule is left out,
the workflow is still listed, and the reason is in the customization's
`warnings` and in the host log.

### meta.x.args

`meta.x.args` is a JSON Schema literal describing the workflow's `args`. This is an
ahp-py extension outside Claude Code's format, which ignores it. The host publishes
it as `_meta["ahp-py/flow"].argsSchema` on the workflow's customization, so a client
can build a form. An invalid schema is left out, the workflow is still listed, and
the reason is a catalog warning in the customization's `warnings` and the host log.

### Typed workflow starts

A client may start a workflow with an ordinary `chat/turnStarted` whose message
`_meta["ahp-py/flow"]` is `{workflow: "<source:name>", args, runId?}`. The message
text stays `/name {json}`, so a host that does not know the key runs the text. The
typed start wins over the text. Args are validated against `argsSchema` for typed and
text starts; a mismatch fails the turn with errorType `flow.badArgs` and nothing is
dispatched. A client-chosen `runId` is used when it is free; otherwise the host mints
one and reports it in `runStarted`.

### The shim

Script: `conformance/shim/dual_target.js`. It has no Python twin: Claude Code
runs only JavaScript.

`shim/flow-shim.js` defines `ask`, `approve` and `canvas` where the runtime
lacks them. Paste it right after `export const meta = {...}`. The package ships
it as `ahp_host_flow/shim/flow-shim.js`; `flow shim` prints it. Under ahp-host-flow
the real functions are kept. Under Claude Code `ask()` and `approve()` fall back
to an `agent()` call with a schema, `canvas()` does nothing, and `opts.x` is
ignored. `FLOW_SHIM = { askDefault: undefined, approveDefault: undefined }`
sets values to return instead of asking an agent; `approveDefault: false` is
the safe choice for destructive steps. If you only need `ask`, a shorter
alternative is `const ask = globalThis.ask ?? (async () => null)`.

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
- Ordinals and the current phase are assigned by the host when a request
  arrives (issue order within one runner; across runners, arrival order), and
  any refused call takes no ordinal.
