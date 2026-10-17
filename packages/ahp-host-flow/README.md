# ahp-host-flow

Run Claude Code workflow scripts (`agent()`, `parallel()`, `pipeline()`,
`phase()`, …) as AHP sessions. Each `agent()` call is a child chat driven by
any provider the host can load: Claude through `ahp-host-claude`, or any ACP
agent through `ahp-host-acp`.

Status: pre-alpha. M4a is in: the provider core. `flow.toml` configures
providers, routing and workflow sources; a run is a session, and each `agent()`
call is a worker chat on a routed provider. Typed progress, credential masking,
worktrees and live corpus runs (M4b) are next. See [docs/plan.md](docs/plan.md)
and [docs/format.md](docs/format.md).

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
flow run script.js --config flow.toml [--args JSON] [--root DIR]
```

With `--config`, the script runs through the configured providers on an
in-process host, and its return value is printed as JSON. The run's concurrency,
budget, timeout, workflow directories and state directory come from the config,
so `--concurrency`, `--budget`, `--timeout`, `--workflows`, `--state-dir`,
`--run-id` and `--resume` cannot be combined with `--config`. Node >= 20 is
needed (`node` in `flow.toml`, `--node`, `AHP_FLOW_NODE`, or `PATH`).

Nobody is connected to answer a confirmation or question an inner session asks
in a worker chat, so the run stops at the first one with an error naming the
call, rather than waiting forever. It is never auto-approved: set
`approval_mode` on the provider in `flow.toml` (for example `"acceptEdits"` or
`"auto"`) so the session does not ask.

Without `--config`, `flow run` is echo-only: every `agent()` call returns its
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

1. **workspace**: `.claude/workflows/*.js` in each of the session's working
   directories.
2. **user**: `~/.claude/workflows/*.js`.
3. **host**: the directories in `[workflows] dirs` of `flow.toml`.

`[workflows] sources` selects which of the three are used and their order. Each
source is published to clients as a customization container, with one `prompt`
child per workflow, so clients list them as slash commands. A shadowed name is
still listed, marked in its `_meta["ahp-py/flow"]`. The directories are re-read
at the start of each run.

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

An opt-in smoke test makes real model calls: set `AHP_FLOW_LIVE_CONFIG` to a
`flow.toml` and run `tests/integration/test_flow_live.py`.
