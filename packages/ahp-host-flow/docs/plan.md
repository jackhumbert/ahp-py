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
built from AHP parts. The protocol does not change.

```
 AHP clients (VS Code, iOS, ahp-client, `flow` CLI)
        │  AHP
        ▼
 ahp-host ── provider: ahp-host-flow ───────────────────────────────┐
             one AgentSession per run                                │
             ├ main chat: the run (phases, log, result)              │
             ├ child chat per agent() call  (ChatOrigin kind "tool") │
             │    └ driven by an inner provider session (in-process) │
             │      ClaudeProvider │ AcpProvider (opencode, …)       │
             └ or mirrored from a remote session (ahp-client) ───────┼──▶ other hosts / ahp-gateway nodes
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
gives two ways to dispatch:

- **In-process (primary).** `ahp-host-flow` hosts the inner providers itself.
  For each `agent()` call it opens a worker chat
  (`publisher.open_tool_chat`). It creates an inner `AgentSession` on the
  chosen provider and runs the turn with that worker chat's `TurnSink` as the
  inner session's sink. The child chat is then the real transcript, not a copy,
  and approvals, questions and file edits raised in it reach any client. No
  stray top-level sessions appear anywhere.
- **Remote (secondary).** For agents on another machine (a gateway node), the
  engine drives a remote session through `ahp-client` and mirrors its events
  into the worker chat. The remote session does show in that node's catalog.
  Proposal for `requests.md`: a `SessionOrigin` kind for delegated sessions
  (`{kind: "delegation", session, chat, toolCallId}`), so the remote session
  can point back. This is not needed for v1.

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
| `effort` | session `config.effort` where the provider has it; otherwise ignored, with one `log()` |
| `isolation:'worktree'` | `git worktree add` under the run dir, used as the inner session's working directory; removed if unchanged, otherwise kept and its changeset published on the child chat |
| `agentType` | an agent from the inner session's customizations; unknown means an error, as in Claude |
| `schema` | see below |
| approvals | per-profile policy: auto-approve what the profile allows, and raise the rest in the child chat for a human |
| skip / abort | cancelling the child chat's turn makes that `agent()` return `null`; cancelling the main turn aborts the run |
| errors | a resumable error uses `turnResume` once, then the call returns `null` with a `log()` line |

The prompt gets a short preamble equivalent to Claude Code's subagent
framing: "your final message is the return value; return raw data". This
matters most for non-Claude agents.

**Structured output.** AHP turns carry no output schema, so there are three
tiers:
1. **Client tool.** The engine offers `StructuredOutput` with the schema as its
   input schema. Arguments are validated with `jsonschema`, and a mismatch is
   returned as a tool error so the agent retries. `ahp-host-claude` already
   serves client tools (`client_tools.py`).
2. **MCP.** `ahp-host-acp` has no client-tool path but does pass MCP servers
   through. The engine serves `StructuredOutput` on a loopback HTTP MCP
   endpoint and adds it to the inner session's `mcp_servers`. If the agent
   only takes stdio, a tiny stdio shim forwards to that endpoint.
3. **Text.** The schema goes in the prompt and the engine parses the last JSON
   block. A bounded number of "fix your JSON" follow-ups are sent in the same
   chat.

## 5. Routing and the first ACP target

The first ACP target is **opencode on Ollama Cloud's GLM 5.3 Flash**
(`ollama/glm-5.3-flash:cloud`), which `ahp-host-acp`'s README already
documents. Config is per user, with an optional per-project overlay under
`"flow"` in `<cwd>/.claude/workflow.json`:

```toml
# ~/.config/ahp/flow.toml
state_dir = "~/.local/state/ahp-host-flow"
node = "node"                                  # or an absolute path

[providers.claude]
load = "ahp_host_claude:ClaudeProvider"
models = { haiku = "claude-haiku-4-5-20251001", sonnet = "claude-sonnet-5-5", opus = "claude-opus-5-5" }
structured = "client-tool"

[providers.glm]
load = "ahp_host_acp:AcpProvider"
config = "~/.config/ahp/opencode.toml"         # command = ["opencode", "acp"], OPENCODE_CONFIG, …
models = { glm = "ollama/glm-5.3-flash:cloud" }
structured = "mcp"

[providers.remote-mac]                          # ClientDispatcher
url = "wss://gateway.example.com"; node = "my-mac-mini"; provider = "claude"

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
something besides this provider uses the engine. The likely candidate is the
gateway's orchestrator running workflows. Until then, an `import-linter`
contract (engine must not import `ahp_host`) keeps the split a move rather
than a rewrite.

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
| M3 | `ahp-host-flow` + in-process Claude: workspace/user/host workflow sources as customizations, child chats, model/effort, StructuredOutput client tool, worktrees, skip/abort, approval relay | fan-out and triage corpus scripts pass against Claude; a run is watched and controlled from VS Code or iOS |
| M4 | In-process ACP (opencode/GLM): MCP StructuredOutput, text fallback, routing | a script routes its Review phase to GLM and gets a schema-valid result |
| M5 | Full corpus unmodified; client-published workflows (+ automation capture); restart resume (`ResumableAgentProvider`); per-child changesets; automations | the plan/build/review corpus script lands a change; a cron automation runs a workflow |
| M6 | `ClientDispatcher` remote + gateway; the `SessionOrigin` request upstream | one call runs on a gateway node and is mirrored into its child chat |
| M7 | Extensions (§6) + shim | `ask()` round-trips to a phone; a dual-target script runs in both runtimes |

**M0 spike result.** `tests/integration/test_spike_worker_chat.py` showed:

1. An inner `EchoProvider` session runs its turn through `ProviderChat.run_turn` on a worker chat from `publisher.open_tool_chat`. The worker turn's message has origin `agent`, the chat's origin is `{kind: "tool", chat, toolCallId}`, and the spawning tool call's result carries the `subagent` link (written by the test's own outer session, so this shows the wire shape, not provider behaviour). So the in-process design of §2 holds and the mirror design is not needed.
2. `run_turn` returns when the turn starts, so the dispatcher waits on its own completion signal. A text tee on the inner sink yields `agent()`'s return text.
3. The inner context's publisher is scoped by a wrapper. By design, chat-scoped calls (background work, changesets, canvases, terminals, nested tool chats) default to the worker chat. Only `background_work_set` and one dropped session-wide call are asserted, and EchoProvider never calls its publisher, so M3 must test each scoped method with a provider that uses it. Session-wide calls (title, config, customizations, MCP state, activity, progress) are dropped, so an inner session cannot rename or reconfigure the run.
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
