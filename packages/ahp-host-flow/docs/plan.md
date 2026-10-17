# Plan: ahp-host-flow

Run Claude Code workflow scripts (`agent()`, `parallel()`, `pipeline()`,
`phase()`, …) on any agent an AHP host can serve. That means Claude through
`ahp-host-claude`, or any ACP agent through `ahp-host-acp`. The run itself is
an AHP session, and each `agent()` call is a **child chat** of it, so any AHP
client can watch and steer the whole run. Extensions to the format are opt-in,
and a script written for Claude Code runs unchanged.

Status: M0 and M1 done (Node sandbox, `flow run`, worker-chat spike; engine semantics on a fake dispatcher). M2 is next. Milestones are in §8.

## 1. Where it sits

AHP has no workflow primitive and no structured-output turns. `requests.md`
calls agent-to-agent coordination something that "belongs to whatever
orchestrates the agents, not to the protocol". So ahp-host-flow is an orchestrator
built from AHP parts. It needs four things the protocol lacks: structured turns
(§4.6), typed progress (§4.7), durable runs and approvals across a restart
(§4.8), and redaction (§4.9). Each is built in ahp-py first, inside the
protocol's extension points (`_meta`, client tools, provider capabilities), so
stock clients still work, and each is written up as an upstream request (§10).

```
 AHP clients (VS Code, iOS, ahp-client, `flow` CLI)
        │  AHP
        ▼
 ahp-host ── provider: ahp-host-flow ───────────────────────────────┐
             one AgentSession per run                                │
             ├ main chat: the run (phases, log, result)              │
             ├ child chat per agent() call  (ChatOrigin kind "tool") │
             │    ├ in-process: an inner provider session drives it  │
             │    │  ClaudeProvider │ AcpProvider (opencode, …)      │
             │    └ fleet: links to a session the gateway started ───┼──▶ a session on any node
             │      on a node, via the gateway's fleet tools (§2)    │    (listed in every surface)
             └ remote: mirrored from a host it dials (ahp-client) ───┼──▶ another host
                                                                     │
 engine (ahp_host_flow.engine) ◀─ JSON-RPC ─▶ Node sandbox ───────────┘
```

## 2. Child chats (open question Q5)

AHP's child concept is the **worker chat**. A chat whose
`ChatOrigin = {kind: "tool", chat, toolCallId}` was spawned by a tool call in
another chat. The spawning call's result carries `ToolResultSubagentContent`
pointing back to it. Clients render it as a "spawned by tool" child, and
`ahp-host-claude` already shows Claude's subagents this way.

That concept is scoped to chats within **one session**. There is no
session-to-session parent: `SessionOrigin` has only the `automation` kind. This
gives three ways to dispatch, chosen per call by routing (§5):

- **In-process (primary).** `ahp-host-flow` hosts the inner providers itself.
  For each `agent()` call it opens a worker chat
  (`publisher.open_tool_chat`). It creates an inner `AgentSession` on the
  chosen provider and runs the turn with that worker chat's `TurnSink` as the
  inner session's sink. The child chat is then the real transcript, not a copy,
  and approvals, questions and file edits raised in it reach any client. No
  stray top-level sessions appear anywhere.
- **Fleet (behind a gateway).** Each `agent()` call becomes an ordinary
  session on a node, so every surface connected to the gateway (VS Code, a web
  client, the CLI) lists it and can open the same transcript. The mechanism is
  the gateway orchestrator's (`ahp-gateway` plan §10), reused rather than
  rebuilt:
  - the run session lives on a node that runs `ahp-host-flow` as an agent type
    (`ahp_host.agents` entry point `flow`), so it appears in the gateway's
    merged agent list like any other agent;
  - the gateway joins each `flow` session as an active client offering **fleet
    tools**, as it already does for orchestrator sessions. The tools are
    `start_session` (node, agent, folder, model, config, prompt, output
    schema, title), `wait_for_sessions`, `read_session` (final text, structured
    result, usage) and `stop_session`;
  - the engine calls them through the main turn's `run_client_tool`, not
    through a model, so the dispatch stays deterministic;
  - the gateway starts each session **as the principal who started the run**,
    over its own supervised per-(principal, node) links. The registry's
    admission and `child_config` apply, and `max_running` bounds the run
    alongside the engine's concurrency cap. Nodes need no fleet credentials;
  - the child chat in the run is thin: status, the result, usage and a link to
    the node session, which holds the full transcript and its approvals. A
    `SessionOrigin` kind for delegated sessions
    (`{kind: "delegation", session, chat, toolCallId}`, §10) would let the node
    session point back to the run.
- **Remote (direct).** Without a gateway, the engine dials another host with
  `ahp-client` and mirrors the remote session's events into the worker chat.
  The remote session shows in that host's catalog.

The main chat shows each `agent()` call as a tool call. Its label is the
`label` option and it is grouped by `phase`. The call's result is the
subagent-content link to its child chat. A running agent is also listed as
`BackgroundWork`.

**Layering:** a provider importing another provider would be a sideways import.
`ahp-host-flow` therefore loads inner providers from config by import path
(`"ahp_host_claude:ClaudeProvider"`, `"ahp_host_acp:AcpProvider"`) and never
imports them statically. They are optional extras of the distribution.

## 3. Compatibility target: Claude's format as it is

Pin the copied semantics in `packages/ahp-host-flow/docs/format.md`.

| Surface | Semantics to match |
|---|---|
| `export const meta` | pure literal: `name`, `description`, optional `whenToUse`, `phases[{title, detail?, model?}]`. Parsed without running anything. |
| `agent(prompt, opts)` | returns text, or the validated object when `schema` is set; `null` on skip or terminal error. Opts: `label`, `phase`, `schema`, `model`, `effort`, `isolation:'worktree'`, `agentType`. |
| `parallel(thunks)` | barrier; a thunk that throws resolves to `null`; never rejects |
| `pipeline(items, ...stages)` | no barrier; a stage gets `(prev, item, index)`; a throw drops that item to `null` |
| `phase`, `log`, `args` | progress grouping, narrator lines, input passed verbatim |
| `budget` | `total`, `spent()`, `remaining()`; a hard ceiling, so `agent()` throws once it is spent |
| `workflow(nameOrRef, args)` | inline child run sharing caps, budget and abort; one level of nesting |
| determinism | `Date.now()`, `Math.random()` and argless `new Date()` throw; no fs or Node APIs; plain JS |
| caps | min(16, CPUs−2) concurrent agents; 1000 per run; 4096 items per call |
| resume | the longest unchanged prefix of `agent()` calls (prompt + opts) is replayed from `journal.jsonl` |

**Probe results (Q3, 2026-10-17):** Claude Code's runtime silently accepts
unknown keys in both `meta` (top level and inside `phases[]`) and `agent()`
opts (`opts.x` and bare unknown keys). Globals it doesn't define are
`undefined`. So:
- extension opts under `opts.x` are safe in dual-target scripts and simply
  ignored there;
- new globals (`ask`, `approve`, `canvas`) need a `typeof` guard or the shim
  (§6).

**Acceptance corpus:** a set of real multi-phase workflows (a plan, build and
review pipeline; a fan-out/reduce; a test-triage; a commit-prep lint). They
live in a project's `.claude/workflows/` and run unmodified. Copies with
generic paths live under `packages/ahp-host-flow/conformance/`. There is also one
small script per row of the table above.

## 4. Components

### 4.1 Sandbox (Node subprocess), `ahp_host_flow.sandbox`
- `node:vm` context: only the hooks plus standard built-ins; `Date.now`,
  `Math.random` and `Date()` throw; no `require`, `process` or `import`.
- JSON-RPC over stdio. Only `agent()`, `log()`, `phase()` and `workflow()`
  cross to Python. `parallel` and `pipeline` are plain promises in JS, so the
  null-on-throw and barrier rules match Claude's by construction.
- `meta` is extracted with acorn (vendored, so there is no npm install at run
  time) and checked to be a literal.
- The `node` binary comes from config or `PATH`. Node ≥ 20.

### 4.2 Engine (`ahp_host_flow.engine`, Python, asyncio)
- **Scheduler:** concurrency semaphore, agent counter, abort signal, budget
  (summed from turn `usage`).
- **Journal:** `<state_dir>/runs/<runId>/journal.jsonl`, one record per
  `agent()` call: ordinal (assigned at issue time in the single-threaded JS),
  `sha256(prompt + canonical opts)`, result and usage. Resume serves the
  matching prefix and runs the rest live.
- **Registry:** resolves workflow names across several sources (§4.3) and
  snapshots each run's script.
- **`Dispatcher` interface:** `run_agent(call) -> AgentResult` streaming
  events. It has two implementations: `ClientDispatcher` (remote,
  through `ahp-client`) and `ProviderDispatcher` (in-process, in the
  provider layer).

### 4.3 Where workflows live

A workflow is one `.js` file. A source is anywhere such files are found. Every
source is surfaced to clients as an ordinary customization container, and
each workflow in it is a `prompt` child. So clients list and toggle workflows
with the existing customization actions (global, workspace or session
enablement), and each one shows as a slash command (`/fanout`) with
`whenToUse` as its description. AHP has no workflow customization type; the
prompt child's `_meta.ahpFlow = {kind: "workflow", phases}` marks it. If
clients want to render workflows differently, that becomes a `requests.md`
item.

| Source | Where the files are | Who defines it | Surfaced as |
|---|---|---|---|
| **workspace** | `.claude/workflows/*.js` in each of the session's working directories: the same files Claude Code uses in that project | the project, in its repo | a `directory` container per working directory |
| **user** | `~/.claude/workflows/*.js` of the account the host runs as: Claude Code's user-level folder | whoever runs the host | a `directory` container |
| **host** | `[workflows] dirs = [...]` in `flow.toml`, plus any bundled with the package (`ahp_host_flow/workflows/`: generic `fanout`, `review`) | the host operator, shared by every session and client | one `plugin` container, "ahp-host-flow" |
| **client** | `workflows/*.js` inside an Open Plugins plugin a client publishes (`activeClients[].customizations`), read from the client with server→client `resourceList`/`resourceRead` | the person at a client, from their own machine, even when the host is elsewhere | the client's plugin container, with workflow children expanded by the host |

- **Name resolution, first match wins:** client → workspace → user → host. A
  name can be qualified (`/host:fanout`, `/workspace:fanout`) to choose
  explicitly. A shadowed name is still listed, marked `_meta.ahpFlow.shadowedBy`.
  `workflow("name")` inside a script resolves the same way, from the calling
  run's session. `workflow({scriptPath})` is a path in the session's working
  directories, or a URI.
- **Claude Code parity:** the workspace and user sources are exactly the
  folders Claude Code reads. With a host on the same machine, the same scripts
  show up in both, unmodified.
- **Snapshots:** a run copies its resolved script, and every nested one, into
  the journal at start. So resume, restart and audit never depend on a source
  that changed or a client that disconnected. Resume refuses to start if the
  run's own snapshot is missing.
- **Automations:** a saved automation that names a client workflow reuses
  `plugin_copies` to capture it at save time, as for any client plugin.
  Workspace, user and host sources are read at run time.
- **Policy:** `flow.toml` can disable sources per host
  (`[workflows] sources = ["workspace", "user", "host"]`, so client workflows
  are opt-in on a shared host). Whatever the source, a workflow's agents get
  the routing profile's permissions and never more.
- **Live updates:** the host watches the workspace, user and host dirs. On a
  change it re-reads `meta` (parsed, never executed) and republishes with
  `customizations_changed`.

### 4.4 What `agent()` maps to

| opt | AHP mapping |
|---|---|
| `model` | `Message.model = {id}`; aliases (`haiku`, `sonnet`, `opus`, `glm`) resolve through the routing profile (§5) |
| `effort` | the inner session's `config.effort` where the provider has it; otherwise ignored, with one `log()`. Since 1.1.0 it is also `chatMutable` on the child chat (§4.5), so a client can change one agent's effort mid-run |
| `isolation:'worktree'` | `git worktree add` under the run dir, used as the inner session's working directory; removed if unchanged, otherwise kept and its changeset published on the child chat |
| `agentType` | an agent from the inner session's customizations; unknown means an error, as in Claude |
| `schema` | see below |
| approvals | per-profile policy, as the inner session's approval mode: auto-approve what the profile allows, and raise the rest in the child chat for a human. Since 1.1.0 the mode is `chatMutable` on the child chat (§4.5), so loosening one agent from a phone never loosens the others |
| skip / abort | cancelling the child chat's turn makes that `agent()` return `null`; cancelling the main turn aborts the run |
| errors | a resumable error uses `turnResume` once, then the call returns `null` with a `log()` line |

The prompt gets a short preamble equivalent to Claude Code's subagent
framing: "your final message is the return value; return raw data". This
matters most for non-Claude agents.

**Structured output.** `opts.schema` becomes the turn's output schema, a
structured turn (§4.6), whichever way the call is dispatched. The engine
validates the result again before `agent()` returns it.

### 4.5 Per-chat config, steering and titles (AHP 1.1.0)

ahp-py moved to AHP 1.1.0 while M1 was in progress. Nothing breaks, but four
1.1.0 features change the design:

- **Per-chat config.** A config property marked `chatMutable` can be set on
  one chat (`chat/configChanged`), and the provider follows it through
  `ReconfiguresChats.chat_config_changed`. `ahp-host-claude` already marks
  `permissionMode` and `effort` this way. `ahp-host-flow` marks the same two
  properties `chatMutable` on the run session. A child chat's overlay is
  forwarded to that child's inner session as its own config, by
  `ConfiguresSessions`, or by `chat_config_changed` on its chat. So a client
  can tighten or loosen one agent's approvals, or raise its effort, mid-run,
  without touching the others or the run. An inner session that reports its
  own change (`config_changed`) lands on its child chat (done in M1).
- **One inner session per call, still.** Per-chat config would let one inner
  session host every call as a chat (`HostsChats` plus per-chat config), with
  one agent process per run instead of one per call. But a chat can only
  narrow its session's working directories, and `isolation: 'worktree'` needs
  a new one. So M3 keeps a session per call, and sharing is a later
  optimisation for calls with the same working directory.
- **Steering.** 1.1.0 unifies steering and queued messages in one list with a
  per-entry kind. A child chat is read-only by default. If its profile makes
  it interactive, a steering message there is forwarded to the inner session
  (`SteersTurns`), which lets a person nudge one agent mid-turn (M5). The run's
  main chat queues nothing while a run is active.
- **Titles and vendors.** `createChat`/`open_tool_chat` take a title, so a
  child chat is titled by `label` (or the prompt's first line).
  `ModelInfo.vendor` lets the run session advertise every inner provider's
  models with their vendor, so a client can pick a run's default model across
  Claude and GLM. On the remote side, `ahp-client`'s `create_chat(title=)`
  serves M6.

### 4.6 Structured turns

A turn whose result is checked against a JSON Schema, rather than prose to
parse. It is useful to any client, not only to workflows, so it lives in the
host and the providers, not in ahp-host-flow.

- **Asking.** A message carries its schema in
  `Message._meta["ahp-py/outputSchema"]`, an extension point stock hosts
  ignore. `ahp-client` gets `prompt(..., output_schema=...)`, and the gateway
  passes `_meta` through untouched.
- **Producing it.** A provider capability, `ReturnsStructuredOutput`, takes
  the schema with the message:
  - `ahp-host-claude` gives Claude a `StructuredOutput` tool whose input schema
    is the schema, through the in-process MCP server it already uses for
    client tools (`client_tools.py`);
  - `ahp-host-acp` gains **client-tool support** in general. A session's client
    tools reach the ACP agent as an MCP server that `ahp-host-acp` serves: a
    small stdio relay the agent launches (every ACP agent takes stdio MCP), or
    a loopback HTTP endpoint when the agent advertises HTTP MCP. Each call goes
    through `TurnSink.run_client_tool`, as Claude's do, and, as in
    `ahp-host-claude`, a client tool passes the approval gate because the
    client offered it. Structured output is then one host-internal tool on the
    same path. Any other client's tools reach ACP agents too.
- **Checking it.** The host validates the tool's arguments with `jsonschema`.
  A mismatch goes back to the agent as a tool error, so it retries, a bounded
  number of times. A provider without the capability gets the schema in the
  prompt, and the host parses and validates the final message's last JSON
  block.
- **The result.** It is the turn's `StructuredOutput` tool call, whose result
  carries `structuredContent`, so it sits in the transcript every client
  already renders. `ahp-client`'s `TurnCompleted` exposes it as
  `.structured`. A turn that never produces a valid result ends as an error
  with a structured-output code.
- **Upstream:** `Message.outputSchema` and a structured result on the turn
  (§10).

### 4.7 Typed progress

Every surface should render a run the same way, so progress needs a shape,
not prose. The spec already types two parts of it: waiting for input
(`SessionState.inputNeeded`, which names the chat and request) and running
work (`BackgroundWork`). It has nothing for phases or steps. Until it does:

- **One descriptor, in `_meta`.** `ahp-host-flow` puts
  `_meta["ahp-py/flow"]` on the parts it already publishes on the main chat. A
  system notification carries `runStarted`, `phaseStarted`, `phaseFinished` or
  `runFinished`. Each step's tool call carries `stepStarted` (ordinal, label,
  phase, child chat or node session, model) and, on its result,
  `stepFinished` (status `ok` | `null` | `error` | `skipped`, structured
  result, usage). While a step's chat has an entry in `inputNeeded`, the step
  also carries `awaitingInput`. Every part still reads coherently as text, as
  the spec requires, so stock clients lose nothing.
- **One fold, shared.** The descriptor's JSON Schema and a pure fold from the
  transcript to a `RunView` (phases → steps → status, result, usage) live in
  `ahp_protocol.ext.flow`, which has no dependencies. The schema is also
  exported as a JSON file for non-Python surfaces. `ahp-client` exposes
  `session.flow_view()`. Every surface folds the same parts the same way.
- **Upstream:** typed run, phase and step state on a chat, with actions for
  each transition (§10).

### 4.8 Durable runs and approvals

Today a session restored after a host restart brings back a turn that was
running only as transcript, so a run would silently stop. A run must instead
continue where it stopped, and approvals that were open must still be
answerable.

- **The journal is the run's memory.** It holds the script snapshots, a record
  when each call **starts** (with its dispatch handle: the node session's URI
  for fleet calls) and when it finishes, and every gate (`ask`, `approve`)
  with its state.
- **Restart.** `ahp-host-flow` is a `ResumableAgentProvider`. On restore, a run
  whose main turn was active is relaunched from its journal, under the same
  turn, not left as transcript:
  - finished calls replay from the cache;
  - started fleet calls are **reattached**, since the node session kept
    running, so the run waits on it again;
  - in-process calls that were cut off run again live, with a `log()` line;
  - open gates are raised again with their original ids.

  This needs an `ahp-host` hook that offers a resumable provider the
  interrupted turn instead of closing it: `ResumesInterruptedTurns`.
- **Open approvals in general (`ahp-host`).** `StoredSession` keeps the
  session's `inputNeeded` entries. On restore, a provider that can still answer
  one (it implements the hook above) re-binds it. Otherwise it is withdrawn
  with a system notification saying why, rather than vanishing. That way no
  surface has to persist approvals on its own. An approval raised in a fleet
  call's node session lives on that node and survives the flow host's restart
  regardless.
- **Upstream:** whether a host must restore interrupted turns and open input
  requests, and how a client learns which ones it did (§10).

### 4.9 Redaction

Once a run is shared, a transcript can carry a secret that a tool printed. AHP
cannot remove or replace part of a channel's history. So there are two layers:

- **Before publishing (no protocol change).** A profile's `redact` rules
  (regexes, plus known credential shapes) mask matches in everything that
  passes through ahp-host-flow on its way out: the child chats' text and tool
  output (in the sink bridge), step results, and the journal. The original
  never leaves the process. This is on by default for credential shapes.
- **After publishing (protocol change).** An `ahp-host` API,
  `Host.redact(channel, turn_id, part_id, reason)`, replaces a part with a
  redaction marker in the state and in the stored session, and records the
  fact (never the content) in the audit log. A client that advertises an
  experimental capability gets the change live. Other clients get it on their
  next snapshot, because unknown actions must not reach a stock client. The
  agent's own context is not redacted, only the shared view; the docs must say
  so plainly. `flow redact <runId> <ordinal>` applies it to a run, including
  its journal.
- **Upstream:** a `chat/partRedacted` action (and a message variant) with
  reducer semantics, so every client applies it the same way (§10). Until
  that is agreed, the live layer stays behind its capability.

## 5. Routing and the first ACP target

The first ACP target is **opencode on Ollama Cloud's GLM 5.3 Flash**
(`ollama/glm-5.3-flash:cloud`), which `ahp-host-acp`'s README already
documents. Config is per user, with an optional per-project overlay under
`"flow"` in `<cwd>/.claude/workflow.json`:

```toml
# ~/.config/ahp/flow.toml
state_dir = "~/.local/state/ahp-host-flow"
node = "node"                                  # or an absolute path

[providers.claude]                             # in-process
load = "ahp_host_claude:ClaudeProvider"
models = { haiku = "claude-haiku-4-5-20251001", sonnet = "claude-sonnet-5-5", opus = "claude-opus-5-5" }

[providers.glm]                                # in-process
load = "ahp_host_acp:AcpProvider"
config = "~/.config/ahp/opencode.toml"         # command = ["opencode", "acp"], OPENCODE_CONFIG, …
models = { glm = "ollama/glm-5.3-flash:cloud" }

[providers.build-box]                          # fleet: a session on a node, via the gateway's fleet tools
via = "fleet"; node = "build-box"; agent = "claude"

[providers.remote-mac]                         # remote: a host dialled directly (ClientDispatcher)
url = "wss://my-mac-mini.example.com"; agent = "claude"

[redact]
patterns = ['(?i)api[_-]?key\s*[:=]\s*\S+']   # plus built-in credential shapes

[routing]
default = "claude"
rules = [ { model = "glm", provider = "glm" },
          { phase = "Review", provider = "glm" } ]   # first match wins
```

Routing lives in config, so a script needs no changes to send a phase to GLM.
A script can also pin one call with `opts.x.provider`.

## 6. Extensions

All of these are opt-in. Under Claude Code, `opts.x` is ignored (probed), and
the globals below come from `flow-shim.js` (a prelude that defines them as
no-ops or as `agent()` fallbacks), or the script guards them with `typeof`.

| Extension | What | Built on |
|---|---|---|
| `opts.x.provider` | pin a call to a provider or remote node | routing |
| `ask(question, {choices?, schema?})` | a human gate: the run pauses until any client answers | `request_input` on the main chat |
| `approve(summary, {diff?})` | yes/no before a destructive step | `confirm_tool_call` |
| `opts.x.chat` | reuse a named child chat across calls (a multi-turn agent) | same inner session |
| `opts.x.attach` | pass prior results or files as message attachments | AHP attachments |
| `canvas(url)` | a live view on the run | `canvas_set` (experimental) |
| `meta.x.schedule` | a default cron when saved as an automation | automations |

## 7. Package

One new distribution: `ahp-host-flow`. It depends on `ahp-host` and
`ahp-client` (both below it), with extras `[claude]` and `[acp]` that pull in
the inner providers it loads by import path.

| Subpackage | Contains | May import |
|---|---|---|
| `ahp_host_flow.engine` | scheduler, journal, budget, registry, `Dispatcher` interface, `ClientDispatcher` (remote, via `ahp-client`), `flow validate` | `ahp_protocol`, `ahp_client`, never `ahp_host` |
| `ahp_host_flow.sandbox` | Node runner (`runner.mjs` + vendored acorn) as package data, and the Python side of its JSON-RPC | `engine` |
| `ahp_host_flow` (top) | the provider (run = session, child chats), `ProviderDispatcher`, `python -m ahp_host_flow`, `flow run` (headless, in-process host) | everything above, plus `ahp_host` |

Why not the existing packages:
- `ahp-client` is a lean library, and Node plus a journal don't belong in it.
- Each provider is its own `ahp-host-<name>` distribution, so the provider
  doesn't go in `ahp-host`.
- The gateway's orchestrator is fleet-only, and workflows should run on one
  node.

Why one package, not two: a separate engine distribution only pays off once
something besides this provider uses the engine. Fleet dispatch does not need
one: the gateway only offers fleet tools to the run session, as it does for
the orchestrator, and never runs the engine. If the gateway ever hosts runs
itself, the engine moves into its own `ahp-flow` distribution. Until then, an
`import-linter` contract (engine must not import `ahp_host`) keeps the split a
move rather than a rewrite.

The package follows the house layout: `AGENTS.md`, `CHANGELOG.md`,
`LICENSE`, `README.md` and `docs/`; mypy strict; `lint-imports` contracts;
`tests/{unit,integration,conformance,docs}` on `memory_pair()`. Inner
providers in tests are `EchoProvider` and a fake ACP agent, never live models.
A live GLM smoke test is opt-in, behind a marker.

## 8. Milestones

| # | Milestone | Done when |
|---|---|---|
| M0 | Package skeleton. Sandbox + RPC + echo `agent()`. Meta parse; banned APIs throw. **Spike:** an inner provider session's sink bridged into an `open_tool_chat` worker chat | `flow run` of a 3-line script prints the result; the spike shows EchoProvider's turn in a child chat |
| M1 | Engine semantics on a fake dispatcher: null rules, caps, phase/log, budget, `workflow()` | one conformance script per row of §3 passes |
| M2 | Journal + resume | kill mid-run, resume: identical results, and live calls start only at the first edited call |
| M3 | `ahp-host-flow` + in-process Claude: workspace/user/host workflow sources as customizations, child chats titled by `label`, model/effort, per-chat approval mode and effort (`ReconfiguresChats`, §4.5), StructuredOutput client tool, worktrees, skip/abort, approval relay, models advertised with `vendor` | fan-out and triage corpus scripts pass against Claude; a run is watched and controlled from VS Code or iOS |
| M4 | In-process ACP (opencode/GLM): MCP StructuredOutput, text fallback, routing | a script routes its Review phase to GLM and gets a schema-valid result |
| M5 | Full corpus unmodified; client-published workflows (+ automation capture); restart resume (`ResumableAgentProvider`); per-child changesets; steering a child chat (§4.5); automations | the plan/build/review corpus script lands a change; a cron automation runs a workflow |
| M6 | `ClientDispatcher` remote + gateway (one remote chat per call, `create_chat(title=)`); the `SessionOrigin` request upstream | one call runs on a gateway node and is mirrored into its child chat |
| M7 | Extensions (§6) + shim | `ask()` round-trips to a phone; a dual-target script runs in both runtimes |

**M0 spike result.** `tests/integration/test_spike_worker_chat.py` showed:

1. An inner `EchoProvider` session runs its turn through `ProviderChat.run_turn` on a worker chat from `publisher.open_tool_chat`. The worker turn's message has origin `agent`, the chat's origin is `{kind: "tool", chat, toolCallId}`, and the spawning tool call's result carries the `subagent` link (written by the test's own outer session, so this shows the wire shape, not provider behaviour). So the in-process design of §2 holds and the mirror design is not needed.
2. `run_turn` returns when the turn starts, so the dispatcher waits on its own completion signal. A text tee on the inner sink yields `agent()`'s return text.
3. The inner context's publisher is scoped by a wrapper. By design, chat-scoped calls (background work, changesets, canvases, terminals, nested tool chats) default to the worker chat. Only `background_work_set` and one dropped session-wide call are asserted, and EchoProvider never calls its publisher, so M3 must test each scoped method with a provider that uses it. Session-wide calls (title, customizations, MCP state, activity, progress) are dropped, so an inner session cannot rename or reconfigure the run. Since AHP 1.1.0 gave chats their own config overlay, `config_changed` is routed to the worker chat (`chat=`), not dropped.
4. Cancelling a worker cancels only the `run` callback (ahp-host's own tool-chat tests), so in M3 the dispatcher must cancel the inner session itself on `CancelledError`.
5. `ScopedPublisher` and the tee live in the test for now, and M3 moves them into the provider.

M0 sandbox decisions: scripts run in strict mode, matching acorn's module parse; `agent()` ordinals are 0-based and taken at issue time; in M0 a dispatcher error makes `agent()` return null, with a `log()` line, until M1 pins the full null rules; parallel, pipeline, budget and workflow arrive in M1.

M0 sandbox hardening, from review:
- The context is created from a null-prototype object. From `{}`, `this.constructor.constructor` was the host `Function`, which reached `process` and the real `Date.now`/`Math.random`.
- The prelude runs inside a block, so its `RealDate` is not in the script's global lexical scope.
- Python writes ASCII-only JSON, because Node's readline splits on raw U+2028/U+2029.
- Tests cover each of these routes. `node:vm` is still not a security boundary: the sandbox enforces determinism and keeps honest scripts off Node APIs. It is not meant to contain a hostile script.

**M1 result.** `parallel` and `pipeline` live in the sandbox prelude. The `Scheduler` (`engine/scheduler.py`) owns the concurrency semaphore (default min(16, CPUs−2), at least 1), the 1000-call cap, the output-token budget (mirrored into the script on each reply) and abort. `workflow()` runs the child in a `node:vm` context of its own, on the same `Scheduler`, with the agent counter, spent tokens and phase held in the host and shared as primitives. Its own `workflow` always throws, and nothing but JSON crosses between the contexts. (The first M1 build shared the parent's hooks into the child, and a value returned by `parallel` led back to the parent's `workflow`.) `Registry` is a plain ordered list of directories until M3's multi-source registry. `RunOptions.timeout` kills Node, argless `Intl` date formatting throws, and the stdout reader fails all pending requests on any exception. A host-side exception while handling an `agent()` or `workflow()` request is answered as an error rather than left pending. `conformance/` holds one script per covered §3 row, and `docs/format.md` pins the rules.

Open for M2:
- node-marked tests still skip silently when no Node is found, so CI must provide Node;
- `Temporal` (absent in Node 24's vm context today) must be banned if a future Node exposes `Temporal.Now`;
- the unchecked choices listed in `docs/format.md` need a probe against Claude Code.

## 9. Risks

- **Format drift:** Claude's workflow format is internal. Mitigation:
  `format.md` and the conformance suite, rerun on Claude Code updates.
- **Sink bridging:** the in-process design assumes an inner session can run
  against a worker chat's `TurnSink` and that the inner context's publisher
  can be scoped to that chat. The M0 spike proves this or forces the mirror
  design everywhere. Resolved by the M0 spike (§8).
- **Weak structured output:** bounded retries, then `null` plus a `log()`.
- **Approval floods:** with N parallel children there can be N prompts.
  Mitigation: per-profile policy, and never auto-approve beyond it.
- **Ordinal determinism:** ordinals are taken at issue time in JS, not at
  completion.
- **Ahead of the spec:** `_meta` keys, an experimental capability and host
  hooks can diverge from what upstream later adopts. Mitigation: names are
  namespaced (`ahp-py/...`), each has a §10 request, and moving to the
  upstream shape is a planned step, not a surprise.
- **Fleet identity:** fleet calls run as the principal who started the run.
  The gateway must record that principal with the run session and never let
  one principal's run drive another's sessions. This is the orchestrator's
  rule, extended to flow sessions.

## 10. Upstream requests

Written into the owning package's `docs/requests.md` as each lands, and raised
upstream:

1. **Structured turns:** `Message.outputSchema`, and a structured result on a
   completed turn (§4.6).
2. **Typed progress:** run, phase and step state on a chat, with actions for
   each transition, so every client renders a run the same way (§4.7).
3. **Restart semantics:** whether a host restores a turn that was running and
   the open input requests, and how a client learns what was restored (§4.8).
4. **Redaction:** `chat/partRedacted` (and a message variant), with reducer
   semantics and the rule that only the shared view is redacted (§4.9).
5. **Delegated sessions:** a `SessionOrigin` kind,
   `{kind: "delegation", session, chat, toolCallId}`, so a session started on
   another host's behalf can point back to the chat that started it (§2).
