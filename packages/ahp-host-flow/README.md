# ahp-host-flow

Run Claude Code workflow scripts (`agent()`, `parallel()`, `pipeline()`,
`phase()`, …) as AHP sessions. Each `agent()` call is a child chat driven by
any provider the host can load: Claude through `ahp-host-claude`, or any ACP
agent through `ahp-host-acp`.

Status: pre-alpha. M4a is in: the provider core. M5 is in: Python workflows. M6
is in: runs survive a restart. M7 is in: fleet dispatch (calls run as sessions
on gateway nodes) and remote providers. `flow.toml` configures
providers, routing and workflow sources; a run is a session, and each `agent()`
call is a worker chat on a routed provider. Most of M4b is in too: typed
progress, credential masking, git worktrees, journal start records and the
fan-out corpus tests. The rest of M4b (provider factories in `ahp-host-claude`
and `ahp-host-acp`, the subagent prompt preamble, live watching of workflow
directories) is next. See [docs/plan.md](docs/plan.md) and
[docs/format.md](docs/format.md).

## Running the provider

```sh
python -m ahp_host_flow --config ~/.config/ahp/flow.toml
```

This serves the provider over WebSocket, bound to loopback (`127.0.0.1`) on port
4323. `--port` and `--bind` change that, `-v` turns on debug logging, and
`--token-file PATH` requires the connection token held in that file (a file,
never a command-line value, so it stays out of process listings). A `--bind`
address that is not loopback is refused unless `--token-file` is given.
`--root PATH` (repeatable) names a folder the inner providers may work in; see
[flow.toml](#flowtoml).

`--wire-log PATH` appends every frame as ahp-inspector JSONL; besides ahp-host's
credential redaction, every text in it is masked by the flow.toml's `[redact]`
rules, so a secret in a script or its args reaches neither the journal nor the log.

`--ping-interval SECONDS` (default 20, `0` = off) sends a WebSocket ping that
often so an idle connection survives a proxy. `--max-size BYTES` (default 16 MiB,
matching ahp-client; `0` = no limit) caps one frame. A non-loopback `--bind` with
`--token-file` works, and the token is accepted as `Authorization: Bearer <token>`
or as `?tkn=`.

## Behind a proxy

Put the host behind a reverse proxy by binding it off loopback with `--token-file`.

- Send the token in the `Authorization: Bearer` header rather than `?tkn=`;
  proxies log the request URI.
- To take the principal from a header the proxy asserts, see
  [Embedding it behind a proxy](https://github.com/jackhumbert/ahp-py/blob/main/packages/ahp-host/README.md#embedding-it-behind-a-proxy).
  A forwarded header is evidence only if the socket is reachable only through the
  proxy.
- Set the proxy's idle/read timeout above `--ping-interval` (or keep pings on), and
  its body/frame limit at or above `--max-size`.

## Typed workflow starts

A client can start a workflow without building the `/name {json}` text. Put
`_meta["ahp-py/flow"]` = `{workflow, args, runId?}` on an ordinary `chat/turnStarted`;
the message text stays `/name {json}` for clients that do not know the key, and the
typed start wins over the text.

- A workflow's `meta.x.args` (a JSON Schema) is published as `argsSchema` on its
  prompt customization, so a client can build a form.
- Args are validated against that schema for typed and text starts alike. A mismatch
  ends the turn with errorType `flow.badArgs` and nothing is dispatched.
- `runId` names the run when it is free; otherwise the host picks one.

From ahp-client, `session.workflows()` lists them and
`session.start_workflow(name, args, run_id=None)` starts one. In ahp_protocol,
`workflows_of` reads the listing from a customization container.

## Running a script from the command line

```sh
flow run script.js|script.py --config flow.toml [--args JSON] [--root DIR]
         [--keep-source] [--wire-log PATH]
```

With `--config`, the script runs through the configured providers on an
in-process host, and its return value is printed as JSON. The run's concurrency,
budget, timeout, workflow directories and state directory come from the config,
so `--concurrency`, `--budget`, `--timeout`, `--workflows`, `--state-dir`,
`--run-id` and `--resume` cannot be combined with `--config`. Node >= 20 is
needed for JavaScript scripts (`node` in `flow.toml`, `--node`,
`AHP_FLOW_NODE`, or `PATH`); a `.py` script needs no Node.

Nobody is connected to answer a confirmation or question an inner session asks
in a worker chat, so the run stops at the first one with an error naming the
call, rather than waiting forever. It is never auto-approved: set
`approval_mode` on the provider in `flow.toml` (for example `"acceptEdits"` or
`"auto"`) so the session does not ask.

`--wire-log PATH` writes the in-process host's frames as ahp-inspector JSONL,
masked by the flow.toml's `[redact]` rules as well as ahp-host's credential
redaction (it needs `--config`); `python -m ahp_host_flow --wire-log PATH` does
the same for a served host. `--keep-source` keeps the script and its args in the
journal as they are (see "The journal holds the script masked" below); with
`--config` it overrides `[journal] keep_source`.

`flow run` takes a `.js` or a `.py` script. Without `--config`, it is echo-only: every `agent()` call returns its
prompt, which is enough to try the engine without a model. It also takes
`[--concurrency N] [--budget TOKENS] [--timeout SECONDS] [--workflows DIR]
[--state-dir DIR] [--run-id ID] [--resume RUN_ID] [--keep-source]`. Its log
lines, errors and journal are masked by the builtin credential shapes. A journal
it wrote masked is resumed with the script path and the same `--args` again
(`flow run script.py --resume RUN_ID --args JSON ...`): both are checked against
the sha256 the journal recorded, and a different script or different args are
refused with a line naming which. `--budget` still comes from the journal.

## Starting a run from a client

Send the flow session a message:

- `/<workflow> [json args]` runs a workflow by name.
- `/workflow <name> [json args]` is the same, spelled out.
- `/<source>:<name>` (`workspace`, `user` or `host`) picks the source explicitly,
  for example `/host:fanout`.
- A message that starts with a fenced ```` ```js ```` block runs that script; JSON
  args may follow the closing fence.

Anything else gets a short help reply listing the workflows found.

## Where workflows come from

Four sources are searched, and the first match wins:

1. **client**: `workflows/*.js` inside an Open Plugins plugin a connected client
   publishes (`activeClients[].customizations`), read from the client with
   `resourceList` / `resourceRead`. Opt-in: add `"client"` to `[workflows] sources`.
2. **workspace**: `.claude/workflows/*.js` (and `*.py`, see below) in each of the session's working
   directories.
3. **user**: `~/.claude/workflows/*.js` and `*.py`.
4. **host**: the directories in `[workflows] dirs` of `flow.toml` (`*.js` and `*.py`).

Inside a script, `workflow('name')` searches the same sources in the same order, and
`workflow('host:name')` (or `client:`, `workspace:`, `user:`) reaches one source only.

A `.py` workflow is arbitrary code on the host, so by default it loads only from
the host and user sources; see Python workflows. A `.py` file in a client's
plugin is never read, and `python_sources` can never include `client`.

`[workflows] sources` selects which are used and their order; the default is
`["workspace", "user", "host"]`, so a shared host runs a client-published
workflow only when the operator opts in. Each source is published to clients as
a customization container, with one `prompt` child per workflow, so clients list
them as slash commands. A shadowed name is still listed, marked in its
`_meta["ahp-py/flow"]`. The directories are re-read at the start of each run.

The local directories (workspace, user, host) are also watched: adding, editing
or deleting a `*.js` or `*.py` there re-publishes the customizations without a
message. `[workflows] watch_interval` in flow.toml is the scan interval in
seconds (default `2.0`; `0` turns watching off). Client sources are not polled;
they follow the client's own republish.

Precedence is client, workspace, user, host. A client workflow runs as `/name`
or, to pick it explicitly, `/client:name`. It is listed in a container of the
provider's own, "Workflows (<plugin>)" with id `ahp-host-flow.client.<plugin id>`,
beside the client's plugin (the host owns and re-expands it). It is re-published
when the client republishes its plugin. The run snapshots the script text the
client served, so resume never needs the client. `workflow('name')` inside a script reaches client workflows too (`.js` only).

## Python workflows

A workflow may be a `.py` file instead of a `.js` one. The verbs keep their
JavaScript meaning; `agent` takes keyword options and `parallel` takes
zero-argument callables:

```python
meta = {"name": "review", "description": "...", "phases": [{"title": "Find"}]}


async def main(args):
    phase("Find")
    found = await parallel(
        [lambda f=f: agent(f"review {f}", schema=FINDINGS) for f in args["files"]]
    )
    return [x for x in found if x]
```

`meta` must be a literal; it is read with `ast.literal_eval` without running
the file. The verbs (`agent`, `parallel`, `pipeline`, `phase`, `log`, `args`,
`budget`, `workflow`) are globals in the script. For editors and type checkers,
`from ahp_host_flow.verbs import agent, parallel` gives typed stubs; they raise
a clear error if called outside a runner. The script runs in a separate
`python -I` process (standard library only), which is not a security boundary.
`-I` ignores `PYTHONPATH`, so `ahp-host-flow` must be installed in that
interpreter (an editable install counts); being importable through `PYTHONPATH`
is not enough.

Both runners, Node and Python, start with an allowlisted environment, not the
host's: `PATH`, the locale (`LANG`, `LANGUAGE`, `LC_*`), `TZ`, the temp directory
(`TMPDIR`, `TMP`, `TEMP`) and, on Windows, `SYSTEMROOT`, `SYSTEMDRIVE`, `WINDIR`,
`COMSPEC` and `PATHEXT` (`sandbox.client.RUNNER_ENV`). A script that reaches
`os.environ` or `process.env` finds no API key or token of the host's.

Trust: a `.py` file is arbitrary code on the host, so by default `.py`
workflows load only from the host and user sources. For a project's own
`.claude/workflows/*.py`, add `workspace` to `python_sources`:

```toml
[workflows]
python_sources = ["user", "host", "workspace"]
```

`workspace` grants only the workspace's `.claude/workflows` directories, not the
rest of the working directory, so `workflow({scriptPath})` cannot load other
`.py` files from the repository; it grants nothing while `[workflows] sources`
excludes `workspace`. A client never supplies one, and a fenced message script
is JavaScript only.

Determinism: Claude Code's sandbox bans `Date.now()` and `Math.random()`;
Python cannot ban `time.time()` or `random`, so nothing is banned. Resume
matches calls by content, so a script that varies its prompts between runs
costs cache hits on resume, not wrong results.

`workflow()` takes a name that resolves to a `.js` or a `.py` (the `.js` wins a
tie in one directory), and works in both directions: a child in the other
language runs in its own runner and shares the run's counter, phase, budget and
caps, with only JSON crossing.

### Library API

Python code that is not a workflow file can drive the same scheduler and
journal in-process, with no runner:

```python
import ahp_host_flow
from ahp_host_flow.engine import RunOptions

async with ahp_host_flow.run(dispatcher, RunOptions(budget_total=50_000)) as r:
    r.phase("Find")
    found = await r.parallel([lambda: r.agent("find bugs"), lambda: r.agent("find leaks")])
    summary = await r.agent(f"summarise {found}")
```

## What a client sees

- Each `agent()` call opens a worker chat, titled by the call's `label` (or the
  first line of its prompt), and the main chat shows the call with a link to it.
- Tool approvals appear in the worker chat that needs them.
- Cancelling a worker chat skips that call: `agent()` returns `null` and the run
  carries on.
- Cancelling the run aborts it.
- `permissionMode` and `effort` can be set per chat; a worker chat's value is
  passed to its inner session.
- `log()` and `phase()` appear as system notifications, and the run's result is
  the final message.
- Progress is typed. `_meta["ahp-py/flow"]` carries `runStarted`,
  `phaseStarted`, `phaseFinished` and `runFinished` on the system
  notifications, and `stepStarted`, `awaitingInput` and `stepFinished` on each
  `agent` tool call (the running call links its worker chat; the result carries
  the status, the structured result and usage). A stock client shows the same
  text without them. `awaitingInput` comes from the session's `inputNeeded`,
  filtered to the worker chat, and the step's `waiting` entries (kind and
  chat of each open request, `RunView.waiting` in the folded view) say what it
  is blocked on, including an MCP server's authentication. The shapes belong to `ahp_protocol.ext.flow`, whose
  `run_view` folds a chat's state into phases, steps and their status;
  `ahp-client` gives it as `session.flow_view()` (and `chat.flow_view()`).
- A run's main chat is quiet while a step runs; the step's work is on its
  worker chat. So that a client's idle timeout does not fire, the host sends a
  heartbeat while at least one `agent()` step is in flight: every
  `[limits] heartbeat` seconds (default 30, below `ahp-client`'s 300 s default
  `idle_timeout`) a `chat/activityChanged` on the main chat ("Running 2 steps")
  carrying `_meta['ahp-py/flow'] = {'heartbeat': {'running': k, 'tick': n}}`;
  the activity is cleared when the run ends. A stock client ignores the `_meta`
  and shows the activity text. In `ahp-client`'s `prompt()`, `idle_timeout` is
  reset by every event the stream yields for the turn, by a
  `chat/activityChanged` on its chat, and by input-needed changes for its chat,
  so the default needs no change; a step silent for longer than `heartbeat`
  and the timeout together (a host without the heartbeat, or a larger
  `heartbeat`) still needs a longer `idle_timeout` or `None`.
- `agent()` accepts `isolation: 'worktree'`. When the run's working directory is
  a git repository, the inner session runs in a new git worktree under
  `<state_dir>/runs/<runId>/worktrees/<ordinal>`. It is removed afterwards if it
  has no changes; otherwise it is kept and its path is logged. Outside a git
  repository the call logs why and returns `null`.
- With `state_dir` set, the journal writes a `started` record (ordinal, key,
  provider and worker chat) when a call is dispatched, before its result. A
  resume does not replay a call that never finished; it logs it.

## Runs survive a restart

A run continues after the host restarts when the host has a session store:
`FileSessionStore` plus `host.restore()`, which `python -m ahp_host_flow` or an
embedder sets up. `python -m ahp_host_flow` stores sessions under `state_dir`
(`sessions/`, plus the `sequence` file) unless you pass `--no-store`. Without a
store nothing is kept and a restored chat is only transcript.

The host fails a run's main turn that a restart cut off (`errorType`
`host.restart`, resumable) and resumes it through `chat/turnResume`: by itself,
once, after a graceful restart that drained past its deadline (`ahp-node
restart`, `Host.drain`); after a crash, when a client resumes the failed turn.
`FlowSession.resume_turn` then relaunches the run from its journal under the
same turn id (`RunOptions.continue_run`):

- finished calls replay from the journal by key;
- a call that was cut off (a `started` record but no result) runs again, with a
  log line naming it, under a tool call id of its own (`agent-r<k>-…`); the
  cut-off one was cancelled when the host failed the turn.

Named chats (`opts.x.chat`) keep their session across a restart on providers
that resume sessions (Claude by session id, ACP by `session/load`): the run
records each named chat's provider, worker chat, config, folder and the inner
session's resume state at the end of every turn, and the first live call that
names the chat after the restart resumes that session in the same worker chat,
so the agent still remembers the earlier turns. A log line says so
(`agent() #N: opts.x.chat 'name' resumed its session on provider 'p' after the
restart`). On any other provider, or when the chat or its state is gone, or the
session cannot be resumed, the chat is recreated and starts without the earlier
turns, with a log line saying so. The state is optional in the resume state
(`namedChats` on the run entry; `RESUME_STATE_VERSION` stays 1).

A call without a name that a restart cut off continues in its worker chat rather
than starting over, on a provider that resumes sessions and turns, when the run
recorded the inner session's state at the start of the turn (`inflight` on the
run entry): the session is resumed on the old worker chat and the turn picked up
where it stopped, with a log line saying so. A call with a schema or a worktree,
or one with no recorded state, or on any other provider, is dispatched again.

What does not continue:

- Other worker chats: each is failed by the restart like any interrupted turn,
  and `resume_turn` declines it if it is offered. The re-dispatched call opens a
  new worker chat.
- Approvals and questions an in-process inner session had open are withdrawn
  by the host with a notice naming them, because the inner session is gone;
  the call runs again live and asks again.
- Remote calls (`url` providers) are not reattached: the dispatcher stops the
  remote turn the restart left behind, with a log line, and dispatches the call
  again.

Fleet calls do continue: a cut-off fleet call whose `started` record names its
node session is **reattached**, waiting on that session again
(`wait_for_sessions`, then `read_session`), since the node session kept running
while the flow host restarted (the host stopping never stops a node session;
only an abort does). A log line says so (`agent() #N: reattached to node
session ...`). If the session is gone, the call is dispatched again, with a log
line. If asking about it fails, the old session is stopped before the call is
dispatched again, and when even the stop cannot be sent the call fails as
`unavailable` rather than start a second session beside one that may still run. A restored session lists no active clients (no connection outlives a
restart), so a continued run waits for the gateway to rejoin and offer its
tools, up to `FlowProvider(fleet_client_wait=...)` (default 10 s), before its
first fleet call fails.

Typed progress stays consistent: a continued run emits one `runStarted` (no
second one), a plain "Run <old> resumed after a host restart as run <new>"
line, phases it had announced are not announced again, and the re-dispatched
step supersedes the one that was cut off.

The provider's resume state is `{flow: 1, runs: [{chat, turnId, runId,
workflow, model, resumes, phasesStarted, phasesFinished}]}`, one entry per live
run (or per run a crash left resumable, until its chat starts another turn).

Limits:

- A crash loses the store's debounce window, so a run started within it may not
  be resumable.
- A second interruption before already-finished later calls are reached runs
  them again, because the new journal holds only the calls it has reached.

## Extensions (ahp-py only)

These are opt-in additions to the script format. Claude Code has none of them:
it ignores `opts.x` and leaves `ask`, `approve` and `canvas` undefined. The full
rules are in [docs/format.md](docs/format.md#extensions-ahp-py-only).

```js
// Pin one call to a configured provider.
const review = await agent('review the change', {x: {provider: 'glm'}})

// A named chat: the second call is a follow-up turn in the same session.
const plan = await agent('plan the change', {x: {chat: 'planner'}})
const next = await agent('now list the risks', {x: {chat: 'planner'}})

// Files and prior results as message attachments.
await agent('summarise', {x: {attach: [
  {kind: 'file', path: 'docs/notes.md'},
  {kind: 'result', value: plan, label: 'plan'},
]}})

// Run one call in a sub-folder of the run's working directory.
await agent('fix the tests', {x: {cwd: 'packages/api'}})

// Human gates: any connected client can answer.
const go = await ask('Proceed?', {choices: ['yes', 'no']})
const ok = await approve('Apply the plan', {diff: 'one-line summary of the diff'})
canvas('https://example.com/run', {title: 'Live view'})

// The human's reason and who answered, instead of a bool.
const {approved, reason, answeredBy} = await approve('Drop?', {x: {detail: true}})
```

`approve(..., {x: {detail: true}})` resolves to `{approved, reason, answeredBy}`: `reason` is
the explanation given with a denial (masked) and `answeredBy` the answering principal, each
`null` when unknown. A plain `approve()` still returns a bool, as under Claude Code, which
ignores `opts.x`.

`meta.x.schedule` offers a default schedule: `x: {schedule: '0 9 * * 1-5'}` or
`x: {schedule: {expression: '0 9 * * 1-5', timeZone: 'Europe/London'}}`
(`UTC` when absent). The host only offers it on the workflow's customization; a
client applies it to an automation.

To run the same script under Claude Code, paste `shim/flow-shim.js` right after
`export const meta = {...}`. It ships with the package: `flow shim` prints it
(`ahp_host_flow.shim.source()` returns it), so a pip install has it too. Under ahp-host-flow it keeps the real functions;
under Claude Code it falls back to `agent()` (set `FLOW_SHIM.askDefault` or
`approveDefault` to answer without one). Python workflows are ahp-py-only.

`flow run --config` has no client, so it cannot answer a gate: an `ask()` or
`approve()` fails with an error that says so. Attachments and named chats work
with in-process providers only. `opts.x.cwd` works with every provider kind, but fleet and remote
providers need an absolute folder (or a `file:` URI); a relative one is resolved in-process only
(docs/format.md says how).

## Custom verbs

A deployment can add its own script verbs that run host code with no model:
`tool()`, `render()`, `approve(person)`. This is an ahp-py extension (Claude Code
has no such globals; see [docs/format.md](docs/format.md#custom-verbs)). Declare
them in flow.toml as `name = "module:function"`:

```toml
[verbs]
tool = "my_pkg.verbs:tool"
render = "my_pkg.verbs:render"
approve = "my_pkg.verbs:approve"
```

A handler takes one `VerbCall` and returns JSON:

```python
from ahp_host_flow.engine import VerbCall


async def tool(call: VerbCall):
    return {"hits": await search(call.args[0]["q"])}
```

`VerbCall` has `name`, `args` (a tuple of JSON values), `ordinal` (verbs only),
`call_id` (stable across a resume), `phase`, `run_id` and `context`.
`context.log(text)`, `context.ask(question, options)` and `context.confirm(summary,
options)` reach the run's main chat; `context.principal` is the run's principal (the
turn's, from ahp-host's `principal_of`; `None` for a turn with none, such as a queued,
steered or host-started one, or without the hook; never the session creator's). `context.ask` returns `AskOutcome(value, answered_by)`, not the bare value (use
`.value`), and `context.confirm` returns `ConfirmOutcome(approved, answered_by)`. A script calls
`await tool('search', {q: 'x'})`.

An `approve(person)` handler can insist that the named person answers, and re-ask
when someone else does:

```python
from ahp_host_flow.engine import VerbCall


async def approve(call: VerbCall):
    person = call.args[0]
    while True:
        outcome = await call.context.confirm(f"{person}, please approve")
        if outcome.answered_by is None:
            raise RuntimeError("this host gives no principal, so answers cannot be attributed")
        if outcome.answered_by == person:
            return {"approved": outcome.approved}
```

- A handler that raises makes the call throw with its masked message, which the
  script can catch. Aborting the run cancels a running handler. Verbs take no
  agent slot and do not count against the `agent()` cap, the budget or the
  concurrency limit.
- Arguments, the result and errors are masked; the masked result is what the
  script gets. A result that is not JSON is an error.
- Calls are journaled and replayed like gates: a finished call replays on resume
  without calling the handler, and an open one is called again with the same
  `call_id`.
- Reserved names (`agent`, `parallel`, `pipeline`, `phase`, `log`, `args`,
  `budget`, `workflow`, `meta`) are refused at config load and in `RunOptions`,
  and so is any name that would shadow a global the sandboxes define or keep
  absent: JavaScript standard globals (`Object`, `JSON`, `Promise`, `console`,
  `eval`, ...), `require`, `process`, `fetch`, `setTimeout` and the like, and
  every Python builtin (`print`, `open`, `type`, ...).
  `ask`, `approve` and `canvas` may be replaced: the built-in is then not defined
  in that run (in Python, call it as a global, or through
  `ahp_host_flow.verbs.verb(name, ...)`).
- The library takes `run(dispatcher, verbs={'tool': handler})` and calls one with
  `await r.verb('tool', {...})`.
- On the main chat a call shows as a tool call titled by the verb name, with
  `verbStarted` and `verbFinished` progress and a `verbs` list in `run_view`.
  Verbs replayed from the journal are not published again.
- For a script that must also run under Claude Code, guard the call:
  `typeof tool === 'function' ? await tool('search', {q}) : <fallback>`.

## Resume keys

A resumed run replays an `agent()` call only when its key matches. By default the
key is Claude Code's rule (plan §3: prompt, opts, phase, label), so a run is
byte-identical to what earlier versions wrote. A flow.toml can add to it:

```toml
[resume]
key = "model"
```

`"model"` adds `{provider, model}` from routing and the `models` alias
expansion (config only; `null` for the provider's default), so changing a
model's alias re-runs the calls it touched. An import path
(`key = "my_pkg.keys:resume_key"`) is called as `fn(call, route)` and returns
JSON, which joins the key. The journal header records `keyMode`; a resume whose
mode differs from the old run's logs one line and replays no `agent()` call.
From the library, pass `key_extra=fn` to `run`.

## Fleet and remote providers

Two more kinds of provider run a call somewhere else than this process
(plan §2):

- **Fleet** (`via = "fleet"`): behind an `ahp-gateway`, each `agent()` call is
  an ordinary session on a node, listed in every surface connected to the
  gateway. The run session lives on a node that runs this package as an agent
  type (`[[agents]] type = "flow"; config = ".../flow.toml"` in an `ahp-node`
  config; registered as the `flow` entry point of `ahp_host.agents`, with runs
  kept in the node's state directory unless flow.toml sets `state_dir`). The
  gateway joins the run session and offers it fleet tools (`start_session`,
  `wait_for_sessions`, `read_session`, `stop_session`), which the engine calls
  through the main turn, with no model in between. The session is started as
  the principal who started the run, under the gateway's `child_config` and
  `max_running`; nodes need no fleet credentials. The worker chat is thin: a
  link to the node session, the status, the result and its usage. The node
  session holds the transcript and its approvals.
- **Remote** (`url`): without a gateway, the engine dials another host with
  `ahp-client` (`ClientDispatcher`) and runs the call as a session there. An
  approval or a question the remote agent raises appears on the call's worker
  chat, and the answer given there goes back to the remote session (no approval
  rules apply to it). An open request pauses `idle_timeout`, so an answer that
  takes longer still lands; a remote turn that goes quiet for `idle_timeout`
  otherwise is stopped and the call fails. By default a relayed request waits
  until the run is aborted; `approval_timeout` (seconds) bounds it: an
  unanswered one then fails the call with an error naming the provider and the
  setting, and the remote turn is stopped.

```toml
[providers.build-box]                 # fleet: a session on the gateway node `build-box`
via = "fleet"
node = "build-box"
agent = "claude"
approval_mode = "default"             # must be one the gateway's child_config allows
effort = "high"                       # dropped by the gateway if child_config sets effort
models = { sonnet = "claude-sonnet-5-5" }
reply_timeout = 900                   # seconds a fleet tool may take to answer (default 900)

[providers.mac]                       # remote: a host dialled directly
url = "wss://my-mac-mini.example.com"
token_file = "~/.config/ahp/mac.token"
agent = "claude"
idle_timeout = 600                    # seconds the remote turn may go quiet (default 600; all kinds)
# approval_timeout = 3600             # seconds a relayed approval may wait (default: no limit)

[routing]
default = "claude"
rules = [ { label = "Build*", provider = "build-box" }, { phase = "Review", provider = "mac" } ]
```

How a fleet call behaves:

- The model is `opts.model` through `models`, else the run's own model (the main
  chat's selection) when `models` names it or is empty.
- `approval_mode` (or the run's Approvals setting) and `effort` are sent as the
  session's `permissionMode` and `effort`. The gateway allows a mode only as
  strict as its `child_config` (with the default `{}` there, only `plan` and
  `default`); a refused one fails the call (`null`) with a log line naming the
  provider and what to change. An `effort` is dropped, not refused, when the
  gateway's `child_config` sets one.
- The node session names its caller. It is created with a delegation `origin`,
  `{kind: "ahp-py/delegation", session: <the run session>, chat: <the call's
  worker chat>, toolCallId: <the main chat's agent tool call, e.g. "agent-0">}`
  (sent as `start_session`'s `originChat` and `originToolCall`), so a client can
  nest the session under the call that started it. It is ahp-py's interim kind
  until upstream names one (plan §10 item 5).
- Skip and abort stop the node session. The host stopping does not: a run
  resumed after the restart reattaches to it (see "Runs survive a restart").
- Each fleet tool call is watched. If the gateway that offered the tools
  leaves (the host reports it gone), or no answer arrives within
  `reply_timeout` seconds (plus the 60 s a `wait_for_sessions` slice may take),
  the call is tried once more when the gateway offers the tools again, within
  `fleet_client_wait` seconds; otherwise, or if it is lost again, `agent()`
  returns `null` with a log line saying why. `start_session` is never retried,
  since the first may have started a session.
- A call fails clearly (`null` plus a log line) when no gateway offers the
  fleet tools to the run session.

Limits of a remote call, which a gateway's fleet dispatch does not have:

- The worker chat mirrors the remote turn's text only when the turn completes;
  the remote session holds the transcript, tool calls and approvals.
- An approval or question the remote agent raises cannot be answered from here
  (the dispatcher is a bare client). Someone must answer it in a client of
  that host; if the remote turn produces nothing for `idle_timeout` seconds,
  the call stops it and fails.
- It is not reattached after a restart (above).
- A run's skip or abort stops the remote turn, and so does the flow host
  stopping.

### Client tools in worker chats

In-process workers see the tools of the session's active clients (an editor
that joined the run session): a worker's agent can call them, and the client
runs them. `[workflows] client_tools` sets which: `true` (the default), `false`,
or a list of tool names. The gateway's fleet tools are never offered. A client
that joins, leaves or republishes its tools while a run is going reaches the
running workers too, and a named chat (`opts.x.chat`) is refreshed before each
call. Fleet workers use their node session's own clients.

Remote workers get the same tools advertised on their remote session, and the
remote agent's calls of them run on the worker chat through the run's own
clients (the answer goes back to the remote session).

## Running on sandboxed hosts

A provider can run its agent inside a container or VM that your own code
starts. Give the provider a `launch` function instead of a `url`; with
`per_call = true` every `agent()` call gets a fresh host, started for that
call's folder and torn down when the call ends.

```toml
[providers.sandbox]
launch = "my_launchers.container:launch"
agent = "opencode"
per_call = true

[routing]
default = "sandbox"
```

The launcher is an async function that starts the host and returns a
transport to it with a coroutine that tears it down:

```python
import asyncio
import socket
from collections.abc import Awaitable, Callable
from pathlib import Path
from urllib.parse import unquote, urlparse

from ahp_client.ws.transport import WebSocketClientTransport
from ahp_host_flow.engine.remote import LaunchRequest
from ahp_protocol.transport import Transport

TOKEN_FILE = Path("/srv/flow/ahp-token")
# The port the ACP host listens on inside the container (its default).
INNER_PORT = 4322


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def _wait_for_port(port: int, proc: asyncio.subprocess.Process) -> None:
    for _ in range(100):
        if proc.returncode is not None:
            raise RuntimeError("the container exited before it listened")
        try:
            _, writer = await asyncio.open_connection("127.0.0.1", port)
        except OSError:
            await asyncio.sleep(0.1)
        else:
            writer.close()
            return
    raise TimeoutError("the container did not start listening")


async def launch(request: LaunchRequest) -> tuple[Transport, Callable[[], Awaitable[None]]]:
    port = _free_port()
    folder = unquote(urlparse(request.folder).path) if request.folder else "/srv/flow/work"
    proc = await asyncio.create_subprocess_exec(
        "docker", "run", "--rm",
        # Published on the outside's loopback only: nothing off this machine reaches it.
        "-p", f"127.0.0.1:{port}:{INNER_PORT}",
        "-v", f"{folder}:/work",
        "-v", f"{TOKEN_FILE}:/run/secrets/ahp-token:ro",
        "registry.example.com/agents/ahp-host-acp:latest",
        "python", "-m", "ahp_host_acp",
        "--root", "/work",
        "--command", "opencode acp",
        # Inside the container the published port arrives on its own interface, not
        # loopback, so the host binds every interface; that needs the token file.
        "--bind", "0.0.0.0",
        "--port", str(INNER_PORT),
        "--token-file", "/run/secrets/ahp-token",
    )  # fmt: skip

    async def close() -> None:
        if proc.returncode is None:
            proc.terminate()
        await proc.wait()

    try:
        await _wait_for_port(port, proc)
        transport = await WebSocketClientTransport.connect(
            f"ws://127.0.0.1:{port}", token=TOKEN_FILE.read_text().strip()
        )
    except BaseException:
        await close()
        raise
    return transport, close
```

What crosses into the sandbox: the prompt, the call's folder (`opts.x.cwd`,
mounted as the agent's working folder), the run's client tools, and the
approvals and questions the agent raises, which are relayed to the worker chat
and answered by the run's clients as in the Fleet section (a question nobody
answers within `approval_timeout` is declined). Nothing else is shared: the
worktree option `isolation: 'worktree'` is ignored for remote providers, named
chats (`opts.x.chat`) and `attach` never route to them, and a call a restart cut
off is run again on a fresh host rather than reattached.

Set `env_allow` in the ACP host's config so only the variables the agent needs
reach the container's agent process; unset, it passes them all. With
`per_call = false` (the default) one host is launched for the run, on first use,
and closed when the run ends. That shared host is started before any call, so its
`LaunchRequest` has no folder and no client tools: only a `per_call` launch is given
the call's folder and the run's client tools.

Don't share the host's network with the container (`--network host`): the ACP host
would then listen on the machine's own interfaces. Publish its port on `127.0.0.1`
as above, and keep `--bind 0.0.0.0` and the token file inside the container
(`python -m ahp_host_acp` refuses a non-loopback bind without one).

## Operations

```toml
[workflows]
sources = ["client", "workspace", "user", "host"]

[providers.claude]
load = "ahp_host_claude:from_config"
interactive = true
```

### Limits

```toml
[limits]
budget = 200000        # output tokens for the whole run
input_budget = 2000000 # input tokens (cache reads count unless count_cache = false)
cost_budget = 5.0      # USD, from the cost the provider reports
wall_budget = 1800     # seconds: stop in-flight calls, refuse new ones
timeout = 3600         # seconds: kill the run
concurrency = 4        # most agent() calls in flight at once
heartbeat = 30         # seconds between main-chat heartbeats while a step runs
reserve = "mean"       # output tokens held per admitted call: a count, or "mean"
count_cache = true

[providers.claude]
load = "ahp_host_claude:from_config"
call_timeout = 600     # seconds one agent() call to this provider may run
```

`[limits]` applies to every run the provider starts, and to `flow run --config`. `budget`,
`input_budget` and `cost_budget` are ceilings on output tokens, input tokens and USD (0 is a
ceiling of nothing); a call that would pass one is refused, and one running when it is passed is
stopped. `reserve` is the output tokens an admitted call is assumed to need, so parallel calls
cannot overshoot `budget` together: a count, or `"mean"` for the mean output of the run's
finished calls (absent: the same mean, which is also the `RunOptions` default; `0` reserves nothing). `count_cache = false` leaves cache-read tokens out of
`input_budget`. `concurrency` caps the calls in flight. A continued run keeps the `budget` its
journal was started with.

`timeout` kills the run, like `flow run --timeout`. `wall_budget` is gentler: when it is spent,
calls in flight are stopped and recorded as `budget` errors, new calls are refused, and the
script carries on (it can still return a result).

`call_timeout` (a provider table key, all three kinds) and `opts.x.timeout` (per call, seconds,
`agent('...', {x: {timeout: 120}})`) limit one call. When it passes, the worker turn is stopped
and the call fails with kind `timeout` (transient): `agent()` throws, or returns null where the
script catches failures. `opts.x.timeout` wins over `call_timeout`; a value that is not a
positive number is an error before the call starts. A fleet call's node session is stopped when
the run stops the call (a timeout, a budget, `wall_budget` or quota stop) or is aborted; only the
host stopping leaves it running, for the resumed run to reattach to.

`idle_timeout` (a provider table key, all three kinds) is a watchdog on silence rather than on
total time: a call whose provider produces no event for that many seconds is stopped and fails
with kind `timeout` (transient). A remote provider defaults to 600 s; in-process and fleet
providers have no watchdog unless it is set. Any text, reasoning (thinking output is the
heartbeat), tool-call, notification or usage event resets it. An open approval or question pauses
it, so a slow human never trips it, and it restarts when the answer arrives (a confirmation the
approval rules answer themselves does not pause it). An in-process worker's client tool call
pauses it too, while the client runs the tool. A fleet call sees no deltas: it reads the
node session's row at least twice per `idle_timeout`, and a change of its status or of its
`activity` (ahp-gateway's fingerprint of the node session's chat, which moves with every delta,
reasoning or tool call) resets the clock; `waiting` pauses it, and on expiry the node session is
stopped. A gateway whose rows carry no `activity` shows status changes only, so a busy node
session there can still trip it.

`concurrency` (a provider table key, all three kinds; the unrelated `[limits] concurrency` caps
one run) limits the calls of one provider across every run of the flow provider: a number >= 1,
or, for an in-process provider, `"probe"`. A call holds the provider's slot inside its run's own
slot, so a run still counts against `[limits] concurrency`. With `"probe"` the first call runs
two minimal turns in parallel on two throwaway sessions and compares timings: if the second's
first event only arrives after the first turn is nearly over, the provider serves one turn at a
time and gets `concurrency 1` (logged `provider <name>: probe found a single slot; concurrency 1`),
otherwise no limit. Calls that arrive while the probe runs wait for it, and a probe whose
caller was cancelled runs again on the next call. A parallel provider that sends nothing until a
turn ends (no streaming, its first event is the reply) reads as single-slot, since its second
turn's first event comes about as late as the first turn's end; give such a provider a number
instead. A failed probe is logged and leaves no limit. A provider that answers
`rate_limited`, or `unavailable` and transient, halves a probed or unset limit (never below 1;
logged `provider <name>: busy (rate_limited); concurrency 4 -> 2`); a number written in
flow.toml is never changed ("explicit numbers win").

`retry_empty` (a provider table key, all three kinds; default 0) dispatches a call again when its
reply is empty: no text (reasoning alone counts as empty), or no value when `opts.schema` is set.
Each retry is logged as `agent() #n: empty reply from <provider>; retrying (1 of N)`. The script
sees one call: one ordinal, one resume key, one journal record, with the usage and cost of every
attempt summed (also when every attempt was empty, and a failed attempt before a fallback counts
too). Each extra attempt has its own worker chat and its own tool call in the main chat
(`agent-0`, `agent-0-a1`, ...). If every attempt is empty the call fails with kind `empty`
(transient); with `retry_empty = 0` an empty string is returned, as Claude Code does.

A running call is stopped live when a budget counter passes its limit; the overshoot
is bounded by how often its provider reports usage. Claude with
`usage_per_message = true` in its provider table (`[providers.claude]`) reports after
every model request, so a call can overshoot by one model request. Claude without it,
ACP, fleet and remote providers report when a turn ends, so the bound is one turn.
`wall_budget` reads a timer, not usage: it overshoots by nothing beyond the cancel
latency. Claude's per-request reports count a request's uncached input plus its cache
creation as input and its cache reads apart, as its turn report does, so the live and the
settled counters agree. ACP's cost (`acpUsage.cost`) is the ACP session's running total: each
call counts what it added, which matters for a named chat reusing one session. An ACP turn's
`inputTokens` is its latest model call (the context meter), so the budget charges the turn's
sums from `_meta.turnTokenTotals` instead, every model round counted. An ACP turn that
reports no `inputTokens` shows its context size instead, marked as a gauge
(`acpUsage.inputGauge`), and counts no input tokens: `input_budget` and the token quotas see
only ACP agents that report per-turn input.

### Quotas

```toml
[quotas]
tokens_per_day = 2000000   # output + input tokens one user may use per UTC day
tokens_per_run = 500000    # ... in one run
cost_per_day = 20.0        # USD, from the cost the provider reports
cost_per_run = 5.0
```

`[quotas]` caps what one user spends, by the principal of the turn that starts the run (never
the session creator's). Tokens are output plus input (cache reads count unless
`count_cache = false`). A call that would start at a reached limit is refused, `agent()` throws
`quota tokens_per_day reached: 2000000 of 2000000`, and the host logs
`<that text>; agent() #<n> refused`; a call running when a limit is passed is stopped like a
budget stop (kind `budget`). The text names no user and no other run. Each key is optional; the
day counters live in `<state_dir>/quotas.json` and survive a restart, and a user's day starts
again at 0 on the next UTC date. A run with no principal (a queued, steered or host-started turn)
is charged under one shared anonymous bucket. A call is charged once, when it ends.

### Fallback

```toml
[providers.claude]
load = "ahp_host_claude:from_config"
fallback = ["codex"]       # tried when claude is unavailable, rate limited, times out or is empty

[providers.codex]
load = "ahp_host_acp:from_config"
fallback = ["local"]

[providers.local]
load = "ahp_host_acp:from_config"

[routing]
default = "claude"

[[routing.rules]]
label = "review*"
provider = "claude"
fallback = ["codex", "local"]   # replaces claude's own list for these calls
```

When a call fails with kind `unavailable`, `rate_limited`, `timeout` or `empty`, or `failed` and
transient, the next provider in its chain runs it (a provider's `retry_empty` and `concurrency`
apply to each attempt) and the host logs `agent() #<n>: fell back from <a> to <b> (<kind>)`.
The chain is the matching rule's `fallback` if the rule sets one, else the routed provider's,
each followed by its own, duplicates dropped; a call pinned by `opts.x.provider` uses that
provider's. `refused` and `budget` errors, a cancellation and a schema failure never fall back.
A call with `opts.x.chat` or `opts.x.attach` skips fleet and remote fallbacks (logged), and the
last error is thrown when the chain runs out. Names must be configured providers and the
fallback graph must have no cycle (`fallback cycle: a -> b -> a`). Resume keys use the routed
provider, not the one that answered, so a resume replays a fallback's result; the journal's
start record and the run view name the provider that answered.

### Automations

A saved automation whose message is `/<workflow> <json args>` runs the workflow
unattended through ahp-host's automations, started manually or by cron. The run
is an ordinary flow run, with a journal and typed progress. If the template names
a client workflow, the host captures the client's plugin when the automation is
saved, so the unattended run never reads the client. A workflow can also offer
its own default cron (`meta.x.schedule`), which a client applies when it saves
the automation; see [Extensions](#extensions-ahp-py-only).

### Worktree changesets

An `agent()` call with `isolation: 'worktree'` that leaves changes publishes the
worktree's diff as a reviewable changeset on its worker chat, with Apply (to the
working tree) and Discard. The provider declares the operations itself
(`FlowProvider.changeset_operations`, ahp-host's `ProvidesOperations`), so
ahp-node, `python -m ahp_host_flow`, `flow run` and any embedder's `Host` get
them with no registration call. The worktree is kept until the
changeset is resolved. Kept worktrees are part of the session's resume state, so
after a host restart each one is republished as a new changeset (a new URI) on
its worker chat, with Apply and Discard working. One whose directory is gone
(removed with `git worktree remove` / `git worktree prune`, say) is dropped with
a log line, and no changeset link is left behind.

Commit makes the agent's work a commit instead of a patch. By default it commits the
worktree's changes on a new branch (`flow/<runId>-<ordinal>`, never checked out, so the
checkout is untouched) and removes the worktree; with `params.apply = true` it applies
them to the working tree and commits just those paths there, leaving other staged work
alone. `params` come from the invoking client (`Session.invoke_operation(params=)`):
`message` is the commit message (default: the call's label, else the prompt's first line
cut at 72 characters, else `flow: run <runId> step <ordinal>`, masked by the run's redactor)
and `apply` a bool. A rename or a delete is committed whole either way. Author and hooks are
git's own config; nothing is ever pushed. `[changesets]` sets `branch` (a template; `{runId}`
and `{ordinal}` are replaced) and `trailer` (add a `Flow-Principal:` line naming who pressed
Commit, when the policy knows them; CR, LF and other control characters are removed from
the name, so it cannot add lines of its own). Discard takes an optional
`params.reason`: it is logged (masked), shown in the changeset's description, posted as a
notification turn on the worker chat and, for a named chat (`x.chat`), put before that
chat's next prompt as `[discarded: <label>] <reason>`, so the agent learns why its work
was dropped.

```toml
[changesets]
branch = "flow/{runId}-{ordinal}"
trailer = false
```

### Which client turn a worker serves

A worker chat's turns are host-internal, so an embedding host whose `Policy` binds a
credential to each client turn never sees them. An inner provider whose
`context.publisher` is a `ResolvesOriginTurn` calls `publisher.origin_turn()` and gets
`OriginTurn(chat, turn_id)`, the client turn the run serves (the resuming turn after a
`chat/turnResume`), then asks its policy for that turn's credential. Embedders pass their
own policy with `build_host(config, policy=...)`.

### Steering

`interactive = true` on a provider makes its worker chats `full` instead of
read-only. A steering message on a running worker chat is forwarded to the inner
session when it takes steering mid-turn (`SteersTurns`), and otherwise rejected
with the reason. Worker chats of a provider that is not interactive stay
read-only.

Changing the session's Approvals config reaches the workers that are running: "approve
all for this run" applies to their next prompt at once (effort is not fanned out). So does
an Approvals overlay on a run's main chat, for that run's running workers, and a named chat
(`opts.x.chat`) gets the run's current Approvals before each of its calls.

### Approval rules

A flow-level `[policy]` and `[[approvals.rules]]` answer an in-process worker's tool
confirmations without bothering anyone. The policy is the same table the Claude and ACP hosts
take (`deny_commands`, `deny_paths`, `ask_paths`, ...); a rule matches on `tool` (a glob),
`read_only`, `provider`, `phase` and `persons` (the turn's principal), and every key present
must match. A `read_only` key matches a hint the agent supplied itself (an ACP call's `kind`,
`read_only_from_agent`) only when the rule also sets `trust_agent_hints = true`: an ACP agent
can label any call a read, so by default such a call is prompted. A call the provider files as
a read or a write but whose path cannot be read (an ACP read with no `locations`) is judged
by the policy as one with no path, which asks when there are path rules.

```toml
[policy]
deny_commands = ["git push*"]

[[approvals.rules]]
read_only = true
action = "allow"

[[approvals.rules]]
tool = "Bash"
persons = ["alice"]
action = "allow"
```

The order is fixed. The policy is checked first and always wins: a deny is final, an ask goes
to a person, and no rule can allow what the policy denies or asks. Next, a call the inner
provider's own policy asked about is prompted. Then the rules run in order and the first match
decides; a call no rule matches is prompted as before. A decision is logged on the main chat
as `agent() #<n>: <tool> approved by rule <i>` (or `denied by rule <i>`, or the policy's
reason). A rule approval is once and nothing broader.

The policy is also handed to each in-process provider whose factory takes a named `policy`
keyword (`ahp_host_claude:from_config` and `ahp_host_acp:from_config` do), folded into the
provider table's own `policy` as a check: a deny from either wins. That hook runs in every
approval mode, so setting the run's Approvals to auto (which stops a worker asking) does not
stop the flow's denies applying. A provider configured by a file path (`config = "x.toml"`)
is given the flow policy alone, and its `from_config` combines it with the file's own
`[policy]` (`ahp_host.provider.policy.combine`), again a deny from either winning. A factory
without the keyword is judged only when its worker asks.

Limits: a remote call's relayed approvals are always prompted (neither the policy nor the rules
see them), and neither fleet nor remote workers get the flow `[policy]` at all: they run on
another host, under that host's own policy, so an approve-all there is not checked here. An
inner policy ask is honoured only when the provider sets `policy_asked`, as `ahp-host-claude`
and `ahp-host-acp` do.

## An OpenAI-compatible endpoint

ahp-host-acp's reference agent (`ahp-acp-openai`) speaks to any `/v1/chat/completions` server,
such as Ollama, so a phase can run on a local or third-party model. The key, if the endpoint
needs one, goes in the host's own environment as `AHP_OPENAI_API_KEY`, never in flow.toml or
argv. A 429 from the endpoint is kind `rate_limited`, so `fallback` and `on_busy` apply to it
like any other provider.

```toml
[providers.claude]
load = "ahp_host_claude:from_config"

[providers.local]
load = "ahp_host_acp:from_config"

[providers.local.config]
command = ["ahp-acp-openai"]

[providers.local.config.env]
AHP_OPENAI_BASE_URL = "http://localhost:11434/v1"
AHP_OPENAI_MODEL = "my-model"

[routing]
default = "claude"

[[routing.rules]]
provider = "local"
phase = "Review"
```

## flow.toml

```toml
state_dir = "~/.local/state/ahp-host-flow"
node = "node"

[providers.claude]
load = "ahp_host_claude:from_config"   # a factory returning an AgentProvider
models = { sonnet = "claude-sonnet-5-5" }
approval_mode = "acceptEdits"
effort = "medium"

[providers.glm]
load = "ahp_host_acp:from_config"
config = "~/.config/ahp/opencode.toml"   # passed as config=Path(...)
models = { glm = "ollama/glm-5.3-flash:cloud" }

[routing]
default = "claude"
rules = [ { model = "glm", provider = "glm" }, { phase = "Review", provider = "glm" } ]

[workflows]
dirs = ["~/flows"]
sources = ["workspace", "user", "host"]   # the default; add "client" to opt in
client_tools = true                       # or false, or ["lookup"]: client tools workers get

[redact]
patterns = ['sekrit-[0-9]+']

[journal]
keep_source = false   # the default: the script and its args are journaled masked
```

Inner providers are loaded lazily, by import path, from `load = "module:attr"`:
each is imported and constructed the first time it is needed (a routed
`agent()` call, or listing models), not at startup. A provider that fails to
load fails only the calls routed to it (`agent()` returns `null` with a `log()`
line, and the host logs the cause); the others still run, and its models are
left out of the list.
The loader calls the factory by what `config` holds:

- no `config`: `factory()`;
- a table: its keys as keyword arguments, `factory(**table)`;
- a string: a path to the provider's own config file, `factory(config=Path(...))`.

`ahp_host_claude:from_config` and `ahp_host_acp:from_config` (the `[claude]` and
`[acp]` extras install them) build a provider from that file or table, so no
wrapper factory is needed. On top of that call the loader injects two keywords
when the factory has a parameter of that name (a bare `**kwargs` does not count)
and the table does not set it:

- `roots`: the host's roots (`--root`, or the `ahp-node` roots) plus
  `<state_dir>/runs`, so a worktree's working directory is inside them. With
  named roots the runs folder is named `flow-runs` (`flow-runs-2`, ... if taken);
  with none, it is the only root;
- `state_dir`: `<state_dir>/providers/<name>`, created if missing.

A provider has exactly one of `load` (in-process), `via = "fleet"` (with `node`
and `agent`) or `url` (with `agent`, and optional `token_file`); see "Fleet and
remote providers" for their keys. `models`, `approval_mode` and `effort` apply
to in-process and fleet providers, `models` to remote ones too. `interactive =
true` (in-process providers, default `false`) makes the provider's worker chats
take steering; see Operations. `preamble` (all three kinds, default `true`)
puts a line of context before every prompt the inner agent is sent: `true` for
the default text (the step's final message is its return value, so reply with
the result alone), `false` (`preamble = false`) for none, or a string of your own.
Only what the inner
agent receives changes: worker chats, the main chat's `agent` call and the
journal keys keep the bare prompt. In a named chat (`opts.x.chat`) the preamble
goes with the chat's first message only; its follow-ups are sent bare, since the
inner session already has it. `[workflows] sources` defaults to `["workspace",
"user", "host"]`; `"client"` is opt-in.

The result must be an `AgentProvider`. `approval_mode` and `effort` are passed to
each inner session as `permissionMode` and `effort`. Routing rules are tried in
order and the first match wins; a rule matches on `model` (an alias from some
provider's `models`), `phase` or `label` (a glob), and `default` takes the rest.
Relative paths resolve against the config file's directory. Unknown keys are
errors, at any level.

## Masking credentials

A secret printed or returned by an inner (worker) agent is masked before it is
published or stored. What is masked, exactly:

- the worker chat's text, reasoning, tool calls (input, output, results);
- structured calls: the `StructuredOutput` call's input and its accepted result;
- approval and input prompts (`ToolConfirmation`: input, messages, title,
  edits; `InputRequest`: message, url, questions), including the copy mirrored
  into the session's `inputNeeded`;
- step results on the main chat, `log()` lines and run errors;
- the `agent()` return value and the journal's stored values and errors.

A match becomes `[redacted]`. `[redact] patterns` is a list of regular
expressions; `builtin` (default `true`) also masks common credential shapes:
AWS access keys, GitHub and GitLab tokens, Slack tokens, PEM private key
blocks, bearer tokens and `key=value` secrets (where only the value is masked),
and any value stored under a secret-named key (`password`, `secret`, `token`,
`api_key`, `access_key`, `private_key`, `auth`, `credential`...) in a structured
value, at any depth. `builtin = false` turns the built-in rules off and leaves
your patterns.

The inner agent's own view is untouched: its prompts and the replies, tool
results and answers handed back to it are never altered. Streamed text is
released line by line, so a secret split across chunks of one line is still
caught, at the price of a worker chat's text arriving a line at a time.

Known gaps (not covered):

- Terminals, canvases and background work an inner session opens through its
  `ScopedPublisher` are published by the host as-is. They are covered by the
  after-publishing layer (plan section 4.9, milestone M9; see "Redacting after
  publishing"), which can remove them after the fact.
- A secret split across a tool-call boundary mid-stream (the first half in one
  chunk, the rest after a tool call started) is not joined; the same
  after-publishing layer (`flow redact`) is the answer.
- Client tool calls a worker makes are published with their input as the agent
  sent it (the client needs the real input, and the agent the real result).
- Changeset file contents (per-child worktree changesets) are published
  verbatim: masking them would corrupt the diff.
- The built-in list is best effort, not a guarantee. It does not match, for
  example, `key=value` values shorter than 8 characters, URL userinfo
  (`https://user:pass@host`) or `sk-` style API keys. Add `[redact] patterns`
  for the shapes your agents handle (plan section 4.9, "before publishing").

## Redacting after publishing

Masking is the before-publishing layer; this is the after layer, for a secret
that got through. `flow redact <runId> <ordinal> [--reason R] --config
flow.toml [--wait S]` queues a request in `<state_dir>/redact` that the running
`python -m ahp_host_flow` applies. Exit code 0 means done, 1 means refused or
failed, and 3 means nobody picked it up yet (the request stays queued and runs
when the host next starts). `--state-dir DIR` instead rewrites only the journal
of a CLI or library run, with no host involved.

What is redacted for the call with that ordinal:

- the worker chat's markdown, reasoning and tool-call parts;
- the step's `agent` tool call on the main chat: its result content, its
  structured content and the `stepFinished` result in `_meta['ahp-py/flow']`,
  so `flow_view` shows `[redacted]`;
- the journal record: its value becomes `[redacted]` and it is marked
  `redacted: {reason, redactedAt}`, so a resume replays `[redacted]`.

A run that is still running is refused: by name for runs the serving process
executes, and for any other process through `journal.lock` beside the journal,
which a run's writer holds while it is open (the operating system drops it if
the process dies). That covers `--state-dir` and the direct API too, and a lock
held elsewhere is never waited for or broken. A journal written before this
lock existed has none to find, so a live writer of that vintage is not detected.

The redaction is not atomic: the journal is rewritten first, then the worker
chat's parts, then the `agent` tool call. If it fails part way (the store is
unwritable, say) the error lists what was already redacted, `flow redact` prints
it, and running the same command again finishes the rest (parts that already
carry a marker are skipped and the journal rewrite is idempotent). The audit
entry for each published redaction names who asked: the request spool records
the operating-system user as `flow redact (local operator <user>)`, and
`redact_step` requires a stated `actor`.

**The agent's own context is not redacted.** The inner session's conversation
and provider state keep the original; only the shared view and the journal
change.

Also not redacted: the worker turn's message (the prompt), the run's final
returned value block, runs resumed from this run (they are listed as
descendants; redact them by their own run id), and wire logs written before the
redaction.

**The journal holds the script masked.** The journal header's script source and
`args`, and each `workflow` record's source, are stored masked by the same rules
(`[redact]`; the builtin shapes for a CLI run), beside the sha256 of the real
text (`sourceSha`, `argsSha`). A resume needs the real script and args, so it
gets them elsewhere and checks them against those hashes: the provider
re-resolves the run's workflow and re-parses the turn's own message, which the
session store holds (`ObservesChatState`); `flow run --resume` takes the script
path and `--args`; the library takes `RunOptions(resume_source=, resume_args=)`
(or `run_script`'s own `source` and `args`); a child workflow is re-resolved
when the resumed run reaches it. Anything that no longer hashes the same is
refused with a line saying what changed ("the workflow changed since the run
started", or the args), never run. `[journal] keep_source = true` in flow.toml
(`flow run --keep-source`) keeps them as they are, as before, and a journal
then resumes on its own.

What still holds a literal secret from a script: the main chat's user message
(the script text or args as they were sent, in the stored session and on the
wire to clients), and the inner agent's own context. `flow redact` touches
neither; pass secrets at run time from somewhere the script reads (an
environment variable through a tool, say), not as literals, and use
`Host.redact_message` on the main chat's turn for the shared view. A wire log the
flow host writes (`--wire-log`) is masked by the `[redact]` rules too.

A live client sees the change only if it opted in (ahp-client
`connect(..., redaction=True)`); any other client gets the redacted state on its
next snapshot. Redaction is host-initiated: there is no AHP client path.

Embedders that hold the `Host` call
`ahp_host_flow.redaction.redact_step(host, state_dir=..., run_id=...,
ordinal=..., reason=..., actor=...)`.

## Tests against real models

Three opt-in tests make real model calls: set `AHP_FLOW_LIVE_CONFIG` to a
`flow.toml` and run `tests/integration/test_flow_live.py` (a smoke test), or
`tests/integration/test_corpus.py`, whose `test_fanout_corpus_live` and
`test_triage_corpus_live` run two corpus scripts and whose
`test_review_routed_to_glm_live` routes a Review phase to a provider named
`glm` (skipped when the config has none). The corpus tests also run on fakes
without the variable. A sample live `flow.toml`, using both `from_config`
factories:

```toml
[providers.claude]
load = "ahp_host_claude:from_config"
models = { sonnet = "claude-sonnet-5-5" }
approval_mode = "acceptEdits"

[providers.glm]
load = "ahp_host_acp:from_config"
config = "~/.config/ahp/opencode.toml"
models = { glm = "ollama/glm-5.3-flash:cloud" }
approval_mode = "acceptEdits"

[routing]
default = "claude"
rules = [ { phase = "Review", provider = "glm" } ]
```

An inner provider reaches only working directories inside the flow host's roots
(plus the runs folder the loader adds). The live tests pass the test's folder as
the root; `python -m ahp_host_flow` needs `--root` covering the projects a
session will work in, or the inner provider refuses the session's folder with a
`PermissionError`.
