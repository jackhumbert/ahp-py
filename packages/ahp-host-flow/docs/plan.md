# Plan: ahp-host-flow

Run Claude Code workflow scripts (`agent()`, `parallel()`, `pipeline()`,
`phase()`, …) on any agent an AHP host can serve. That means Claude through
`ahp-host-claude`, or any ACP agent through `ahp-host-acp`. The run itself is
an AHP session, and each `agent()` call is a **child chat** of it, so any AHP
client can watch and steer the whole run. Extensions to the format are opt-in,
and a script written for Claude Code runs unchanged.

Status: M0-M17 done (M4b complete). M18 is planned (§8, **Post-M12 plan**); M18 is next.

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
gives three ways to dispatch, chosen per call by routing (§5). As built (M11), a
fleet call's node session carries an interim `SessionOrigin`,
`{kind: "ahp-py/delegation", session, chat, toolCallId}`, naming the run session,
the call's worker chat and its `agent` tool call (`ahp_protocol.ext.delegation`;
`ahp-host` `docs/requests.md` entry 12), so the child side of the link exists.

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
  `ahp-client` and shows the call in a thin worker chat: a link to the remote
  session and its text once the turn completes. The remote session shows in
  that host's catalog. An approval or question raised there cannot be answered
  from the run (it is a bare client); a remote turn idle for `idle_timeout`
  seconds is stopped and the call fails, so it never hangs the run.

The main chat shows each `agent()` call as a tool call. Its label is the
`label` option and it is grouped by `phase`. The call's result is the
subagent-content link to its child chat. A running agent is also listed as
`BackgroundWork`.

**Layering:** a provider importing another provider would be a sideways import.
`ahp-host-flow` therefore loads inner providers from config by import path
(`"ahp_host_claude:from_config"`, `"ahp_host_acp:from_config"`, the factories
that build them) and never imports them statically. They are optional extras of the distribution.

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
  `agent()` call: ordinal (assigned by the host, `Scheduler.issue`, when the request line arrives),
  key (sha256 of the canonical JSON of prompt, opts, phase and label), status
  (`ok`, `null`, `error`), result and usage. The header holds the script
  snapshot plus args and semantic options; each child workflow snapshot is a
  record of its own. Resume serves the matching prefix and runs the rest live.
- **Registry:** resolves workflow names across several sources (§4.3), in
  JS or Python (§4.10), and snapshots each run's script.
- **`Dispatcher` interface:** `run_agent(call) -> AgentResult` streaming
  events. It has two implementations: `ClientDispatcher` (remote,
  through `ahp-client`) and `ProviderDispatcher` (in-process, in the
  provider layer).

### 4.3 Where workflows live

A workflow is one `.js` file (or a `.py` file, §4.10). A source is anywhere such files are found. Every
source is surfaced to clients as an ordinary customization container, and
each workflow in it is a `prompt` child. So clients list and toggle workflows
with the existing customization actions (global, workspace or session
enablement), and each one shows as a slash command (`/fanout`) with
`whenToUse` as its description. AHP has no workflow customization type; the
prompt child's `_meta["ahp-py/flow"] = {kind, phases, source, language, shadowedBy?}` marks it
(`language` is present, as `"python"`, only for a `.py` workflow). If
clients want to render workflows differently, that becomes a `requests.md`
item.

| Source | Where the files are | Who defines it | Surfaced as |
|---|---|---|---|
| **workspace** | `.claude/workflows/*.js` in each of the session's working directories: the same files Claude Code uses in that project | the project, in its repo | a `directory` container per working directory |
| **user** | `~/.claude/workflows/*.js` of the account the host runs as: Claude Code's user-level folder | whoever runs the host | a `directory` container |
| **host** | `[workflows] dirs = [...]` in `flow.toml`, plus any bundled with the package (`ahp_host_flow/workflows/`: generic `fanout`, `review`) | the host operator, shared by every session and client | one `plugin` container, "ahp-host-flow" |
| **client** | `workflows/*.js` inside an Open Plugins plugin a client publishes (`activeClients[].customizations`), read from the client with server→client `resourceList`/`resourceRead` | the person at a client, from their own machine, even when the host is elsewhere | a `directory` container of ahp-host-flow's own per plugin (`ahp-host-flow.client.<plugin id>`), beside the client's plugin, which the host owns and re-expands on every republish |

- **Name resolution, first match wins:** client → workspace → user → host. A
  name can be qualified (`/host:fanout`, `/workspace:fanout`) to choose
  explicitly. A shadowed name is still listed, marked `_meta["ahp-py/flow"].shadowedBy`.
  `workflow("name")` inside a script resolves the same way, from the calling
  run's session, and takes a qualified name too (`workflow("host:child")`; an
  unknown source throws `unknown workflow source 'x' in 'x:child': use one of
  client, workspace, user, host`). As built, the engine never sees the catalog:
  the provider hands it the labeled directories (`WorkflowDir`) and the client
  entries as plain data (`RunOptions.client_workflows`, `.js` only), and a
  resume serves the journal's snapshot first. `workflow({scriptPath})` is a path in the session's working
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
  (`[workflows] sources = ["workspace", "user", "host"]`, so `client` is off by
  default and opt-in on a shared host). Whatever the source, a workflow's agents get
  the routing profile's permissions and never more.
- **Live updates:** a client republishing its plugin re-publishes the client
  workflows (`customizations_changed`). The local dirs (workspace, user, host) are
  watched too: a `PollingResourceWatcher` per session scans them every
  `[workflows] watch_interval` seconds (default 2.0; 0 turns watching off), and
  a change re-publishes the customizations with no message sent. A workspace
  `.claude/workflows` created later is picked up. A watch-triggered refresh
  never reads a client; client sources still follow the client's republish. The
  dirs are also re-read per run and on describe; `meta` is parsed, never
  executed.

### 4.4 What `agent()` maps to

| opt | AHP mapping |
|---|---|
| `model` | `Message.model = {id}`; aliases (`haiku`, `sonnet`, `opus`, `glm`) resolve through the routing profile (§5) |
| `effort` | the inner session's `config.effort` where the provider has it; otherwise ignored, with one `log()`. Since 1.1.0 it is also `chatMutable` on the child chat (§4.5), so a client can change one agent's effort mid-run |
| `isolation:'worktree'` | `git worktree add` under the run dir, used as the inner session's working directory; removed if unchanged, otherwise kept and its changeset published on the child chat, with Apply and Discard operations |
| `agentType` | an agent from the inner session's customizations; unknown means an error, as in Claude |
| `schema` | see below |
| approvals | per-profile policy, as the inner session's approval mode: auto-approve what the profile allows, and raise the rest in the child chat for a human. Since 1.1.0 the mode is `chatMutable` on the child chat (§4.5), so loosening one agent from a phone never loosens the others |
| skip / abort | cancelling the child chat's turn makes that `agent()` return `null`; cancelling the main turn aborts the run |
| errors | a resumable error uses `turnResume` once, then the call returns `null` with a `log()` line |

The prompt gets a short preamble equivalent to Claude Code's subagent
framing: "your final message is the return value; return raw data". This
matters most for non-Claude agents. Built (M11): `ProviderConfig.preamble`
(`true` by default, `false`, or a string; `engine/preamble.py` holds
`DEFAULT_PREAMBLE`, `preamble_text` and `with_preamble`). It is applied to what
the inner agent is sent: the in-process `UserMessage.text` and `raw["text"]`,
the fleet `start_session` prompt and the remote `chat.prompt`. It is not applied
to `AgentCall.prompt` (so journal keys do not change), the main chat's `agent`
tool-call input or the worker turn's published message. In a named chat
(`opts.x.chat`) only the first message carries it; follow-ups go to a session
that already has it. Test fakes strip it through `tests/preamble.py`.

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
  a new one. So M4 keeps a session per call, and sharing is a later
  optimisation for calls with the same working directory.
- **Steering.** 1.1.0 unifies steering and queued messages in one list with a
  per-entry kind. A child chat is read-only by default. If its profile makes
  it interactive, a steering message there is forwarded to the inner session
  (`SteersTurns`), which lets a person nudge one agent mid-turn. Done (M8): `interactive = true` in flow.toml opens worker chats `full`, and ahp-host's `GatesSteering` lets the session refuse a steering message with a reason. The run's
  main chat queues nothing while a run is active.
- **Titles and vendors.** `createChat`/`open_tool_chat` take a title, so a
  child chat is titled by `label` (or the prompt's first line).
  `ModelInfo.vendor` lets the run session advertise every inner provider's
  models with their vendor, so a client can pick a run's default model across
  Claude and GLM. On the remote side, `ahp-client`'s `create_chat(title=)`
  serves M7.

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
    small stdio relay the agent launches (every ACP agent takes stdio MCP, so
    only the relay was built; a loopback HTTP endpoint is not needed). Each call goes
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
- **Restart.** `ahp-host-flow` is a `ResumableAgentProvider`, and
  `FlowSession` implements the existing `ResumesTurns`. `ahp-host`'s graceful
  restarts fail a turn the restart cut off (`errorType` `host.restart`,
  resumable) and resume it through `chat/turnResume`: by itself, once, when a
  drain-restart cut it off; on a client's request when a crash did.
  `resume_turn` on a run's main chat relaunches the run from its journal,
  under the same turn, not left as transcript:
  - finished calls replay from the cache;
  - started fleet calls are **reattached**, since the node session kept
    running, so the run waits on it again;
  - in-process calls that were cut off run again live, with a `log()` line;
  - open gates are asked again (the old request was withdrawn on restore, see
    below).

  The session's resume state names each live run (main chat, turn, run id,
  and what `resume_turn` cannot read back from the turn: model, resume count,
  phase notices published), so it knows which run a resumed turn belongs to.
  Worker chats are not resumed: `resume_turn` declines them, and the cut-off
  call runs again in a new worker chat.
- **Open approvals in general (`ahp-host`).** `StoredSession` keeps the
  session's open input requests beside its `inputNeeded` entries. Nothing in
  the new process can answer one, so on restore each is withdrawn with a system
  notification naming it, rather than vanishing; a resumed turn asks again
  under a new id. That way no surface has to persist approvals on its own. An
  approval raised in a fleet call's node session lives on that node and
  survives the flow host's restart regardless.
- **Typed progress on a continued run.** One `runStarted` per run: a continued
  run emits a plain "resumed" notification instead of a second one, phases
  already announced are not announced again, and the re-dispatched step
  supersedes the one the restart cancelled.
- **Later milestones.** Started fleet calls reattach since M7 (remote calls do
  not: the remote turn left behind is stopped and the call dispatched again).
  Gates (`ask`, `approve`) are journaled (M10), on the journal's extensible record
  format (readers ignore unknown record types).
- **Upstream:** what a host must do with open input requests across a restart,
  and how a client learns they were withdrawn (§10; `ahp-host`
  `docs/requests.md` entry 11).

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
- **Shipped (M9).** The capability is `_meta['ahp-py/redaction']` at
  initialize; the extension actions are `ahp-py/chat/partRedacted` and
  `ahp-py/chat/messageRedacted`; the replay log is truncated rather than
  rewritten (`ahp-host` ADR 0007); `flow redact` reaches a running host through
  a request spool under `<state_dir>/redact`.

### 4.10 Python workflows

The engine is Python already. JavaScript only comes in through the Node
sandbox, which speaks a small JSON-RPC protocol (`sandbox/protocol.py`) to the
engine. A Python front end plugs in at the same point, so the scheduler, caps,
budget, journal, resume and every dispatcher apply unchanged. Python workflows
are an ahp-py extension: Claude Code runs only JS.

- **Workflow files.** A `.py` file in any workflow source, next to the `.js`
  ones:

  ```python
  meta = {"name": "review", "description": "...", "phases": [{"title": "Find"}]}


  async def main(args):
      phase("Find")
      found = await parallel(
          [lambda f=f: agent(f"review {f}", schema=FINDINGS) for f in args["files"]]
      )
      return [x for x in found if x]
  ```

  `meta` must be a literal and is read with `ast.literal_eval` without
  running the file, as acorn reads a JS one. The verbs (`agent`, `parallel`,
  `pipeline`, `phase`, `log`, `args`, `budget`, `workflow`) keep their JS
  semantics. `agent`'s options are keyword arguments; `parallel` takes
  zero-argument callables returning awaitables; a stage or thunk that raises
  resolves to `None`. They are globals in the module, and also importable from
  `ahp_host_flow.verbs` for editors and type checkers.
- **A runner process.** A `.py` workflow runs in a separate
  `python -I -m ahp_host_flow.sandbox.pyrunner` process that speaks the same
  JSON-RPC as `runner.mjs`. So the host side is the same `Sandbox` client, a
  crashing or blocking script cannot take the host down, and the per-run
  timeout kills it the same way. The process is not a security boundary.
- **Determinism is the author's job.** Python cannot ban `time.time()` or
  `random` the way the Node prelude bans `Date.now()`. Resume matches calls by
  content (§4.2), so a nondeterministic script costs cache hits, not wrong
  results. The docs say so plainly.
- **Trust.** A `.py` workflow is arbitrary code on the host. So by default it
  loads only from the **host** and **user** sources. Workspace `.py` files need
  `[workflows] python_sources = ["workspace", ...]` in `flow.toml`, and
  client-published plugins never supply one.
- **Mixing languages.** `workflow()` resolves a name to `.js` or `.py` (JS
  wins a tie, so Claude Code's behaviour does not change). A child in the
  other language runs in its own runner, sharing the run's state as primitives,
  just as JS children do (§8, M1 result), with one level of nesting.
- **A library API.** For Python code that is not a workflow file (a CLI,
  tests, another tool): `async with flow.run(dispatcher, options) as r:` then
  `await r.agent(...)`, `await r.parallel(...)` and so on, in-process, on the
  same scheduler and journal. A trusted caller needs no runner process.
- **Parity.** Every conformance script gets a `.py` twin, checked against the
  same expected-result sidecar.

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
load = "ahp_host_claude:from_config"
models = { haiku = "claude-haiku-4-5-20251001", sonnet = "claude-sonnet-5-5", opus = "claude-opus-5-5" }

[providers.glm]                                # in-process
load = "ahp_host_acp:from_config"
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

In M4a, `load` is called as a factory: with no arguments, with the `config` table as keyword arguments, or with `config=Path(...)` when `config` is a string. `[redact]` is accepted (`patterns`, `builtin`). Since M7 a provider has exactly one of `load` (in-process), `via = "fleet"` (with `node`, `agent`, and optionally `models`, `approval_mode`, `effort`, `reply_timeout`) or `url` (with `agent`, and optionally `token_file`, `models`, `idle_timeout`).

Routing lives in config, so a script needs no changes to send a phase to GLM.
A script can also pin one call with `opts.x.provider`.

## 6. Extensions

All of these are opt-in. Under Claude Code, `opts.x` is ignored (probed), and
the globals below come from `flow-shim.js` (a prelude that defines them as
no-ops or as `agent()` fallbacks), or the script guards them with `typeof`.

| Extension | What | Built on | As built |
|---|---|---|---|
| `opts.x.provider` | pin a call to a provider or remote node | routing | `routing.pinned_provider`, checked first by `RoutingDispatcher` |
| `ask(question, {choices?, schema?})` | a human gate: the run pauses until any client answers | `request_input` on the main chat | `gates.MainChatGates` over `engine/gates.GateRunner`: a `gate` request from the runner, journal records |
| `approve(summary, {diff?})` | yes/no before a destructive step | `confirm_tool_call` | the same, as a tool confirmation |
| `opts.x.chat` | reuse a named child chat across calls (a multi-turn agent) | same inner session | `ProviderDispatcher` named chats |
| `opts.x.attach` | pass prior results or files as message attachments | AHP attachments | `dispatch.message_attachments` |
| `canvas(url)` | a live view on the run | `canvas_set` (experimental) | `MainChatGates.canvas` via `canvas_set` |
| `meta.x.schedule` | a default cron when saved as an automation | automations | `catalog.schedule_of` and `ahp_protocol.ext.flow.with_offered_schedule` |
| shim | the globals under Claude Code | | `shim/flow-shim.js` |

Gates are journaled (§4.8): each writes an `open` record before it asks and an
`answered` record after, keyed by kind, question and options, and an answered
gate replays on resume. Their ordinals come from a counter of their own, so
`agent()` ordinals do not move. A gate's prompt and the answer are masked
(§4.9) before they are published, and the masked answer is what the script and
the journal get.

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
| M3 | **Structured turns** (§4.6), across packages: the `_meta` output schema, `ReturnsStructuredOutput`, host validation and the text fallback in `ahp-host`; `ahp-host-claude`; general client-tool support over MCP in `ahp-host-acp`; `output_schema` and `.structured` in `ahp-client`; the `requests.md` entry | a client asks Claude and an ACP agent (a fake in tests, GLM as an opt-in smoke test) for a schema and gets a validated result; a mismatch is retried, then fails with a structured-output code |
| M4a | **The provider core:** config, routing, lazy provider loading, workspace/user/host sources as customizations, slash/fenced run start, `ProviderDispatcher` worker chats titled by label, model/effort/approval mode, per-chat approval mode and effort via `ReconfiguresChats`, schema as a structured turn with engine re-validation, skip/abort, approval relay, models with vendor, `python -m ahp_host_flow`, `flow run --config` | the in-memory host tests over EchoProvider and scripted fakes pass |
| M4b (done) | **Typed progress** (§4.7) with `ahp_protocol.ext.flow` and `flow_view()`, credential masking (§4.9), `isolation: 'worktree'`, journal start records, a `from_config`-style factory in ahp-host-claude and ahp-host-acp so flow.toml can load them directly, the subagent prompt preamble (§4.4), live watching of workflow dirs (§4.3), live corpus runs | fan-out and triage corpus scripts pass against Claude; a Review phase routed to GLM returns a schema-valid result; a run is watched and controlled from VS Code or iOS, and `flow_view()` folds it from the transcript |
| M5 | **Python workflows** (§4.10): `.py` workflow files run by a Python runner process on the same JSON-RPC protocol as `runner.mjs`; `meta` read with `ast.literal_eval`; the verbs as a Python module; the `flow.run(...)` library API; source trust rules; `workflow()` across JS and Python; a `.py` twin of each conformance script | every conformance sidecar passes for its JS and Python twins; a `.py` workflow shows as a slash command next to the JS ones and runs, resumes and dispatches like them |
| M6 | **Durability** (§4.8): on `ahp-host`'s graceful restarts, stored open input requests (withdrawn with a notice on restore); `FlowSession.resume_turn` relaunches a run from its journal under the same turn | restarting the host mid-run, the run continues where it stopped (by itself after a drain-restart, on `chat/turnResume` after a crash); an open approval is withdrawn with a notice |
| M7 | **Fleet** (§2): fleet tools for `flow` sessions in `ahp-gateway`, bound to the run's principal; the `flow` agent type for `ahp-node`; fleet dispatch with thin child chats; reattach after restart; the direct remote `ClientDispatcher` (`create_chat(title=)`); the delegation-origin request | a run on one node starts calls on two others, each visible from every surface; restarting the flow host reattaches to calls still running |
| M8 | **Full corpus and operations:** the four corpus workflows unmodified; client-published workflows (+ automation capture); per-child changesets; steering a child chat (§4.5); automations | the plan/build/review corpus script lands a change; a cron automation runs a workflow |
| M9 | **Redaction after publishing** (§4.9): `Host.redact`, the experimental capability, the stored session and audit; `flow redact`; the `requests.md` entry | a redacted part disappears from live opted-in clients, from snapshots, from the store and from the run's journal; the audit records the fact without the content |
| M10 | **Extensions** (§6) + shim | `ask()` round-trips to a phone; a dual-target script runs in both runtimes |
| M11 (done) | **Close the gaps** (the M11 spec below): the M4b remainder (`from_config` factories, the subagent preamble, watched workflow dirs, live corpus tests), operations as a provider capability (so `ahp-node` has them), changesets across a restart, `workflow()` reaching client workflows and qualified names, the delegation origin | every item's proving test passes on Linux and Windows; a flow.toml naming `ahp_host_claude:from_config` and `ahp_host_acp:from_config` runs the live fan-out, triage and GLM-routed Review tests |
| M12 (done) | **Custom verbs and the resume-key hook** (the M12 spec below): host-side verbs a script calls like `agent()` but that run no model (`tool()`, `render()`, an `approve(person)` of the deployment's own), journaled and replayed like gates; an opt-in hook that adds the routed provider and model (or anything a callable returns) to each `agent()` call's resume key | the M12 proving tests pass on Linux and Windows; with no hook configured every journal key is byte-identical to M11's |
| M13 (done) | **Identity, typed workflow calls, quick fixes** (the M13 spec below): the README and proxy fixes, `--ping-interval` and `--max-size`, a non-loopback bind with a token; an optional principal on sessions, messages and answers; `meta.x.args`, typed workflow starts and client-chosen run ids | the M13 proving tests in §8 pass |
| M14 (done) | Folders, sandboxes and host policy | the M14 proving tests in §8 pass |
| M15 (done) | Approvals and changesets | the M15 proving tests in §8 pass |
| M16 | Limits, timeouts and fallback | the M16 proving tests in §8 pass |
| M17 (done) | Sandboxed workflow hosts, client tools in workers, durable workers | the M17 proving tests in §8 pass |
| M18 | A reference non-Claude agent, the support matrix, releases | the M18 proving tests in §8 pass |

**M0 spike result.** `tests/integration/test_spike_worker_chat.py` showed:

1. An inner `EchoProvider` session runs its turn through `ProviderChat.run_turn` on a worker chat from `publisher.open_tool_chat`. The worker turn's message has origin `agent`, the chat's origin is `{kind: "tool", chat, toolCallId}`, and the spawning tool call's result carries the `subagent` link (written by the test's own outer session, so this shows the wire shape, not provider behaviour). So the in-process design of §2 holds and the mirror design is not needed.
2. `run_turn` returns when the turn starts, so the dispatcher waits on its own completion signal. A text tee on the inner sink yields `agent()`'s return text.
3. The inner context's publisher is scoped by a wrapper. By design, chat-scoped calls (background work, changesets, canvases, terminals, nested tool chats) default to the worker chat. Only `background_work_set` and one dropped session-wide call are asserted, and EchoProvider never calls its publisher, so M4 must test each scoped method with a provider that uses it. Session-wide calls (title, customizations, MCP state, activity, progress) are dropped, so an inner session cannot rename or reconfigure the run. Since AHP 1.1.0 gave chats their own config overlay, `config_changed` is routed to the worker chat (`chat=`), not dropped.
4. Cancelling a worker cancels only the `run` callback (ahp-host's own tool-chat tests), so in M4 the dispatcher must cancel the inner session itself on `CancelledError`.
5. `ScopedPublisher` and the tee live in the test for now, and M3 moves them into the provider.

M0 sandbox decisions: scripts run in strict mode, matching acorn's module parse; `agent()` ordinals are 0-based and taken at issue time; in M0 a dispatcher error makes `agent()` return null, with a `log()` line, until M1 pins the full null rules; parallel, pipeline, budget and workflow arrive in M1.

M0 sandbox hardening, from review:
- The context is created from a null-prototype object. From `{}`, `this.constructor.constructor` was the host `Function`, which reached `process` and the real `Date.now`/`Math.random`.
- The prelude runs inside a block, so its `RealDate` is not in the script's global lexical scope.
- Python writes ASCII-only JSON, because Node's readline splits on raw U+2028/U+2029.
- Tests cover each of these routes. `node:vm` is still not a security boundary: the sandbox enforces determinism and keeps honest scripts off Node APIs. It is not meant to contain a hostile script.

**M1 result.** `parallel` and `pipeline` live in the sandbox prelude. The `Scheduler` (`engine/scheduler.py`) owns the concurrency semaphore (default min(16, CPUs−2), at least 1), the 1000-call cap, the output-token budget (mirrored into the script on each reply) and abort. `workflow()` runs the child in a `node:vm` context of its own, on the same `Scheduler`, with the agent counter, spent tokens and phase held in the host and shared as primitives. Its own `workflow` always throws, and nothing but JSON crosses between the contexts. (The first M1 build shared the parent's hooks into the child, and a value returned by `parallel` led back to the parent's `workflow`.) `Registry` is a plain ordered list of directories until M4's multi-source registry. `RunOptions.timeout` kills Node, argless `Intl` date formatting throws, and the stdout reader fails all pending requests on any exception. A host-side exception while handling an `agent()` or `workflow()` request is answered as an error rather than left pending. `conformance/` holds one script per covered §3 row, and `docs/format.md` pins the rules.

**M2 result.** `engine/journal.py` writes `<state_dir>/runs/<runId>/journal.jsonl` (a header with the script snapshot, args, call cap and budget; workflow snapshots when first resolved; one record per completed call), flushed per record, with a torn last line ignored. `engine/resume.py` serves calls by key (the n-th call with a key gets the n-th record with it, re-stamped with the new ordinal) until the first call with no match, then runs that call and every later one live, and logs where the cache ended. A resumed run gets a new run id and a journal that copies the reused records and, when it opens, every inherited child snapshot. (The first M2 build matched by ordinal in issue order, so a later pipeline stage, numbered in completion order, missed the cache on resume. A journal opened before any record was written leaked its file on a bad header.) `flow run` takes `--state-dir`, `--run-id` and `--resume`. Tests show identical (ordinal, key) sequences under varied latencies for synchronously issued calls, a kill mid-run resumes with only the in-flight calls dispatched, and an edited prompt makes that call and later ones live. `conformance/resume.js` covers the §3 resume row.

Open for M4:
- node-marked tests still skip silently when no Node is found, so CI must provide Node;
- `Temporal` (absent in Node 24's vm context today) must be banned if a future Node exposes `Temporal.Now`;
- the unchecked choices listed in `docs/format.md` need a probe against Claude Code;

**M3 result.** Structured turns landed across five packages.
- `ahp_protocol.ext.structured` holds the `_meta` key (`ahp-py/outputSchema`), the `StructuredOutput` tool name and the `ahp-py/structuredOutput` errorType (an `ErrorInfo.errorType` string, not a JSON-RPC code). A schema whose `type` is not `object` is wrapped under `value`, because MCP arguments and `structuredContent` are objects.
- `ahp-host` `core/structured.py` validates with `jsonschema`, allows at most 3 invalid calls, and sends up to 2 follow-ups inside the same AHP turn with `UserMessage.continues_turn`. The text fallback appends the schema to the prompt and parses the reply's last JSON block. `ReturnsStructuredOutput.offers_host_tools` marks a capable provider. Host tools are intercepted by `HOST_CLIENT_ID` in `run_client_tool` and are never shown to a client as client-executed.
- `ahp-host-claude` adds a second in-process MCP server, `host`. Claude Code fixes its tools at start-up, so a turn needing a schema the process was not started with restarts it on the same conversation. Mirrors use the fallback.
- `ahp-host-acp` supports client tools in general through a stdio relay launched with `sys.executable -m ahp_host_acp.relay`, which connects back over a loopback socket with a per-chat token in the server's env. The server is fixed at `session/new`, because ACP cannot change a live session's `mcpServers`; the tool list is dynamic with `tools/list_changed`, and an agent that ignores it keeps its first list. Relayed calls are allowed once without asking, a security decision, and the agent's own row for a relayed call is suppressed by title.
- `ahp-client` has `prompt(..., output_schema=)` and `TurnCompleted.structured`.
- `ahp-host` `docs/requests.md` carries the upstream request. An opt-in live test is enabled by `AHP_ACP_LIVE_AGENT`.

Open for M4 (structured turns):
- the engine re-validates `agent()` results (§4.4);
- §9's "bounded retries, then null plus a log()" maps a structured-output failure to `null`.

**M4a result.** The top-level modules: `config` parses flow.toml (unknown keys are errors); `routing` picks a provider per call (first matching rule on model alias, phase or label glob, else the default); `loading` imports inner providers by `load` path and calls them as factories (when the factory has a `roots` or `state_dir` parameter and the table does not set it, the loader injects the host's roots plus `<state_dir>/runs`, and `<state_dir>/providers/<name>`); `bridge` holds `ScopedPublisher` and `TurnTee`; `catalog` finds workflows in the workspace, user and host sources and publishes them as customizations; `commands` parses the message that starts a run; `dispatch` is `ProviderDispatcher`; `provider` is `FlowProvider`/`FlowSession`; `inprocess` is the in-memory host behind `flow run --config`; `__main__` serves it over WebSocket (loopback, port 4323). A run is one turn on a chat of the flow session. Worker turns run through `ProviderChat.run_turn` with a `TurnTee`. Structured calls reuse `StructuredTurn` on the worker turn's `ActionTurnSink` (`run_turn`'s message carries no `_meta`), and the engine re-validates the result. Skip is a self-cancelled dispatch; abort is the main task cancelled. The host does not call the session's `cancel` while worker turns run, so `CancelsChats` and task cancellation carry it. Inner sessions get `permissionMode` and `effort` (Claude's names) in their config. Review fixes: `ScopedPublisher` honours a `chat=` only for its worker chat or tool chats it opened (anything else is dropped or redirected), a worker turn cancelled before it starts is a skip (through `run_turn(on_end=)`, added to `ahp-host` for this: the review fix first found the host task by inspecting task frames, which was replaced before commit), every `agent()` failure completes the main chat's tool call, `flow run --config` fails at the first unanswerable confirmation, and providers load on first use. Open for M4b: the items in the M4b row; main-chat model selection only applies when the routed provider lists that model; workflow dirs are re-read at each run rather than watched.

**M4b result (first part).** `ahp_protocol.ext.flow` holds the descriptor shapes (`runStarted`, `phaseStarted`, `phaseFinished`, `runFinished`, `stepStarted`, `stepFinished`, `awaitingInput`), the meta key `ahp-py/flow`, a JSON Schema shipped as package data and the pure `run_view` fold; `ahp-client` exposes it as `flow_view()` on a chat and a session. Placement: the system notifications carry the run and phase descriptors; a step's `stepStarted` rides the main chat's `agent` tool call, and the running call's content links the worker chat. The result's `_meta` carries the whole step descriptor (`stepStarted` plus `stepFinished`), because the reducer spreads the result onto the tool call. `awaitingInput` is set while the worker chat has an open confirmation or input request (`TurnTee.on_waiting`). Masking (§4.9, pre-publish layer): `engine/redact.py` has `Redactor` (builtin shapes plus `[redact] patterns`, replaced by `[redacted]`, with a `secret` group so `key=value` masks only the value) and a line-buffered `StreamMasker` for streamed text. The `TurnTee` masks what it forwards, the dispatcher masks results and errors, the provider masks `log()` lines, run errors and the final value, and the journal is masked through `RunOptions.redact`. The inner agent's view is never altered. Coverage: masked are the worker chat's text, reasoning, tool calls and results, structured calls (`MaskingSink` around the turn sink given to `StructuredTurn`), approval and input prompts (`TurnTee` masks the published copies), step results, log lines, the journal and the `agent()` return; values under secret-named keys are masked at any depth. Known gaps, left to the after-publishing layer (this section, M9): terminals, canvases and background work an inner session opens through `ScopedPublisher`, and a secret split across a tool-call boundary mid-stream; the builtin list is best effort (no `key=value` values under 8 characters, URL userinfo or `sk-` keys; use `[redact] patterns`). Text now reaches a worker chat a line at a time. `awaitingInput` is derived from the `TurnTee`'s confirmations and input requests, an approximation of `inputNeeded`. Worktrees: `isolation: 'worktree'` creates `<state_dir>/runs/<runId>/worktrees/<ordinal>` from the run's git repository, gives it to the inner session, removes it if unchanged, otherwise keeps it and logs its path; a non-repository fails the call (`null` plus a log line). Changesets per child stay M8. Journal start records: the scheduler writes a `started` record (ordinal, key, dispatch handle) before the agent record; the dispatcher reports its handle (provider name, worker chat URI) through a context variable set around task creation; resume ignores a start without a finish except to log it; `JOURNAL_VERSION` stays 1. Leftovers fixed: skip and abort tests assert closed inner sessions and no leaked tasks, worker A's overlay never reaches worker B, an overlay that arrives before the inner session is registered is applied once it is, customizations are re-published when a source directory changes between runs, `/workflow workflow` reaches a workflow named `workflow`, an invalid `effort` logs one line, and discovery skips symlinks and non-regular files. A fan-out corpus script runs on fakes and, opt-in, through the configured providers. Open: the rest of the M4b row (from_config factories, the subagent prompt preamble, live watching of workflow dirs); `run_view` orders phases by first appearance, so a phase that starts after a later one's steps keeps its first position.

**M5 result.** The Python runner (`sandbox/pyrunner.py`) is standard library only and speaks the runner.mjs JSON-RPC on its own copies of stdin and stdout (UTF-8 with newline line ends, ASCII-only output), so a stray `print()` goes to stderr and cannot corrupt the stream. `meta` is read by `ast.literal_eval` without running the file; a non-literal `meta` is a `MetaError`. The verbs are awaitables issued at call time, so `agent()` order follows call order. Ordinals and the current phase are now assigned by the host when a request line arrives (`Scheduler.issue`), a protocol change: the `agent` request carries no ordinal. Across runners, arrival order decides. A cross-language child runs in a runner of its own, sharing the run's counter, phase, budget and caps; the settings `child`, `spent` and `args` and the `{value}` reply carry the state, and only JSON crosses. Trust: `.py` loads only from the host and user sources unless `[workflows] python_sources` includes `workspace`; the check is `RunOptions.python_dirs`, and the catalog lists a `.py` only from an allowed source, marked with a language in `_meta["ahp-py/flow"]`. The library API (`ahp_host_flow.run`, `FlowRun`) runs in-process on the same `Scheduler` and journal with a scriptless journal header, and no runner. Every `conformance/*.js` has a `.py` twin checked against the same sidecar; `determinism.json` has a `py_result`, the one documented difference, since Python cannot ban `time` or `random`. `flow run --config` serves a `.py` script from a temporary host directory. Open: the rest of M4b.

**M6 result.** Built on `ahp-host`'s graceful restarts, which landed first and are the restart model: a turn the restart cut off is failed with `errorType` `host.restart` (resumable for a resumable provider) and resumed through the standard `chat/turnResume` path, once and automatically after a drain-restart, by the user after a crash. (A first version added a provider capability that kept the turn open at restore and continued it; it was dropped for that one path.) `ahp-host`: `StoredSession` persists the open input requests as `inputRequests` beside `interruptedByRestart` (JSON only, old files still load, format version 1); on restore each is withdrawn, its `inputNeeded` entry removed and a system notification naming it added to the interrupted turn, never silently; a resumed turn asks again under new ids. Shutdown persists every session first, then stops persisting, so the store keeps the active turns and their requests. Notices carry `_meta['ahp-py/restart']`. Decision in ADR 0006; the upstream request is `docs/requests.md` entry 11. `ahp-host-flow`: `FlowProvider` is a `ResumableAgentProvider` (resume state `{flow: 1, runs: [{chat, turnId, runId, workflow, model, resumes, phasesStarted, phasesFinished}]}`) and `FlowSession` implements `ResumesTurns`: `resume_turn` on a run's main chat relaunches the run from its journal with `RunOptions.continue_run` (finished calls replay by key, cut-off calls are logged and dispatched again; the cut-off steps were cancelled when the host failed the turn), under tool-call ids with the prefix `agent-r<k>`; no second `runStarted`, a plain "resumed" line instead, and phases already announced are not announced again. A worker chat, another turn, or a run whose journal is gone is declined (the entry dropped with a log line). Tests run over a `FileSessionStore` in a tmp dir, simulating a drain-restart (`Host.drain` past its deadline) and a crash with a new host on the same store, for a `.js` and a `.py` run, twice in a row too: finished calls are not re-dispatched, the in-flight call is, and the result equals an uninterrupted run's. Open: fleet reattach (M7, since done); gates (since done in M10); known limits: a crash loses the store debounce window, so a run started within it may not be resumable, and a second interruption before already-finished later calls are reached runs them again, since the new journal holds only calls it has reached.

**M7 result.** Fleet dispatch, remote providers and reattach, across five packages. `ahp-gateway` joins sessions of `OrchestratorConfig.flow_agents` (default `flow`) as an active client offering `start_session`, `wait_for_sessions`, `read_session` and `stop_session` (its plan §10): sessions start as the principal who created the flow session, under `child_config` (a requested `permissionMode` only stricter; a requested `effort` dropped when `child_config` sets one), at most `max_running` per flow session (a start over it waits up to `slot_wait`; the count is read from the nodes, so it holds across a gateway restart), and another principal can neither prompt the flow session nor subscribe to or dispatch on a session its run started. `ahp-host-flow`: `ProviderConfig.kind` (`via = "fleet"`, `url`), `RoutingDispatcher` over `ProviderDispatcher`, `FleetDispatcher` and `ClientDispatcher` (engine) with `RemoteWorkerChats`, sharing `StepDispatcher`; the `flow` agent type for `ahp-node` (`agent.create`, entry point `ahp_host.agents`). A fleet call is driven from the main turn with no model in between; its thin worker chat links the node session, shows the status, result and usage; the call passes the run's model when the provider's `models` names it (or has none); a refused approval mode fails it with a line naming the provider and the setting. Each fleet tool call is watched: the offering client leaving or no answer within `reply_timeout` (default 900 s) loses it, and it is retried once on the rejoined gateway (`start_session` never), else `agent()` is `null` with a log line. Start records carry the session (`DispatchHandle.session`), and a continued run hands it to the dispatcher (`reattach_handle()`): fleet calls reattach (`wait_for_sessions` on the recorded session, re-dispatched with a log line if it is gone); remote calls stop the turn left behind and run again; the engine's log line says only that the call was cut off in that session. Only an abort or a skip stops a node session: the host stopping leaves it running (`FleetDispatcher(aborted=)`), which is what makes reattach possible. Found and fixed underneath, each with its own test: `ahp-host` restored a session's `activeClients` from the store, so after a restart the agent was told a client of the previous process could run its tools and a resumed run's first fleet call went to a client that was gone and hung (now emptied at restore, as if each had disconnected); `ahp-client`'s `ClientToolHost` kept answering on the connection it was built with after a reconnect replaced it, so a rejoined gateway ran a tool and its answer was lost (now rebound on reconnect). Remote limits: text mirrored at completion only, no answering approvals or questions (bounded by `idle_timeout`, default 600 s), no reattach. Tests: a real in-memory gateway with worker nodes and a flow node (`tests/fleet_hosting.py`; `ahp-gateway` is a test-only dependency, AGENTS.md) covers fan-out, structured results, `max_running`, skip, abort, a flow-host drain-restart and crash (reattached), a mid-call gateway restart (retried) and a refused approval mode; fake-gateway tests cover reattach, liveness and the reply deadline; the gateway's own tests drive each tool through a real client tool call from the flow node. Open: the delegation `SessionOrigin` (§10, `ahp-host` `docs/requests.md` entry 12) to point a node session back to its run; disposing a flow session mid-run leaves its node sessions running (no turn is left to stop them from).

**M8 result.** The acceptance corpus (§3): four scripts under `conformance/corpus` (`build_to_spec`, `fanout`, `triage`, `commit_prep`) run unmodified through the provider on a scripted fake (`tests/corpus_fakes.py`), with call counts 6, 4, 5 and 3, structured results threaded between stages, and the plan/build/review script landing its files; the fan-out script also has the opt-in live test. `ahp-host` gained `ReadsClientPlugins` (read the plugins published into a session, a client's or an automation's captured copy) and `GatesSteering`, plus the worker-chat input gate (worker chats accept only steering, only while their turn runs). Client source: `workflows/*.js` in a client's plugin are read through `ReadsClientPlugins`, listed first in a container of the provider's own with `shadowedBy`, run as `/name` or `/client:name` from the served text, re-published when the client republishes, opt-in through `[workflows] sources`; a client `.py` is never read. A saved automation naming a client workflow is captured at save time. Per-child changesets: a kept worktree is published on its worker chat with Apply and Discard. Steering: `interactive = true` forwards steering to an inner session that `SteersTurns`, else rejects it with a reason. Automations: `/<workflow> <json args>` runs unattended as an ordinary flow run. Open: `workflow()` cannot reach client workflows; no cron test in this package (ahp-host covers cron); changeset links are lost on restart; the operations are unavailable under ahp-node; `meta.x.schedule` (since done in M10).

**M9 result.** Redaction after publishing, across four packages. Protocol: `ahp_protocol.ext.redaction` (capability, extension actions, pure `apply_redaction`) and `flow.redacted_step`. `ahp-host`: `Host.redact` and `Host.redact_message` validate the target, publish through `Sequencer.redact` (selective delivery to opted-in clients, replay-log truncation and owed snapshots for the rest), write the store immediately, record a `chat.redacted` audit entry (who, when, target, reason, never content) and discard the original from the content store. `ahp-client`: opt-in (`redaction=True`), the mirror applies the actions, `PartRedacted` and `MessageRedacted` events. `ahp-host-flow`: the journal rewrite (`redact_agent_record`, value `[redacted]` plus a `redacted` marker, kept through resume), `redact_step`, the request spool and the server poller, and `flow redact` with exit codes 0, 1 and 3. Not redacted: the agent's own context (the inner session's conversation and provider state), the worker turn's prompt, the run's final value, the journals of resumed descendants, wire-log frames written before the redaction, and seeded session titles. Tests prove each: two-client host tests (opted-in sees it live, stock client gets no unknown action and sees the redacted state after resubscribe, neither gets the original through replay, the store file and audit never hold it), the ahp-client mirror tests, `test_journal_redaction_*`, `test_flow_redaction_*` and `test_cli_redact_*`. Exactly what the flow tests prove: `test_journal_redaction_resume_replays_redacted_value` (library level) shows a run resumed from a redacted journal replays `[redacted]` without dispatching the call; `test_flow_redaction_run` (a real run on a host with a `FileSessionStore`) shows that after `redact_step` plus `Host.redact_message` on the worker prompt the stored session file holds no trace of the secret for the worker chat or the main chat, and neither does the journal; `test_flow_redaction_survives_restart_and_resume` shows the same after a crash, a redaction on the restarted host and a second restart, and that a client's `chat/turnResume` of the cut-off run then replays `[redacted]` for the redacted call (not re-dispatched), re-dispatches only the held one, and leaves no trace in the stores or the new journal. Review fixes: the audit actor is required (`redact_step(actor=)`, the spool records `flow redact (local operator <user>)`); a journal's writer holds `journal.lock` and `redact_agent_record` raises `JournalBusyError` rather than rewrite under it (this process or another; a journal from before the lock is not detected); a redaction that fails part way reports what was already redacted and a retry finishes it. Still not redacted: a secret written literally into the script or its args in the main chat's message holding them (in the stored session and on the wire to clients), and in the inner agent's own context. Since M17 the journal header's `source`/`args` and the `workflow` records are stored masked, with their sha256 (unless `[journal] keep_source`), and a wire log the flow host writes is masked by the same rules.

**M10 result.** The extensions of §6, across ahp-protocol and ahp-host-flow (and an `ahp-host` fix: request ids carry a per-process boot token, so an open gate is withdrawn, not mistaken, after a restart). Gates: `ask`, `approve` and `canvas` in both runners with identical argument errors, `GateRunner` (separate counter, re-validation, abort), `type: gate` journal records with replay by key, and `MainChatGates` on the run's main chat (an input request, a tool confirmation, `canvas_set`); an open gate cut off by a restart is withdrawn with a notice and asked again on the resumed turn. `opts.x.provider` pins a call through routing. `opts.x.chat` keeps a named worker chat and inner session across calls. `opts.x.attach` passes confined files and prior results as message attachments. `meta.x.schedule` is offered on the customization and applied by a client. `shim/flow-shim.js` and the dual-target script run under a Claude-Code-like harness. Proved by `test_flowext_gates`, `test_flowext_scripts`, `test_flowext_gatejournal`, `test_flowext_hostgates`, `test_flowext_gaterestart`, `test_flowext_provider`, `test_flowext_namedchat`, `test_flowext_attach`, `test_flowext_schedule` and `test_flowext_shim`. Gate options are re-checked at the host (an adversarial audit, `test_flowext_sandbox_escape`, found a script could send options its runner never checked; no realm escape). On a client's cancel the host's own turn cancellation clears the open gates, so the gates' withdrawal frames are no-ops there and the proof that they are sent is the unit tests `test_flowext_gateform_abort_withdraws_*`. Limits: a named chat keeps its first call's config (effort, approval mode; a follow-up asking for another logs a line) and loses the inner agent's memory on resume unless its provider resumes sessions; attach and chat are refused for fleet and remote providers; canvases are experimental and stay on the main chat after the run; the library API has no gates, so `ask()` fails there; an `ask()` answer is masked before the script sees it.

**M11 result.** Every item is closed, each by its own tests. The `from_config` factories: the acp, claude and flow `from_config` tests (a flow.toml loads both through the fakes). The preamble: the `preamble` tests (each dispatch kind, `false`, a custom string, identical journal keys). Watched workflow dirs: `flow_watch`. Operations as a provider capability and eager resume: `provider_operations` and `resume_on_restore` in ahp-host, `flow_node_operations` in ahp-host-flow. Changesets across a restart: `restart_changesets`. `workflow()` reaching client workflows and qualified names: the `workflow_sources` conformance script and `client_workflows`. The delegation origin: the `delegation` tests in all five packages. The live runs are opt-in (`AHP_FLOW_LIVE_CONFIG`); on 2026-10-18 the smoke tests, the fan-out and triage corpus scripts against Claude and a Review phase routed to GLM (opencode on `ollama/glm-5.3-flash:cloud`) all passed through a flow.toml naming both `from_config` factories. They found three things the fakes could not: a plugin Claude Code reports with a relative path failed ahp-host-claude's whole customization tree (fixed); a client driving a run with ahp-client's default 300 s `idle_timeout` gives up while a slow step runs, since the main chat is quiet (the live tests pass `idle_timeout=None`, and the README says so); and an agent with nothing to read reaches for a shell, which under `acceptEdits` waits on an approval (the live triage test gives it files). Building M11 also found `PollingResourceWatcher` reporting nothing on Windows (fixed in ahp-host with `native_path_from_file_uri`). The phone, VS Code and iOS checks stay by hand.

**Open after M10.** What M11 left: a phone round trip of `ask()` and watching a run from VS Code or iOS (both by hand), and raising the §10 upstream requests.

**M11 spec.** Each item names its proving test; none is a live test (live ones are extra, opt-in). The items are ordered so a later one can build on an earlier one: 1 before 8, 4 before 5.

1. **`from_config` factories (M4b).** Today `load` is called as `factory()`, `factory(**table)` or `factory(config=Path)`, and neither inner provider can be built that way: `AcpProvider` needs an `AgentSpec`, `ClaudeProvider` needs `ModelInfo` objects and an async `discover`, and plan §5's `config = "…/opencode.toml"` raises a `TypeError`. Add a synchronous module-level `from_config(config: Path | None = None, **options)` to `ahp_host_claude` and `ahp_host_acp`. `config` is that package's own TOML file (the one its CLI reads, with its server-only keys such as `port`, `bind` and `token_file` ignored); `options` take the same keys as that package's `ahp-node` agent options (`agent.py`), and override the file. Reuse the option parsing `agent.py` already has rather than copying it (move shared helpers out of private names if needed, keeping `agent.py` and `config.py` in step as ahp-host-acp's AGENTS.md requires). Roots: each factory takes `roots` and `state_dir` keywords; flow's loader passes them when the factory accepts them and the table does not set them: `roots` as the flow host's roots plus `<state_dir>/runs` (so a worktree cwd is inside them), and `state_dir` as `<flow state_dir>/providers/<name>`. Claude's model discovery is async: the factory returns at once and the provider discovers on first use (its `agent` info lists flow's `models` aliases until then); the loader stays synchronous. Add the `[claude]` and `[acp]` extras to ahp-host-flow's `pyproject.toml` (plan §7) and refresh `uv.lock`. Fix plan §5's example and the README's wrapper-factory text to name `from_config`. Proving tests: in each provider package, `from_config` from a TOML file and from keywords builds a provider with the right roots, models and spec (ahp-host-claude with its fake SDK, no subprocess); in ahp-host-flow, a flow.toml with `load = "ahp_host_acp:from_config"` and `config = "<tmp>/acp.toml"` drives a call through the fake ACP agent, and one with `load = "ahp_host_claude:from_config"` loads and lists models.
2. **The subagent preamble (§4.4, M4b).** A short framing paragraph, in our own words: the final message is the call's return value; return the data itself, without commentary; when a schema is set, the structured result is the value. It is prepended (preamble, blank line, prompt) only to what the inner agent is sent: the in-process `UserMessage.text` and `raw["text"]`, the fleet `start_session` prompt, the remote `chat.prompt`. Never to `AgentCall.prompt` (the journal key and resume must not change), the main chat's `agent` tool-call input, or the worker turn's published user message. The structured-output fallback's schema text still comes after the prompt. `ProviderConfig.preamble`: `true` (default), `false`, or a string replacing the text. Test fakes that script by prompt strip the preamble through one helper in `tests/`. Proving tests: the inner agent receives preamble plus prompt for each dispatch kind (in-process, fleet, remote); `preamble = false` and a custom string work; journal keys of a run are identical with and without the preamble.
3. **Watched workflow dirs (§4.3, M4b).** Today a source dir change is only noticed on the next message. Each `FlowSession` runs one `ahp_host.core.watches.PollingResourceWatcher` over its local source dirs (workspace `.claude/workflows` per working directory, user, host), started when the session is created or restored and stopped in `aclose`. A change schedules the existing coalescing refresh, local sources only: it never reads a client (AGENTS.md). `[workflows] watch_interval` (seconds, default 2.0; 0 turns watching off). A workspace `.claude/workflows` created after the session started is picked up (watch the working directory's `.claude`, or re-evaluate the watched set on each refresh). Update §4.3 "Live updates". Proving test: adding, editing and deleting a `.js` in a watched dir publishes `customizations_changed` with no message sent, and `watch_interval = 0` publishes nothing.
4. **Operations as a provider capability (ahp-host, ahp-host-flow).** Changeset Apply and Discard exist only where the embedder calls `register_worktree_operations(host, …)`: `flow run --config` and `python -m ahp_host_flow`, never `ahp-node`. Add an `ahp-host` provider capability (in the style of `UpdatesAgentInfo.attach_agent_updates`) through which a provider declares its operations and the `Host` registers them when it is built; an operation id already registered by the embedder is an error naming it. `FlowProvider` implements it; `inprocess` and `__main__` stop registering by hand; `register_worktree_operations` is kept only if something else still needs it. Document it in ahp-host's README and `docs/guide/` (ahp-host AGENTS.md: undocumented means unfinished). Proving tests: in ahp-host, a provider with the capability gets its operations invoked through the host and a duplicate id fails; in ahp-host-flow, a `flow` agent built by `ahp-node`'s `create_agents` path publishes a kept worktree's changeset with Apply and Discard that work.
5. **Changesets across a restart.** A kept worktree's changeset (`WorktreeChangesets._kept`, the random changeset URI, the base commit) lives only in memory, and the host does not persist changeset channels, so after a restart the worker chat may name a changeset that no longer exists and Apply fails. Persist each kept worktree (worker chat, run id, ordinal, repo, path, cwd, base, label) in the flow session's resume state, removed when applied or discarded. When the session is restored, republish each one whose directory still exists on its worker chat (a new changeset URI) with working operations, and drop the rest with a log line. In ahp-host, restore must not leave a chat naming a changeset channel that was not restored (prune `ChatState.changesets` and the session's changeset lists, or restore them; pick one, test it, and note it in ahp-host's CHANGELOG). Also give `python -m ahp_host_flow` a `FileSessionStore` under its state dir (`--no-store` to turn it off), so its restarts are durable, and fix the stale "durability is M6" text there. Proving test: over a `FileSessionStore`, a run keeps a worktree, the host is recreated on the same store, and a client applies the republished changeset on the worker chat; one whose directory was deleted is dropped with no dead link.
6. **`workflow()` reaching client workflows, and qualified names.** Inside a script, `workflow(name)` resolves only the local dirs, and `WORKFLOW_NAME` rejects `source:name`. Give `RunOptions` the client entries as plain data (name, source text, filename; `.js` only, never a client `.py`), checked first, so a script resolves names with the same precedence as a top-level run (client → workspace → user → host), and label each workflow dir with its source so `workflow("host:fanout")` and `workflow("workspace:fanout")` choose explicitly. The engine still never imports `ahp_host` or catalog types. Resume keeps using the journal's snapshots. Proving tests: a script started from the workspace calls `workflow("x")` and gets the client's `x` when a client publishes one, `workflow("workspace:x")` gets the workspace's, an unknown source prefix is an error naming it, and a resumed run replays the client child from its snapshot after the client disconnected; plus a conformance script for the qualified form with its `.py` twin.
7. **The delegation origin (§2, §10 item 5).** A node session started by a fleet call cannot point back to its run. Until upstream adds a kind, use `SessionOrigin` `{kind: "ahp-py/delegation", session, chat, toolCallId}` (the spec marks the kind nonexhaustive and clients must preserve unknown kinds verbatim): `ahp_protocol.ext.delegation` holds the kind, a builder and a parser. `ahp-host` accepts a requested origin from `createSession` `_meta["ahp-py/origin"]`, generically and only for that kind (never `automation`, which only the host sets; anything else is ignored), seeds it as the session's origin, and keeps it through the store. `ahp-client`'s `create_session` gets an `origin=` keyword. `ahp-gateway`'s `start_session` fleet tool takes optional `originChat` and `originToolCall` and passes `{session: the flow session, chat, toolCallId}`; `fleet.py` sends the worker chat and the main chat's `agent` tool-call id. Update `ahp-host` `docs/requests.md` entry 12's "What ahp-py does". The gateway stays plain AHP to its nodes (`_meta` on `createSession` is plain AHP). Proving tests: ahp-host seeds and persists the requested origin, ignores `automation` and unknown kinds; a fleet call's node session's summary carries the delegation origin naming the run's worker chat and tool call, through a real in-memory gateway.
8. **Live corpus runs (M4b).** Add opt-in live tests beside `test_fanout_corpus_live` (same `AHP_FLOW_LIVE_CONFIG`): the triage corpus script, and a Review phase routed by `[routing]` to a provider named `glm` returning a schema-valid result (skipped unless the config has a `glm` provider). Document a sample live flow.toml using the two `from_config` factories in the README. Then mark M4b and M11 done in the status line and §8, empty the "Open after M10" list of what M11 closed, and update both AGENTS.md files and every touched CHANGELOG. Proving test: `test_flow_toml.py` parses the README's sample live flow.toml.

Out of M11, needing a person: a phone round trip of `ask()` and watching a run from VS Code or iOS (by hand), and raising the §10 requests upstream.

**M12 spec.** Two hooks a deployment asked for. Each item names its proving tests; no live model is needed anywhere. Item 1 before item 2 only because both touch the journal's record types.

1. **Custom verbs.** A script calls a verb like any global (`await tool('search', {q})`, `await render(view)`, `await approve(person, summary)`), and a host-side handler answers it without a model.
   - **Declaring them.** flow.toml `[verbs]` maps a global name to an import path, loaded like a provider's `load` (lazily, on first use, by import path, never statically): `tool = "my_pkg.verbs:tool"`. The library API takes `verbs={name: handler}`. A name must be an identifier; Claude Code's verbs and runtime names (`agent`, `parallel`, `pipeline`, `phase`, `log`, `args`, `budget`, `workflow`, `meta`) are refused at config load with an error naming them. The ahp-py extension globals (`ask`, `approve`, `canvas`) may be replaced, which is how a deployment gives `approve` its own meaning (`approve(person, summary)`); the replaced built-in is then not defined in that run, and the catalog/README say so. A verb name unknown to a run is simply not defined (`typeof x === 'undefined'`), as under Claude Code.
   - **The handler.** Engine code (`engine/verbs.py`, no `ahp_host`): `VerbHandler = Callable[[VerbCall], Awaitable[JSON]]`, with `VerbCall(name, args: tuple[JSON, ...], ordinal, call_id, phase, run_id, context)`. `call_id` is stable across a resume (derived from the verb key and its occurrence, as resume matches calls), so a handler waiting on a person can find the request it already made. `context` is a `VerbContext` the provider fills: the session and main-chat URIs, the run's principal when the host knows it, `log(text)`, and `ask(question, options)` / `confirm(summary, options)` that raise an input request or a tool confirmation on the run's main chat through the existing `MainChatGates` (so a handler can involve a human over AHP without its own channel). The library API's context has no ask/confirm (they raise `GateUnavailableError`, as gates do there).
   - **Crossing the sandbox.** The host sends the run's verb names to the runner in its settings; `runner.mjs` and `pyrunner.py` each define one global per name that JSON-clones its arguments and sends a `verb` request (`{name, args}`), and resolves to the reply's value or throws an `Error` carrying the handler's message. Nothing but JSON crosses; the host re-checks the name against the run's verbs (a script can make its runner send anything; see the gate options rule) and refuses an undeclared one. A child workflow gets the same verbs.
   - **Semantics.** A handler's exception makes the verb throw in the script (unlike `agent()`, which returns `null`), so a script can `try`/`catch` it; abort cancels running handlers; verbs do not count toward the agent cap or the budget and run outside the agent concurrency semaphore. Arguments and results are masked by the run's `Redactor` before they are published or journaled, and the masked result is what the script gets (as for gates). A non-JSON result is an error naming the verb.
   - **Journal and resume.** `type: "verb"` records (`open` before the handler is called, `done` with the result or `error` with the message after), keyed by sha256 of the canonical JSON of name and args, with their own host-side counter (never the `agent()` or gate ordinals). Resume replays a `done` or `error` record by key without calling the handler; an `open` one cut off by a restart calls the handler again with the same `call_id`, with a `log()` line. Readers that predate M12 ignore the type.
   - **Progress.** Each verb call is a tool call on the run's main chat, titled by the verb name, its input the masked args and its result the masked value, grouped by the current phase. `ahp_protocol.ext.flow` gains `verbStarted`/`verbFinished` descriptors (name, ordinal, phase, status `ok` | `error`) and `run_view` lists verb calls beside steps (a `kind` field telling them apart), with the JSON Schema file updated; a stock client sees an ordinary tool call.
   - **The shim.** `flow-shim.js` cannot know a deployment's verbs; the README shows the `typeof` guard for a dual-target script.
   - **Proving tests:** a `.js` and a `.py` script calling a configured verb get its value (conformance script plus `.py` twin, sidecar); a handler error throws and is catchable; an undeclared name sent by a hostile runner is refused at the host; a reserved name is a config error; replacing `approve` gives the custom handler; a resumed run replays a finished verb without calling it, and re-calls a cut-off one with the same `call_id` (over a `FileSessionStore` restart, as the M6 tests do); `context.ask` round-trips through the main chat in an in-memory host test; masking applies to args and result; `run_view` shows verb calls.
2. **The resume-key hook.** Today `call_key` hashes label, opts, phase and prompt, so changing a phase's routing or a model alias in flow.toml and resuming replays results another model produced. Add `RunOptions.key_extra: Callable[[AgentCall], JSON] | None` (engine; default `None`). When set, its value is added to the key's payload under `extra`; when `None`, the payload and so every key is byte-identical to M11's (Claude Code's resume rule, plan §3, is unchanged by default). The scheduler computes it at issue time, before dispatch, so it must be a pure function of the call and the config. The provider sets it from flow.toml `[resume]`: `key = "model"` adds `{provider, model}` from routing (the routed provider's name and the model id the call resolves to after `models` alias expansion, or `null` for the provider's default; config only, never the provider's discovered list, so it is known before dispatch); `key = "pkg.mod:fn"` loads a callable by import path that gets the call and that resolved route and returns any JSON. The library API takes `key_extra=`. The journal header records the key mode (`"default"`, `"model"` or the import path); resuming under a different mode logs one line saying so (nothing then matches, and the run goes live from the first call). Proving tests: a default run's keys equal pinned hashes for a fixed call (guarding byte-identity); with `key = "model"`, changing a phase's routed provider or a model alias between a run and its resume makes those calls live and leaves the others cached; a custom callable's value changes keys; the header records the mode and a mode change logs its line.
3. **Docs.** README sections for both hooks (flow.toml examples with placeholder package names), `docs/format.md` notes that custom verbs and `[resume]` are ahp-py extensions outside Claude Code's format, plan status line and an **M12 result.** paragraph, both AGENTS.md invariants (verbs: host re-checks names, reserved names, separate counter, masked; keys: byte-identical by default), every touched CHANGELOG.

**M12 result.** Both hooks landed, each proved by its own tests, with no live model anywhere.

1. **Custom verbs.** Declaring and names: `test_customverb_config` (the `[verbs]` table, lazy import by path, reserved names and, beyond the spec, names that would shadow a sandbox global: JS standard globals, `require`/`process`/`fetch` and the like, Python builtins; `ask`/`approve`/`canvas` stay replaceable). The wire and both runners: `test_customverb_runner` (ordinals, call ids, masking, abort, an undeclared name refused at the host) and `test_customverb_sandbox` (hostile runner source in JS and Python, values staying in the realm, children). The journal: `test_customverb_journal` (`open`/`done`/`error` records, a finished record winning, replay by key, redaction keeping the line). The library: `test_customverb_library`. Hosted runs: `test_customverb_hosted` (JS and Python scripts, a replaced `approve`, an undefined verb, `run_view` listing the calls) and `test_customverb_context` (`context.ask` and `confirm` round-tripping through the main chat). Restart: `test_customverb_restart` (a finished verb replays without its handler; a cut-off one is called again with the same `call_id`, over a `FileSessionStore`, drain and crash). The conformance script `extensions/verbs` (`.js`, `.py` twin and `verbs.json` sidecar) runs through `test_flowext_scripts`, and the protocol side is `test_flow_ext_verb_builders_exact`, `_verb_schema`, `_verb_fold`, `_verb_running_and_cancelled` and `_verb_malformed_ignored` in ahp-protocol's `test_flow_ext`.
2. **The resume-key hook.** `test_keyhook_engine` (pinned hashes guarding byte-identity with M11, the hook changing keys, a same-mode resume replaying from the cache, a mode change logging its one line, hook failure, option validation, the library API and a hook set on `RunOptions` surviving `run()`), `test_keyhook_route` (the routed provider and model, the `[resume]` table, a changed phase route or model alias making only that earlier call live while later unchanged calls replay under `continue_run`, a custom callable) and `test_keyhook_hosted` (the journal records `default`, `model` or the import path; resuming a run through the provider after a model alias changed dispatches only the changed call and the cut-off one).
3. **Docs.** README `Custom verbs` and `Resume keys`, `docs/format.md`, both AGENTS.md invariants and the CHANGELOGs; the README and format doc tests cover them.

Known limits. A verb's `ask`/`confirm` requests are not journaled (a handler finds its request by `call_id`); `principal` is None until the host passes one. A verb replayed from the journal is not published again as a main-chat tool call: the continued turn still holds the original. `run_view` lists verb calls in a separate `verbs` tuple beside `steps` (`StepView.kind` tells the two apart) rather than mixed into `steps`. The `model` key mode reads config only (the routed provider and the alias-expanded model id), never a provider's discovered model list, so a change the config does not show does not invalidate a cached call.

**Post-M12 plan (M13-M18).** Fourteen requests from a deployment, plus three nits, grouped into six milestones. Each item names its proving tests. None is a live test; live ones are extra and opt-in. Order matters in four places:
- M13's principal comes before M15's `approve(person)` and M16's quotas.
- M14's sandbox comes before M14's hard ACP enforcement.
- M15's remote approvals come before M17's remote client tools, because both iterate the remote `TurnStream`.
- M16's error kinds come before M16's fallback.

Two facts from the survey (2026-10-19) shape the items:
- **No `spawn_scrubbed`.** Nothing in this repo is called `spawn_scrubbed`. No spawn path filters the environment: ahp-host-acp spawns with `{**os.environ, **env}` (`jsonrpc.py`), and Claude inherits it too. So the allowlist in M14 item 2 is new code, not a layer in front of something that exists.
- **ACP's own tools are invisible.** ahp-host-acp advertises `fs` and `terminal` as false, so an ACP agent runs its own tools, and the host sees only what the agent chooses to ask permission for.

**M13: identity, typed workflow calls, quick fixes.**

0. **Nits.**
   - README "Client workflows" still says `workflow('name')` does not reach client workflows. M11 item 6 made it reach them (`.js` only), as the CHANGELOG and the README's own source list say. Fix the line, and add a README doc test asserting the sentence is gone.
   - `python -m ahp_host_flow --bind <non-loopback> --token-file t` always fails: `_serve` never passes `allow_remote` to `serve_websocket`, which refuses any non-loopback bind. Also, the flow's `_is_loopback` accepts `127.0.0.2` but the server's set does not. Fix both by sharing ahp-host's loopback check.
   - Behind a WebSocket proxy, expose `--ping-interval` and `--max-size` (today the server's 1 MiB default is far below the client's 16 MiB). Add a proxy section to the README: the token in a header rather than `?tkn=`, because proxies log the URI; header-asserted principals linking ahp-host's "Embedding it behind a proxy"; and idle-timeout settings for the proxy.
   - Proving tests: the real `_serve` binds `0.0.0.0` with a token, without the monkeypatch `test_flow_main` uses today. A client connects through a minimal in-test TCP or WebSocket relay that forwards the upgrade with an `X-Forwarded-For` header and a header token, and a run completes through it.
   - The OpenHands ACP lane nit has no source here: no GOTCHAS file, and no mention of OpenHands, exists anywhere in this repo or its history. It is left out until someone points to the file it came from.
1. **The principal (ahp-host, ahp-host-flow).**
   - Add an optional `Policy.principal_of(info: ConnectionInfo) -> str | None`, looked up with `getattr` like the other optional hooks. `OwnedSessionPolicy` already has one.
   - The host stamps `AgentSessionContext.principal` from the creating connection, and `UserMessage.principal` from the connection that sent each `chat/turnStarted`. The value persists in session metadata, beside the owner `OwnedSessionPolicy` stores.
   - In `_react`, the host adds `answeredBy` to `RequestOutcome`, surfaced as `InputOutcome.answered_by` and `ToolConfirmationOutcome.answered_by`. `clientId` stays a non-identity.
   - ahp-host-flow puts the turn's principal (only the turn's: never the session creator's, so a queued, steered or host-started turn runs with `None`) on the run entry and its resume state, and `MainChatVerbs` fills `VerbContext.principal`. `VerbContext.confirm` and `ask` return the outcome with `answered_by`, so a handler can implement `approve(person)` by re-asking when the wrong person answered.
   - No gateway change: a node already gets per-principal credentials through `CredentialSource`, and a deployment's `principal_of` reads them.
   - Proving tests:
     - ahp-host: a header-asserted principal reaches the provider's context and each `UserMessage`, survives a store restart, and is `None` without the hook; `answered_by` names the answering connection's principal (`test_multi_user` style).
     - ahp-host-flow: a verb sees the principal; an `approve(person)` handler re-asks when another principal answers (`test_customverb_context` style); through a real in-memory gateway (`fleet_hosting.py`), the flow node sees the gateway principal's subject.
2. **Typed workflow calls (ahp-protocol, ahp-host-flow, ahp-client).**
   - **Schema.** `meta.x.args` is a JSON Schema literal, validated at catalog time like `meta.x.schedule`, published as `_meta["ahp-py/flow"].argsSchema` on the prompt child, and added to `flow.schema.json`.
   - **Typed start.** A client sends an ordinary `chat/turnStarted` whose message `_meta["ahp-py/flow"]` is `{workflow: "<source:name>", args, runId?}`. The text stays `/name {json}`, so stock hosts and the history still read. `send_user_message` prefers the `_meta` over parsing, validates `args` against `argsSchema`, and fails the turn with `errorType` `flow.badArgs` naming the first error.
   - **Run id.** A client-chosen `runId` is used when it is free; otherwise a fresh one is minted and reported in `runStarted`, as today.
   - **Helpers.** `ahp_protocol.ext.flow` gains a pure `workflows_of(customizations) -> tuple[WorkflowInfo, ...]` (name, qualified name, description, phases, args schema, source, language). ahp-client adds `Session.workflows()` and `Session.start_workflow(name, args, run_id=None)`, which returns the turn stream with `run_id` known up front.
   - Proving tests:
     - Protocol: builder, schema and fold tests in `test_flow_ext`.
     - Catalog: the schema is published, and a non-literal or invalid schema is a catalog warning, not a crash.
     - Hosted: a typed start runs with the given run id; bad args fail with `flow.badArgs` and dispatch nothing; a stock-text start still works.
     - ahp-client: `workflows()` lists them, and `start_workflow` returns the run id before the first event.
     - A `.py` workflow's `meta["x"]["args"]` works the same.

**M13 result.** The three groups landed with no live model anywhere.

1. **Quick fixes.** `--ping-interval`, `--max-size` (16 MiB default, as the client) and a non-loopback `--bind` with `--token-file`: ahp-host `tests/unit/test_wsopts.py`; ahp-host-flow `tests/integration/test_flowproxy.py`. The README line on client workflows is current, and a proxy section was added.
2. **The principal.** ahp-host `tests/integration/test_hostprincipal.py` and `test_answeredby.py`; ahp-host-flow `tests/integration/test_flowprincipal.py` and, through a real in-memory gateway, `test_flowprincipal_fleet.py`.
3. **Typed workflow calls.** ahp-protocol `test_flow_ext` (`test_flow_ext_workflow_*`); ahp-host-flow `tests/unit/test_typedargs_catalog.py` and `tests/integration/test_typedstart.py`, `test_startworkflow_client.py`; ahp-client `tests/client/test_start_workflow.py`.

Known limits. Queued, steered and host-started turns carry no principal. Absent args are not validated. The OpenHands nit stays out.

**M14: folders, sandboxes and host policy.**

1. **A call in a given folder (`opts.x.cwd`).**
   - The folder is an extension option, not a new `isolation` value, because `isolation` is Claude Code's (§3).
   - `dispatch.call_directory(opts, roots)` is parsed like `message_attachments`: resolved strictly, a directory, inside `_roots()`, with a relative path resolved against `roots[0]`. It replaces `self.working_directories` in the inner `AgentSessionContext`.
   - With `isolation: 'worktree'`, the folder is the base of the worktree (`create_worktree(workdir=cwd)`), and the inner cwd is its relative path inside the worktree.
   - With `opts.x.chat`, the first call fixes the folder; a later different one logs a line, as config does.
   - Fleet passes `folder` to `start_session`, and the node's roots decide. Remote passes `working_directories=[uri]` to `create_session`. Both need an absolute path or a `file:` URI; a relative one fails the call with a line.
   - Unknown `isolation` values become one log line instead of being silently ignored. Fleet and remote log that `isolation: 'worktree'` is not applied there.
   - The key already covers `opts`.
   - Proving tests:
     - The inner session gets the folder.
     - A folder outside the roots and a file path each fail the call with a line.
     - With a worktree, the worktree is cut from that folder.
     - Fleet `start_session` and remote `create_session` receive the folder (`fleet_fakes`, `remote_dialer`).
     - Plus a conformance script and its `.py` twin.
2. **Launcher and environment allowlist (ahp-host, ahp-host-acp).**
   - `ahp_host.provider.spawn` gains `child_env(allow, extra, *, base=os.environ)`. It keeps only the named variables, which may be glob patterns, plus `extra`, and always drops variables with secret-shaped names unless named exactly. The ahp-host and ahp-host-claude CLIs keep inheriting by default.
   - ahp-host-acp agent options gain two keys:
     - `launcher`: an argv prefix with `{cwd}`, `{uid}` and `{gid}` placeholders, for example `["docker", "run", "-i", "--rm", "-v", "{cwd}:{cwd}", "-w", "{cwd}", "img"]`.
     - `env_allow`: when set, the agent's environment is built by `child_env`; unset keeps today's behaviour, with a CHANGELOG note recommending it.
   - Both keys go in `AGENT_OPTIONS`, `config.py` and `AgentSpec`, kept in step as ahp-host-acp's AGENTS.md requires.
   - **Paths.** The supported mapping is identity: each root is mounted at the same path, and the README says so. A `path_map` (host path to container path, applied to `session/new` cwd, prompt resource links, and the paths of inbound diffs, locations and `rawInput`) is a later option, not part of M14.
   - **Relay in a container.**
     - `relay_command` overrides `sys.executable -m ahp_host_acp.relay`, so the relay can be installed in the image.
     - `relay_host` (for example `host.docker.internal`) makes the bridge listen on that interface instead of loopback, and only when set.
     - The bridge token reaches the relay through the MCP entry's environment, which the agent hands to the relay it starts inside the container, so the launcher needs no `-e NAME`; never argv.
   - ahp-host-claude gets `env_allow` only, through the SDK's `env`. Whether the SDK can be launched in a container is out of scope.
   - Proving tests:
     - `fake_agent.py` gains a scenario that reports its `os.environ` keys and argv: `env_allow` drops an unlisted variable and a secret-shaped one, keeps `PATH`, and the launcher prefix and placeholders reach argv.
     - A launcher that is a small Python wrapper script, standing in for docker, starts a working session.
     - The relay with `relay_command`/`relay_host` round-trips a client tool.
     - The token never appears in argv.
     - No test needs docker; an opt-in one may.
3. **Host-enforced command and path policy (ahp-host, ahp-host-claude, ahp-host-acp).**
   - `ahp_host.provider.policy.ToolPolicy` is pure: `decide(ToolRequest(name, kind, input, cwd, roots)) -> Decision(allow | deny | ask, reason)`. A deny is final in every approval mode, never downgraded by `acceptEdits` or `bypass`. `ask` forces a prompt.
   - Rules come from config as a `[policy]` table:
     - `deny_commands`: shell-word patterns such as `git push*`, `git remote set-url*` and `curl * | sh`. Matching is on the parsed command (`shlex`), so `git  push` and `git -C x push` match; an unknown option is tried with and without an argument, wrappers (`env`, `sudo`, `timeout`, `xargs`, `nice`, ...), `eval`, `sh -c`, shell keywords and `$()` bodies are unwrapped, and a command's words are judged against `deny_paths` and `ask_paths` too.
     - `deny_paths`: globs such as `**/.env`, `**/id_*` and `~/.ssh/**`. Matching happens after `resolve()`, so symlinks and `..` are caught.
     - `ask_paths`: reads, writes and commands naming a matching path that need a prompt.
     - `allow_paths_outside_roots = false`.
     - A callable by import path, for a deployment's own rules.
   - Each provider maps its tools to `ToolRequest`, and its mapping table is the security decision tested:
     - Claude maps `Bash` (the command), `Read`, `Write`, `Edit`, `Glob`, `Grep` and `NotebookEdit` (the path), and MCP tools (by name). It consults the policy first in `_pre_tool_use`, where a deny is binding in every mode.
     - ACP consults it in `_request_permission` before `confirm_tool_call` and answers the reject option.
   - The ACP limit: on its own this is advisory, because an ACP agent in its own allow-always mode never asks.
   - The hard path is `[policy] host_tools = true`: ahp-host-acp advertises the ACP `fs` and `terminal` client capabilities and serves `fs/read_text_file`, `fs/write_text_file` and `terminal/*` itself, through the roots jail and the policy. Reads, writes and commands then run under host control. Pair it with the M14 item 2 sandbox, so an agent that ignores the host's tools still has no credentials.
   - The policy reason reaches the agent: Claude through `PermissionResultDeny(message=)`; ACP through the tool error text.
   - Proving tests:
     - The pure policy: command parsing variants, symlink and `..` escapes, `~` expansion, and Windows paths under `--platform linux` mypy and on the Windows CI.
     - Claude: a denied `git push` is refused under `acceptEdits` and `bypassPermissions`, with the reason, using the fake SDK.
     - ACP: a permission request for a denied path is rejected without a prompt; with `host_tools`, a `fs/read_text_file` of `.env` returns an error, a read inside the roots works, and `terminal/create` of `git push` is refused (`fake_agent.py` gains these client calls).
     - Each mapping table is pinned by a test.

**M14 result.** The three groups landed with no live model and no docker anywhere.

1. **A call in a given folder.** `packages/ahp-host-flow/tests/integration/test_callcwd.py` (in-process: the inner session's folder, the roots check, the worktree base and the named chat), `packages/ahp-host-flow/tests/integration/test_callcwd_remote.py` (fleet and remote) and the conformance script `packages/ahp-host-flow/conformance/extensions/callcwd.js` with its twin `packages/ahp-host-flow/conformance/extensions/callcwd.py`.
2. **Launcher and environment allowlist.** ahp-host `packages/ahp-host/tests/unit/test_childenv.py`; ahp-host-acp `packages/ahp-host-acp/tests/test_acplaunch.py` and `packages/ahp-host-acp/tests/test_acprelayhost.py`; ahp-host-claude `packages/ahp-host-claude/tests/test_claudeenv.py`.
3. **Host-enforced command and path policy.** The pure policy in ahp-host `packages/ahp-host/tests/unit/test_toolpolicy.py`; Claude's mapping and its binding deny in ahp-host-claude `packages/ahp-host-claude/tests/test_claudepolicy.py`; the advisory ACP path in ahp-host-acp `packages/ahp-host-acp/tests/test_acppolicy.py`; and the hard path, `[policy] host_tools = true`, in `packages/ahp-host-acp/tests/test_acphostfs.py`.

Known limits. Path mapping is identity only. The ACP permission path is advisory, and decline feedback sends an agent's reason on the chat's next prompt (M15 item 4). Claude's dropped variables arrive as empty strings, because the SDK merges `os.environ` (`CLAUDECODE` excepted, which the SDK drops). The command policy is best effort over shell text: a command's words, `--opt=value` values and redirection targets are checked as paths, but paths built at run time (variables, `$()` output, an earlier `cd`), globs the shell expands (`cat .en*`) and files a program opens itself are not, and a word that only looks like a denied path is denied. Claude's `Glob` pattern and `Grep` glob are judged as one path each, not expanded. On Windows a terminal's grandchildren are killed only while the command still runs (`taskkill /T`). No test uses docker.

**M15: approvals and changesets.**

1. **A waiting signal from `inputNeeded`.**
   - A step's `awaitingInput` is derived from the session's `inputNeeded`, filtered to the worker chat, by observing the host's session state as `inprocess._unanswerable` already does. So out-of-turn MCP entries, authentication requests and restore withdrawals count.
   - `TurnTee.on_waiting` stays as the fallback when no session view exists.
   - `run_view` gains `RunView.waiting`: each step waiting, with its request kind and, if the protocol entry carries one, the chat. ahp-client adds `Session.waiting()` over `inputNeeded`, with no flow knowledge needed.
   - Proving tests:
     - An MCP authentication entry on a worker chat sets `awaitingInput`.
     - A restore withdrawal clears it.
     - `RunView.waiting` lists two parallel waiting steps.
2. **Approvals for remote workers.**
   - `ClientDispatcher` iterates the remote `TurnStream` instead of awaiting it:
     - `ToolCallReady` is re-raised as a tool confirmation on the thin worker chat (`RemoteOut.confirm`, implemented by `RemoteWorkerChats` with the worker sink), and the answer goes back with `approve()` or `deny(reason_message=)`.
     - `InputRequested` is re-raised as an input request there and answered with `.answer()`.
   - `idle_timeout` stays the backstop, and an open relayed request pauses it.
   - Masking applies to the relayed copies, as the `TurnTee` does.
   - Proving tests, against a second in-memory host (`remote_dialer`): a remote approval answered on the run's worker chat lets the remote turn finish; a denial with a reason reaches the remote agent; a remote question round-trips; an answer given after more than `idle_timeout` still lands, while a silent remote turn still times out.
3. **Bulk and policy approvals.**
   - Changing the run's `Approvals` session config now fans out to every live worker in `_workers`, not only new ones, so "approve all for this run" applies at once.
   - A flow.toml `[approvals]` rule table answers a worker's confirmation in `TurnTee.confirm_tool_call` before it is published, logging "approved by rule <n>":
     - Match keys: tool name glob, `readOnly` hint, provider, phase.
     - Actions: `allow` or `deny`.
     - A `persons` list answers only when the turn's principal is listed (M13 item 1).
   - M14's `ToolPolicy` is checked first and always wins; a rule can never allow what the policy denies. This is a security decision, tested as one and named in AGENTS.md.
   - Proving tests:
     - A config change approves an already-running worker's next prompt.
     - A rule auto-approves a read-only tool and leaves a write to prompt.
     - A rule cannot override a policy deny.
     - A person-scoped rule applies only for that principal.
4. **Commit on a changeset, and reasons back to the agent (ahp-host, ahp-host-flow, ahp-host-acp, ahp-client).**
   - **Operations, widened compatibly.**
     - `OperationHandler` may take a fourth `OperationContext(params, principal)` argument, chosen by signature, with `params` from `InvokeChangesetOperationParams._meta["ahp-py/operation"]`.
     - It may return `{message, followUp}`, which the host passes through in place of `{}`.
     - Existing three-argument handlers keep working.
   - **Commit.**
     - Adds `COMMIT_OPERATION = "ahp-host-flow.worktree.commit"`: commit the worktree on a branch named by `[changesets] branch` (default `flow/<runId>-<ordinal>`), with the message from `params.message`, defaulting to the step label, else the first prompt line (up to 72 characters), else `flow: run <runId> step <ordinal>`, masked by the run's redactor.
     - Optionally (`params.apply = true`) apply onto the working tree and commit there.
     - The author is the git config's; the principal goes in a `Flow-Principal:` trailer only when `[changesets] trailer = true`.
     - It never pushes.
     - It is persisted across a restart like Apply and Discard (M11 item 5).
   - **Discard with a reason.** Discard takes `params.reason`, logs it, and posts a system notification on the worker chat. A named chat (`opts.x.chat`) gets it as the first line of its next turn.
   - **Reasons in the script.** `approve()` keeps returning a bool (Claude Code parity). With `opts.x.detail: true`, it returns `{approved, reason, answeredBy}`.
   - **ACP reasons.** ACP has no reason field, so ahp-host-acp sends a declined call's reason as a short user-visible text block on the agent's next prompt in that session (`[declined: <tool>] <reason>`), behind `decline_feedback = true` (the default).
   - **ahp-client.** `ToolCallReady.deny` and `InputResponder.confirm_tool` gain `reason_message=` and `user_suggestion=`, and `Session.invoke_operation(..., params=)` exists.
   - Proving tests:
     - Commit creates the branch with the message and leaves the repo's checkout untouched; commit-and-apply lands it; a second resolve fails; commit survives a restart.
     - A discard reason reaches a named chat's next turn.
     - The ACP fake receives the reason on its next prompt.
     - The ahp-client helpers send the fields.
     - A three-argument handler still works.

**M15 result.** The four items landed with no live model. The proving tests are `packages/ahp-protocol/tests/unit/test_flow_ext_waiting.py`, `packages/ahp-protocol/tests/unit/test_operation_ext.py`, `packages/ahp-host/tests/integration/test_opcontext.py`, `packages/ahp-host/tests/unit/test_sessionview.py`, `packages/ahp-client/tests/client/test_m15client.py`, `packages/ahp-host-acp/tests/test_acpdecline.py`, `packages/ahp-host-claude/tests/test_claudehints.py`, `packages/ahp-host-flow/tests/integration/test_waitingview.py`, `packages/ahp-host-flow/tests/integration/test_remoteapprove.py`, `packages/ahp-host-flow/tests/integration/test_bulkapprove.py`, `packages/ahp-host-flow/tests/unit/test_approvalrules.py`, `packages/ahp-host-flow/tests/integration/test_approvalrules_hosted.py`, `packages/ahp-host-flow/tests/integration/test_flowpolicy_inner.py`, `packages/ahp-host-flow/tests/integration/test_flowcommit.py`, `packages/ahp-host-flow/tests/unit/test_approvedetail.py`, `packages/ahp-host-flow/tests/integration/test_approvedetail_hosted.py` and the conformance script `packages/ahp-host-flow/conformance/extensions/approve_detail.js`.

Known limits. A relayed remote request waits until the run is aborted unless the provider sets `approval_timeout`, after which an unanswered one fails the call (naming the provider and the setting) and stops the remote turn. A `read_only` rule ignores an agent-supplied hint (ACP's `kind`) unless it sets `trust_agent_hints = true`. Remote relays are never rule-answered (`[approvals]` rules apply to in-process workers only). A flow `[policy]` judges this host's paths. It is enforced in an in-process provider's own policy hook only when the provider's factory takes a named `policy` keyword (the Claude and ACP `from_config` do; the loader folds it into the provider table's `policy`, and a provider configured by a file path combines it with the file's `[policy]` in its `from_config`); elsewhere it is consulted only when a worker asks, so an approve-all stops it. Fleet and remote workers never get it: they run under their own host's policy. A discard reason reaches a named chat only when the handler runs while the run is live, because the host refuses operations during an active turn. The Claude Code shim's `approve()` stays a bool.

**M16: limits, timeouts and fallback.**

1. **Error kinds reach the engine.**
   - `TurnTee.turn_failed` keeps `error_type` and `resumable`.
   - Dispatchers raise `DispatchError(kind, transient, message)`, where `kind` is one of `unavailable`, `rate_limited`, `refused`, `timeout`, `empty` or `failed`. The table maps `claude.*` (the transient statuses already there), `acp.start`/`acp.agentExited` (unavailable), `acp.refusal`, `acp.max_tokens`, a provider load failure and a remote `TurnFailed`.
   - The scheduler records `kind` on the `error` record; old readers ignore it.
   - Proving test: the mapping table, one case per row.
2. **Budgets and a hard cap.**
   - `Budget` becomes named counters, each with an optional limit:
     - `output_tokens`, which is `budget` and unchanged by default;
     - `input_tokens`, including cache reads unless `[limits] count_cache = false`;
     - `cost_usd`, where a provider reports it;
     - `wall_seconds`.
   - Usage is read properly: Claude's `_meta.turnTotals` (the summed figures, not the last request's gauge) and ACP's cache tokens.
   - **Hard cap, two layers.**
     1. At admission, each call reserves `[limits] reserve` tokens (default: the mean of finished calls, else 0), released or corrected when it ends. So N parallel calls cannot each pass a check that only one fits.
     2. A live stop: when a counter passes its limit, the scheduler cancels every in-flight call through the existing skip path, which stops the inner turn, the remote turn and the fleet session. Those calls are recorded as `error` with kind `budget`, not `null`, so a resume re-runs them rather than replaying a null.
   - Because providers report usage only at turn end, the live stop is driven by wall time and by Claude's per-`AssistantMessage` usage. ahp-host-claude emits `sink.usage` per assistant message, behind a capability flag, so the overshoot is bounded by one model request rather than one turn. The README states the bound for each provider.
   - Per-call `opts.x.timeout` and `ProviderConfig.call_timeout`.
   - flow.toml `[limits]`: `budget`, `input_budget`, `cost_budget`, `timeout`, `concurrency`, `reserve`. These finally give the provider path what `--budget`, `--timeout` and `--concurrency` give the CLI.
   - **Per-user quotas.** A provider-wide `QuotaLedger` keyed by principal (M13 item 1), with per-day and per-run token and cost limits in `[quotas]`, persisted under `state_dir`, and checked in `Scheduler.admit` and on each usage report. Hitting it fails the call like the budget does, with a run log line naming the quota, never the principal's other runs. ahp-gateway gains a per-principal `max_running` beside the per-session one.
   - Proving tests:
     - Eight parallel calls under a budget that fits two overshoot by at most the reservation.
     - The live stop cancels in-flight fakes and records `budget` errors that a resume re-runs.
     - A wall-clock limit stops a slow call.
     - Input and cost counters.
     - Claude per-message usage with the fake SDK.
     - The quota survives a provider restart and is per principal.
     - The gateway's per-principal cap holds across two flow sessions.
3. **Idle timeout, heartbeat, empty-turn retry and single-slot models.**
   - `idle_timeout` is allowed for every provider kind. A watchdog fed by a `TurnTee.on_activity` hook covers every delta, including reasoning, tool updates and usage, so thinking output is the heartbeat. An open approval pauses the watchdog. It cancels the turn and fails the call with kind `timeout`.
   - ahp-client's `TurnStream` also counts `activityChanged` and session-level `inputNeeded` changes for its turn's chat, and documents what resets it. The README's live-test advice (`idle_timeout=None`) is replaced by: a flow run's main chat emits a heartbeat (a `_meta` progress tick on `activityChanged`) every `[limits] heartbeat` seconds (default 30) while steps run.
   - `ProviderConfig.retry_empty` (default 0): a turn whose final text is empty or whitespace-only, with no structured result, is retried inside the dispatcher up to N times. The ordinal and key are unchanged, the journal holds one record, and the usage of every attempt is summed. Reasoning-only output counts as empty. Under the default, Claude Code semantics are unchanged.
   - `ProviderConfig.concurrency` is a semaphore per provider, shared across a provider's runs and held inside the run's own slot. `concurrency = "probe"` times two parallel minimal turns at first use. If the second turn's time to first event is about as long as the first turn takes, the semaphore is set to 1 with a log line. A 429 or 503 marked busy also halves it, down to 1. Explicit numbers win.
   - Proving tests:
     - A worker that only streams reasoning is not timed out; a silent one is.
     - An approval pauses the watchdog.
     - Empty then text succeeds on retry with one journal record and summed usage.
     - `concurrency = 1` serialises calls across two runs.
     - The probe detects a fake single-slot provider and leaves a parallel one alone.
     - The client heartbeat keeps a default-`idle_timeout` client attached through a 10-minute fake step under a virtual clock.
4. **Fallback chain.**
   - `ProviderConfig.fallback = ["claude"]`, and a rule-level `fallback`, validated at config load: known providers, no cycles.
   - `RoutingDispatcher` walks the chain on `unavailable`, `rate_limited`, a transient failure, `timeout` and `empty` after retries. It never walks on `refused`, a schema failure, or `budget`. It logs "fell back from a to b (kind)".
   - The provider actually used goes in the `started` record and the step descriptor. The key stays on the primary route, so `key = "model"` is still decided before dispatch, and a resume replays the fallback's result unless routing changes.
   - Named chats and `attach` never fall back to a fleet or remote provider. Such a fallback is skipped with a line.
   - Proving tests:
     - A local fake that is unavailable falls back to a second fake.
     - A refusal does not fall back.
     - A cycle is a config error.
     - Pinned key hashes stay byte-identical under the default `[resume]`.
     - The step descriptor names the used provider.

**M16 result.** The four items landed with no live model. Shipped: error kinds on every dispatcher and journal error records; named budget counters (output, input, cost, wall, cache reads) with admission reservations (`reserve` now defaults to the mean output tokens of the run's finished calls) and a live stop on usage reports and wall time, recorded as kind `budget` and re-run on resume; per-request usage in ahp-host-claude behind a flag; `[limits]`, per-call timeouts and `call_timeout`; per-user quotas and the gateway's per-principal `max_running`; the idle watchdog, `retry_empty` and the heartbeat; per-provider `concurrency` with `probe`; and the fallback chain. The proving tests are `packages/ahp-host-flow/tests/unit/test_errkind_table.py`, `packages/ahp-host-flow/tests/unit/test_errkind_journal.py`, `packages/ahp-host-flow/tests/integration/test_errkind_dispatch.py`, `packages/ahp-host-flow/tests/unit/test_budgetctr.py`, `packages/ahp-host-flow/tests/unit/test_livestop_reserve.py`, `packages/ahp-host-flow/tests/unit/test_livestop_engine.py`, `packages/ahp-host-flow/tests/unit/test_reservemean_default.py`, `packages/ahp-host-claude/tests/test_claudereq_usage.py`, `packages/ahp-host-flow/tests/integration/test_calltimeout.py`, `packages/ahp-host-flow/tests/unit/test_quota_ledger.py`, `packages/ahp-gateway/tests/test_quota_gateway.py`, `packages/ahp-host-flow/tests/integration/test_idlewatch.py`, `packages/ahp-host-flow/tests/integration/test_retryempty.py`, `packages/ahp-client/tests/client/test_heartbeat_turnstream.py`, `packages/ahp-host-flow/tests/unit/test_provslots.py` and `packages/ahp-host-flow/tests/integration/test_fallchain_dispatch.py`; the review fixes added `packages/ahp-host-flow/tests/integration/test_fleet_stopcause.py`, `packages/ahp-host-flow/tests/integration/test_fleet_idlewatch.py`, `packages/ahp-host-flow/tests/integration/test_provslots_probe_hosted.py`, `packages/ahp-host-flow/tests/unit/test_attempt_usage.py` and `packages/ahp-host-flow/tests/unit/test_claudereq_budget.py`. Known limits: replayed calls add only output tokens to the counters; input/cost/wall budgets, reserve and count_cache are not journaled; Claude's 429 is recognised from error text; the anonymous quota bucket is shared by every run without a principal; fleet idle_timeout sees status changes and the gateway's `activity` fingerprint only, never deltas (status changes alone from a gateway that reports no `activity`); ACP cachedReadTokens is assumed to be separate from inputTokens; an ACP turn without `inputTokens` reports its context size as a gauge (`acpUsage.inputGauge`), which counts no input; ACP's `acpUsage.cost` is read as the session's running total and counted per call as a delta.

**M17: sandboxed workflow hosts, client tools in workers, durable workers.**

1. **Client tools in worker chats.**
   - In-process workers get the session's active clients and client tools in their inner `AgentSessionContext`. `FlowSession.active_clients_changed` is forwarded to every live worker that `FollowsActiveClients`. The worker sink's `run_client_tool` already routes, since active clients are per session.
   - `[workflows] client_tools = true | false | ["name", ...]` (default `true`) lets a deployment narrow them.
   - Remote workers get a `ClientToolHost` advertising the same tools. Its executors call back through a callable injected from the provider layer (the engine never imports `ahp_host`), which runs the tool on the worker chat. Fleet workers are unchanged: their node session's own clients apply.
   - Proving tests:
     - A worker turn calls a main-session client tool and gets its answer.
     - A client joining mid-run reaches a running worker.
     - `client_tools = false` hides them.
     - A remote worker's tool call round-trips through the run's client.
2. **Per-call sandboxed hosts.**
   - `ProviderConfig.launch = "pkg.mod:factory"`, for `via = "remote"`: an async factory returning `(transport, aclose)`. With `per_call = true`, `ClientDispatcher` launches, dials, runs and tears down a host for each `agent()` call, and on every failure path.
   - It takes the call's folder (M14 item 1) and the client tools (item 1 above).
   - Reattach for such a call is "run again", logged.
   - The README gets a "Running on sandboxed hosts" section:
     - a launcher example that starts `ahp-host` in a container with `--bind` and a token file, using placeholder names;
     - what crosses the boundary (prompt, folder, client tools, approvals per M15 item 2);
     - what does not (worktrees, named chats, `attach`);
     - a reminder to set `env_allow`.
   - Proving tests:
     - A fake launcher starting an in-memory host per call runs a fan-out and is torn down after each call, including on skip, abort and failure, with no leaked tasks.
     - The README example parses.
3. **Worker chats that resume after a restart.**
   - The named-chat map is persisted in the flow session's resume state as `{name: {provider, worker_chat, title, config, inner_state}}`, with `inner_state` from the inner provider's `resume_state_of`, refreshed after each turn. It is an optional key, so `RESUME_STATE_VERSION` stays 1.
   - On resume, a provider that is a `ResumableAgentProvider` rebuilds the named chat lazily through `resume_session` on the old worker chat, keeping Claude's memory by session id and ACP's by `session/load`; others are recreated, as today, with the line.
   - A cut-off unnamed call whose inner provider `ResumesTurns` is resumed in place rather than re-dispatched, when its inner state was recorded at start. Otherwise it is re-dispatched as today.
   - Proving tests, over a `FileSessionStore` with drain and crash: a named chat on a resumable fake remembers an earlier turn after the restart; a non-resumable one is recreated with the line; a cut-off call on a `ResumesTurns` fake is resumed once, not re-dispatched.
4. **Secrets out of the run log.**
   - The journal header's `args` are masked with `mask_value`. Its `source`, and each `workflow` record's `source`, are masked with `mask`.
   - Since resume needs the real text, the header gains `sourceSha` (and `argsSha`). A masked journal resumes only if the current script and args hash to those values, with a clear error otherwise.
   - `[journal] keep_source = true` keeps today's behaviour.
   - The run's `Redactor` is offered to ahp-host's and ahp-client's `WireLog` as an optional content masker.
   - `log()` and run errors stay masked on every path: library, CLI and provider.
   - Update the M9 "still not redacted" list.
   - Proving tests:
     - A secret in args and in the script text appears in neither the journal nor the wire log.
     - A masked journal resumes against the same script and refuses a different one.
     - `keep_source` keeps the text.

**M17 result.** The four items landed with no live model. Client tools: in-process workers get the session's client tools (`clienttools.ClientTools`, filtered by `[workflows] client_tools`) and follow active-client changes; remote workers advertise them through a `ClientToolHost` whose calls run on the worker chat. Per-call hosts: `launch` and `per_call` start a host per call (with the call's folder and the client tools) or one per run, torn down on every path, a cut-off call running again; ahp-host-acp gained an off-loopback `--bind` with a required token file, which the README's container launcher uses (a published port on `127.0.0.1`, no `--network host`). Durable workers: `DurableWorkers` keeps named chats and in-flight worker turns in the resume state; a named chat resumes its inner session after a restart (or is recreated, with the cause logged), a cut-off unnamed call on a `ResumesTurns` session is resumed in place, an in-process call's handle names its worker chat in `DispatchHandle.worker_chat`, and only a host shutdown leaves a turn in flight. Secrets: the journal stores the script, its args and child workflow sources masked with their sha256 (`[journal] keep_source`, `flow run --keep-source` keep them); a resume gets the real ones back and checks them (the provider re-resolves the workflow and re-parses the turn's stored message through ahp-host's new `ObservesChatState`; the CLI takes the path and `--args`; the library `RunOptions.resume_source`/`resume_args`), refusing a changed script or args with a line saying which; the sandbox masks `log()` lines and script errors for every caller; ahp-host's and ahp-client's `WireLog` take a content masker (`mask=`, `Host(wire_log_mask=)`), and the flow host passes its `Redactor` (`--wire-log` on `python -m ahp_host_flow` and `flow run --config`). The proving tests are `packages/ahp-host-flow/tests/integration/test_workerctool.py`, `packages/ahp-host-flow/tests/integration/test_remotectool.py`, `packages/ahp-host-flow/tests/unit/test_ctoolcfg.py`, `packages/ahp-host-flow/tests/integration/test_percall.py`, `packages/ahp-host-flow/tests/unit/test_percall_engine.py`, `packages/ahp-host-flow/tests/unit/test_launchcfg.py`, `packages/ahp-host-flow/tests/docs/test_sandboxhost_readme.py`, `packages/ahp-host-acp/tests/test_acpbind.py`, `packages/ahp-host-flow/tests/integration/test_durnamed.py`, `packages/ahp-host-flow/tests/unit/test_durnamed_state.py`, `packages/ahp-host-flow/tests/integration/test_durinflight.py`, `packages/ahp-host-flow/tests/unit/test_handle_kinds.py`, `packages/ahp-host-flow/tests/unit/test_maskjournal.py`, `packages/ahp-host-flow/tests/integration/test_maskresume.py`, `packages/ahp-host-flow/tests/integration/test_maskwire.py`, `packages/ahp-host-flow/tests/unit/test_cli_mask.py`, `packages/ahp-host-flow/tests/unit/test_journalcfg.py`, `packages/ahp-host/tests/unit/test_wirelog.py`, `packages/ahp-host/tests/unit/test_sessionview.py` and `packages/ahp-client/tests/client/test_wirelog_mask.py`. Known limits: the main chat's message still holds a script's or its args' literal secrets (stored session, wire to clients); a resume of a masked run whose start message was redacted or edited in the store is refused rather than run; a shared (non-`per_call`) launch gets no folder or client tools; client tool inputs and results are not masked.

**M18: a reference non-Claude agent, the support matrix, releases.**

1. **A reference agent on an OpenAI-compatible endpoint.**
   - `ahp_host_acp.reference` is a small, standard-library-only ACP agent (`python -m ahp_host_acp.reference`, console script `ahp-acp-openai`). It runs a `/v1/chat/completions` tool loop: base URL, model and key from environment variables (the key never in argv or logs).
   - It maps the relay's MCP tools to OpenAI `tools` and returns structured output through the relay's `StructuredOutput` tool. It streams text and reasoning (`reasoning_content`, where the server sends it), honours `session/cancel`, and asks `session/request_permission` before its one built-in tool, a file write.
   - It is an ACP agent rather than a host provider, so permissions, relay, structured output and every M14 policy path apply to it unchanged.
   - It is tested against an in-test stub HTTP server that speaks the chat completions SSE format. A live lane (`AHP_OPENAI_BASE_URL`) runs it against Ollama or any compatible server, in CI too if a deployment provides one.
   - Proving tests:
     - Text, a tool round trip through the relay, structured output, cancel mid-stream, a permission denial with its reason (M15 item 4), and a 429 classified `rate_limited` (M16 item 1).
     - A flow run routes a phase to it.
2. **The support matrix.**
   - A README table (ahp-py root) lists providers (echo, ACP generic, ACP reference, Claude, flow) by capability: steering, structured output (native or prompted), client tools, host policy (advisory or hard), turn resume, session resume, fork, history truncation, changesets, usage (live or at turn end), cost and reasoning stream.
   - It also lists the ACP agents known to work (opencode, OpenClaw, the reference agent), with what each supports in practice.
   - A test builds the capability columns from `isinstance` checks on each provider's session class and fails when the table disagrees. The "in practice" columns are prose, with a date.
3. **Tagged releases and a compatibility matrix.**
   - Pin ahp-host-flow's siblings (`ahp-client`, `ahp-host-claude`, `ahp-host-acp`, and `ahp-gateway` for tests) to `~=0.1.0`, add it to `[tool.uv.sources]`, and add `ahp_host_flow` to the CI wheel smoke import.
   - Each package without one gets `RELEASING.md`, `smoke_wheel.py` and a dated release section moved out of `[Unreleased]`.
   - Fix release-plan.md's stale PyPI line and sequencing.
   - A root `COMPATIBILITY.md` has one row per tag: package, version, `ahp-protocol` range, the AHP versions it offers (`DEFAULT_SUPPORTED_VERSIONS`), the upstream spec commit, and the tested sibling versions. A root docs test reads each `pyproject.toml` and `versions.py` and fails on drift.
   - Release in dependency order: `ahp-protocol/v0.1.0`, `ahp-host/v0.1.0`, `ahp-client/v0.1.0`, then gateway and the providers. Each tag goes through `release.yml`.
   - Tagging and pushing tags is done by the maintainer, not by a build run.
   - Proving tests: the compatibility test; `release.yml`'s version check against each wheel (it exists); and the wheel smoke importing every package.

**Not in M13-M18.**
- A container path map for ACP: identity mounts only.
- Launching Claude's SDK in a container.
- Upstream requests for a decline reason in ACP, a principal on `UserMessage`, and typed workflow starts, which are added to §10 as each lands.

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
- **Ordinal determinism:** ordinals are taken by the host (`Scheduler.issue`)
  when an `agent` request line arrives, in line order, not at completion, but a
  later pipeline stage issues in completion order, and with two runners (a
  cross-language child) arrival order decides. Resume
  matches by key, not ordinal, so this does not cost cache hits.
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
3. **Restart semantics:** what a host does with a turn that was running and
   the open input requests, and how a client learns what was withdrawn (§4.8; `ahp-host` `docs/requests.md` entry 11).
4. **Redaction:** `chat/partRedacted` (and a message variant), with reducer
   semantics and the rule that only the shared view is redacted (§4.9)
   (`ahp-host` `docs/requests.md` entry 13).
5. **Delegated sessions:** a `SessionOrigin` kind,
   `{kind: "delegation", session, chat, toolCallId}`, so a session started on
   another host's behalf can point back to the chat that started it (§2;
   `ahp-host` `docs/requests.md` entry 12). Built as the interim kind
   `ahp-py/delegation` in `createSession` `_meta["ahp-py/origin"]` (M11); the
   upstream kind is still the ask.
6. **Principal on messages and answers:** who sent a message and who answered an input
   or confirmation request, as an optional, host-supplied identity (§8 M13; `ahp-host`
   `docs/requests.md` entry 14).
7. **Typed workflow starts:** a `_meta["ahp-py/flow"]` start (`{workflow, args, runId?}`) on an
   ordinary `chat/turnStarted`, and `argsSchema` on a workflow's prompt customization (§8 M13).

