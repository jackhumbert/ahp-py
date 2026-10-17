# ahp-host-flow

Run Claude Code workflow scripts (`agent()`, `parallel()`, `pipeline()`,
`phase()`, …) as AHP sessions. Each `agent()` call is a child chat driven by
any provider the host can load: Claude through `ahp-host-claude`, or any ACP
agent through `ahp-host-acp`.

Status: pre-alpha. M4a is in: the provider core. M5 is in: Python workflows. `flow.toml` configures
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

## Running a script from the command line

```sh
flow run script.js|script.py --config flow.toml [--args JSON] [--root DIR]
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

`flow run` takes a `.js` or a `.py` script. Without `--config`, it is echo-only: every `agent()` call returns its
prompt, which is enough to try the engine without a model. It also takes
`[--concurrency N] [--budget TOKENS] [--timeout SECONDS] [--workflows DIR]
[--state-dir DIR] [--run-id ID] [--resume RUN_ID]`.

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

Three sources are searched, and the first match wins:

1. **workspace**: `.claude/workflows/*.js` (and `*.py`, see below) in each of the session's working
   directories.
2. **user**: `~/.claude/workflows/*.js` and `*.py`.
3. **host**: the directories in `[workflows] dirs` of `flow.toml` (`*.js` and `*.py`).

A `.py` workflow is arbitrary code on the host, so by default it loads only from
the host and user sources; see Python workflows.

`[workflows] sources` selects which of the three are used and their order. Each
source is published to clients as a customization container, with one `prompt`
child per workflow, so clients list them as slash commands. A shadowed name is
still listed, marked in its `_meta["ahp-py/flow"]`. The directories are re-read
at the start of each run.

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
  text without them. `awaitingInput` is derived from the confirmations and
  input requests the provider sees on the worker's turn (`TurnTee`), an
  approximation of the session's `inputNeeded`. The shapes belong to `ahp_protocol.ext.flow`, whose
  `run_view` folds a chat's state into phases, steps and their status;
  `ahp-client` gives it as `session.flow_view()` (and `chat.flow_view()`).
- `agent()` accepts `isolation: 'worktree'`. When the run's working directory is
  a git repository, the inner session runs in a new git worktree under
  `<state_dir>/runs/<runId>/worktrees/<ordinal>`. It is removed afterwards if it
  has no changes; otherwise it is kept and its path is logged. Outside a git
  repository the call logs why and returns `null`.
- With `state_dir` set, the journal writes a `started` record (ordinal, key,
  provider and worker chat) when a call is dispatched, before its result. A
  resume does not replay a call that never finished; it logs it.

## flow.toml

```toml
state_dir = "~/.local/state/ahp-host-flow"
node = "node"

[providers.claude]
load = "my_flow_providers:claude"   # a factory returning an AgentProvider
models = { sonnet = "claude-sonnet-5-5" }
approval_mode = "acceptEdits"
effort = "medium"

[providers.glm]
load = "my_flow_providers:glm"
config = "~/.config/ahp/opencode.toml"   # passed as config=Path(...)
models = { glm = "ollama/glm-5.3-flash:cloud" }

[routing]
default = "claude"
rules = [ { model = "glm", provider = "glm" }, { phase = "Review", provider = "glm" } ]

[workflows]
dirs = ["~/flows"]
sources = ["workspace", "user", "host"]

[redact]
patterns = ['sekrit-[0-9]+']
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
  after-publishing layer (plan section 4.9, milestone M9), which does not exist
  yet.
- A secret split across a tool-call boundary mid-stream (the first half in one
  chunk, the rest after a tool call started) is not joined; the same
  after-publishing layer (section 4.9, M9) is the answer.
- The built-in list is best effort, not a guarantee. It does not match, for
  example, `key=value` values shorter than 8 characters, URL userinfo
  (`https://user:pass@host`) or `sk-` style API keys. Add `[redact] patterns`
  for the shapes your agents handle (plan section 4.9, "before publishing").

## Tests against real models

Two opt-in tests make real model calls: set `AHP_FLOW_LIVE_CONFIG` to a
`flow.toml` and run `tests/integration/test_flow_live.py` (a smoke test) or
`tests/integration/test_corpus.py` (a small fan-out script, which also runs on
fakes without the variable).
