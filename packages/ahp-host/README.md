# agent-host-server

A Python **host/server** library for the [Agent Host Protocol][ahp] (AHP) —
Microsoft's protocol for synchronized multi-client state over AI agent sessions.

> **"AHP" here means the Agent Host Protocol, not the Analytic Hierarchy
> Process.** The PyPI names `ahp` and `pyahp` belong to packages for the latter,
> a decision-making method with no relationship to this project — which is why
> this one is spelled out. If you want pairwise-comparison matrices and
> consistency ratios, you want one of those instead.

> ### ⚠️ Status: working, but pre-alpha. Not published.
>
> A real client can connect, create a session, and run a turn. **All seven
> reducers pass upstream's whole 247-fixture corpus**, the v0.1 command set plus
> `fetchTurns` is implemented, and a WebSocket transport is in place. Not on
> PyPI, API not stable, single-trust-domain only.
> [`docs/roadmap.md`](docs/roadmap.md) scopes everything that remains;
> [`docs/decisions/`](docs/decisions/) records the decisions taken.

## Try it

```bash
pip install -e '.[ws]'
python -m agent_host_server
```

That serves the offline echo provider on loopback and prints the VS Code
settings to paste — add them to `settings.json`, then open the **Agent Sessions**
view and pick **Echo**:

```json
{
  "chat.remoteAgentHostsEnabled": true,
  "chat.remoteAgentHosts": [
    { "address": "127.0.0.1:4321", "name": "Echo" }
  ]
}
```

Add `"connectionToken": "…"` to the entry if you started the host with
`--token`. `address` and `name` are both required, and VS Code silently drops an
entry that is missing either. The address is scheme-less on purpose — VS Code's
transport prepends `ws://`, and only `wss://` is preserved.

Connecting a third-party host is a supported, extension-free VS Code feature
(1.131+). Verified working against **VS Code Stable 1.131.0**: handshake,
session creation, and a full turn. The details of what it sends — and the three
host bugs that finding out uncovered — are in
[`docs/experiments.md`](docs/experiments.md) §E12.

### Demo flags

The bare command is deliberately minimal. Each flag turns on one surface, and
the ones that execute or destroy are separate from the ones that only read:

| Flag | What it turns on |
|---|---|
| `--customizations` | Publish a demo plugin tree: agents, skills, prompts, rules, hooks, an MCP server |
| `--configurable` | Both config schemas — the session one, and `RootState.config` so the client's own pushes stop being dropped |
| `--elicit` | The agent stops mid-turn and asks a question (ADR 0005) |
| `--confirm-tools` | The agent asks before running its tool, and honours edits to the input |
| `--client-tools` | The agent delegates to a tool the **client** owns |
| `--multi-chat` | Advertise `capabilities.multipleChats` — chat tabs, fork, and side chats |
| `--serve-directory PATH` | Expose `PATH` over the `resource*` commands, jailed to that root |
| `--writable` | **Also allow writes** under `--serve-directory`. A second opt-in, on purpose |
| `--terminal` | Install a real pty backend. **This runs commands** |
| `--changes` | The agent makes real git edits in `--serve-directory` and publishes changesets with working stage/commit/revert |
| `--token [VALUE]` | Require a connection token on the upgrade; omit the value to generate one |
| `--allow-remote` | Bind off-loopback. Read the security section first |
| `--wire-log PATH` | Append every frame as ahp-inspector JSONL |
| `--sequence-file PATH` | Persist `serverSeq` so it keeps increasing across a restart |
| `--port PORT` | Listen here instead of 4321 |
| `--bind ADDR` | Bind address. Loopback unless `--allow-remote` |
| `--delay SECONDS` | Pause between echo deltas, so streaming is visible |
| `--agent-name NAME` | `AgentInfo.displayName` — how clients label the agent |
| `--model-name NAME` | The model name shown in the client's model picker |
| `-v`, `--verbose` | Debug logging |

`--elicit`, `--confirm-tools` and `--client-tools` are mutually exclusive: the
demo provider takes the first one enabled and returns.

A full exploration, against a scratch git repo you do not mind being edited:

```bash
python -m agent_host_server --customizations --configurable --multi-chat   --serve-directory /tmp/scratch --writable --terminal --changes
```

To see the wire:

```bash
python -m agent_host_server --token --wire-log /tmp/agent-host-demo.jsonl
```

which writes [ahp-inspector](https://github.com/roblourens/ahp-inspector) JSONL —
`npx ahp-inspector` picks up an `agent-host-*.jsonl` file with no arguments.

## What this is for

AHP turns "an agent session" from a thing trapped inside one application into a
shared resource several clients can attach to at once — an editor, a browser
tab, a phone, a CLI — all seeing the same live session, any of them able to
answer a tool-approval prompt. The protocol handles ordering, reconnection and
conflict resolution so each front-end doesn't reinvent a worse version of it.

Microsoft publishes AHP **clients** for Rust, TypeScript, Kotlin, Swift and Go.
It publishes **no host library in any language** — the only first-party host is
embedded in the VS Code source tree. Upstream's own
`docs/guide/hosts.md` is a seven-line redirect stub, and its list of server
implementations has exactly one entry.

Python is where a large share of agent infrastructure already lives — harnesses,
orchestrators, eval platforms, internal agent services. Those systems hand-roll a
bespoke SSE or WebSocket stream per front-end today. A conformant Python host
lets them expose a standard protocol instead, and be driven by editor clients
that already speak it.

## Scope

This is a **coordination and state-synchronization layer**. It is not an agent.
Model calls, tool loops, context management and prompting live behind a
pluggable provider interface. Upstream's doctrine is explicit that the protocol
must not enshrine an agent loop, tool schema, model provider or storage backend,
and this project holds that line: the core is vendor-neutral and fully testable
with no adapter installed.

### Implemented

Protocol **0.7.0 and 0.6.0** on the wire · root, session, chat and annotations
channels · **all seven reducers**, gated on upstream's whole 247-fixture corpus
· host-global sequencing with per-channel replay budgets · a pluggable agent
provider with an offline echo implementation · WebSocket transport behind a
transport abstraction.

All 29 commands, and none of them a stub:

`initialize` · `ping` · `subscribe` · `unsubscribe` · `reconnect` ·
`listSessions` (paginated) · `createSession` (with `fork`) · `disposeSession` ·
`dispatchAction` · `fetchTurns` · `createChat` · `disposeChat` ·
`resolveSessionConfig` · `sessionConfigCompletions` · `completions` ·
`authenticate` · `createTerminal` · `disposeTerminal` · `createResourceWatch` ·
`invokeChangesetOperation` · `resourceResolve` · `resourceRead` ·
`resourceList` · `resourceRequest` · `resourceWrite` · `resourceMkdir` ·
`resourceDelete` · `resourceMove` · `resourceCopy`

A provider can also **stop and wait for a human** — elicitation, tool-call
confirmation, and handing a tool to a client to execute — all on one primitive
([ADR 0005](docs/decisions/0005-suspending-provider-requests.md)). That last one
is worth calling out: the host marks a tool call `contributor: {kind: "client"}`,
the client runs it in its own process, and the client reports the result. **The
agent gets the editor's own tools with no filesystem API on the host at all.**

Try any of them against the demo host:

```bash
python -m agent_host_server --elicit
```

`--confirm-tools` asks before running a tool and honours a client's edits to the
input; `--client-tools` delegates the work to a tool the client owns;
`--configurable` publishes a session config schema.

**Params are validated where the schema is specific, and the refusal is the
schema's code.** A `protocolVersions` array with a non-string entry, a
`listSessions` `limit` that is a JSON boolean or a string, a chat `source` with
no `turnId`, a `SideChatSelection` whose `text` is empty: each is `-32602`,
rather than a Python exception rendered as `-32603`, or a field quietly ignored
so that it means the opposite of what was asked. `limit` is typed `number`, so
`3.0` is honoured and `true` is not. The `-32005` refusal carries
`supportedVersions` — the name `errors.schema.json` declares, and the one frame
whose whole job is telling a user which host version to install.

**`reconnect` de-duplicates its subscription list and registers a connection
only for the channels it actually resumes.** The list is peer-supplied and the
schema does not forbid repeats: N copies of one URI returned N copies of every
missed envelope, each carrying the same `serverSeq`, so a conformant mirror
folded every delta N times and a 50 KB request was answered with 62 MB. And a
channel the reply reports in `missing` is no longer left subscribed — the host
was saying "drop this" while continuing to deliver it, and because session and
chat URIs are *client-chosen*, the URI it disowned can be created later by
somebody else. A telemetry channel is the one exception, carved out explicitly:
it is stateless but live, and `reconnect` returns no `telemetry` map for a
client to re-read.

**A `chat/turnCancelled` must name the turn that is running**, not merely arrive
while one is. The reducer no-ops unless `turnId` matches, so accepting a
mismatch aborted the provider while the host's own state went on claiming a turn
was in flight — and nothing could then clear an `activeTurn` whose id nobody
knew. **`disposeChat` cancels that chat's turn before dropping the channel**,
which is also what retracts any confirmation it was parked on; otherwise the
session stayed at `InputNeeded` holding a request that could only be answered on
a channel that no longer existed.

**Configuration** is `resolveSessionConfig` / `sessionConfigCompletions` plus
`RootState.config`, and it comes with its gate rather than after it. A host
publishes no schema by default, and **the schema is the gate**: an unknown key,
a read-only property or a wrong-typed value is refused whatever policy the
embedder supplied. That matters because `root/configChanged` is
client-dispatchable, VS Code sends it about ten times per connect, and a
permissive policy is the norm for a loopback host.

**Terminals** run real commands on a POSIX pty behind `--terminal`, with shell
integration parsed (never injected), process-group teardown on hangup, and
`terminal/exited` announced so a client can close the tab. A `!command` turn is
a one-shot: its child dies when the turn is cancelled and again when the host
stops, because a shell that outlives what asked for it is nobody's idea of a
closed terminal. `terminal/input` and `terminal/claimed` are refused from a peer
that does not hold the claim; **`disposeTerminal` deliberately is not** — a
session-claimed terminal is held by no client, so gating disposal on the claim
would make every handed-over terminal unkillable — and it is gated where the
other commands are, by `Policy`. **Resource watches** poll and coalesce.

**Changesets** are driven from a real git working tree: two changesets
(`uncommitted` and `session`), per-file review flags, and operations wired to
`git add`, `git commit` and a scoped revert. Four rules the schema states and
a host is on the hook for:

- **An operation is invocable only while the changeset declares it.** A
  registered handler is not an invitation — the demo drops Commit from the list
  when nothing is staged — and the check comes first, so an undeclared id is
  refused rather than reaching a handler with its scope and target unchecked.
  `target` is validated against the declared `scopes`, and a `range` target
  without a `range` is `-32602` rather than whatever the handler raises.
- **The `disabled` gate is re-evaluated at both ends of every turn**, not
  sampled when the changeset is published. A provider can only publish from
  inside its own turn, so a gate read once is a gate stuck at "busy" for the
  life of the session. `running` and `error` are left alone: they belong to an
  invocation, not to this gate.
- **Review survives a republish whichever side set it.** `filesReviewChanged`
  is the one client-dispatchable `changeset/*` action and the server MAY
  originate it too; the ticks are reconciled from the channel's own state, so
  the host's own tick is remembered exactly like a reviewer's.
- **The catalogue entry is re-emitted when it changes** — label, description,
  `changeKind`, `capabilities.review` — and not when it does not. The entry is
  the only copy a client has, and the review gate reads the current one.

`SessionSummary.changes` rides on `root/sessionSummaryChanged`. It is not
written into `SessionState`, which declares no such key.

**The `resource*` family** is implemented, and exposes nothing by default. A
host does not acquire a filesystem by being upgraded: install
`RootedFilesystemResourceProvider` to serve one directory, and `writable=True`
is a **second, separate opt-in** on top of that — reading discloses, writing
destroys, and the two should not be granted by the same gesture.

The jail walks a path one component at a time with `openat` and `O_NOFOLLOW`,
resolving any symlink itself and re-checking the result against the root. That
is deliberately not `realpath`-then-open: between the check and the open, a
component can be swapped for a symlink and the open follows it — the check
passed, the read escaped. There is a test for exactly that race. Reads follow
symlinks; a write whose final component is one is refused rather than followed,
because a write's policy check runs against the name the peer sent.

Reads are bounded — 16 MiB by default, `Host(max_read_bytes=…)` to raise it and
`None` to remove it. The protocol has no partial read (`resourceRead` takes no
offset or length), so a file above the bound is refused rather than truncated:
unbounded, one 64 MiB file took host memory from 29 MB to 970 MB.

```bash
python -m agent_host_server --serve-directory ./workspace
```

**Sessions can survive a restart.** Install a `FileSessionStore` and call
`await host.restore()` before serving. JSON only — never `pickle`, never
`eval` — because a stored session is attacker-influenced data: titles, chat
content and tool results all come off the wire. `serverSeq` survives too, which
matters more than it sounds: the reference client records it with a *maximum*,
so a counter that restarts at zero can never replay again.

**Client-published plugins are expanded.** `resource*` is symmetrical, and the
reverse direction exists so a host can fetch content only the client has — a
`virtual://my-client/...` plugin lives in the client's memory and no filesystem
here will find it. The host reads it back and publishes the children, which is
what makes a plugin's skills, prompts and instructions render rather than
appearing as an empty container.

### Not implemented

**No command answers `MethodNotFound`.** That used to be how this host declined
a feature, since AHP has no server capability object. A refusal is specific
now: `PermissionDenied` for something the host will not do, `NotFound` for
something it does not have, `ProviderNotFound` for an agent that does not
exist. Each tells a client more than "stop asking".

What is genuinely absent, and why:

- **An MCP client runtime.** The lifecycle commands are answered and the
  customization is published; nothing spawns or connects a server.
- **Model routing.** `AgentInfo.models` is published for the client's picker
  and `UserMessage.model` carries back what the user chose — the host is a
  courier and never selects.
- **`chat/usage`.** No producer, so no token counts or cost attribution.
- **Checkpoints and plan review.** Not host-drivable: they are internal to
  VS Code's own in-process host, with no channel, command, action or state
  field in the protocol.
- **`pickle`, `eval`, or any `__reduce__`-capable store format**, permanently.
  JSON only. See [`docs/roadmap.md`](docs/roadmap.md) §10.
- **Retracting a session-summary field over `root/sessionSummaryChanged`** —
  the wire cannot say it. "Only fields present in `changes` have new values;
  omitted fields are unchanged", and every property of `changes` is typed as
  its own non-null type, so there is no token for "this field is gone". A
  session that reported an `activity` and then went idle keeps advertising the
  last one to a client rendering from that incremental cache, until it
  re-fetches. `listSessions` and every fresh subscriber always see the truth,
  and the chat catalogue does not have the problem — `session/chatAdded` is a
  documented upsert and this host retracts through it. Open question 11 in
  [`docs/research.md`](docs/research.md); this host will not invent an
  encoding for it unilaterally.

Session state is in-memory by default: sessions do not survive a host restart
unless the embedder installs a `SessionStore`. `serverSeq` survives one with
`--sequence-file`; without it a reconnecting client is correctly told to take
fresh snapshots, but it is told that on **every** reconnect thereafter, because
the reference client records the sequence with a maximum and stays permanently
ahead of a counter that restarted at zero.

## Embedding it behind a proxy

The library ships one `Policy` that permits everything
(`LoopbackSingleUserPolicy`) and one that partitions sessions between users
(`OwnedSessionPolicy`). The second is an **example**, not a core concept — but
it comes with the negative tests that matter, because every deployment writes
the same four and they are the ones that fail loudly when a change routes around
a hook: peer B cannot see A's session, cannot subscribe to its channels, cannot
dispatch into it, and cannot resume A's connection by asserting A's `clientId`.

`Host.serve` takes `headers=` and `token=`, and the WebSocket server forwards
both from the upgrade. **The library assigns meaning to neither.** Which header
carries a principal, and whether to believe it, is the embedder's decision — and
a forwarded header is evidence only if the socket cannot be reached except
through the proxy that set it.

`Host.counters()` reports connections, sessions, active turns, pending requests,
watches, channels and `serverSeq`. `AuditSink` records decisions — admissions,
refusals, session creation, tool-call resolutions — with **no conversation
content by construction**. Both are absent by default.

One host runs **one provider**. `RootState.agents` is plural and this publishes
one entry, deliberately: see [`docs/roadmap.md`](docs/roadmap.md) §4a.

## ⚠️ Security: read this before exposing a host

**AHP defines no security model, and says so.** Connection admission is
explicitly outside the wire protocol
([`transport.md`][transport]). The `authenticate` command is *not* a login — it
pushes tokens for upstream services the agent talks to, and never gates the AHP
connection itself. There is no server capability object, so a host cannot
negotiate dangerous surfaces away; it can only refuse them.

A host that implements the full protocol literally hands any peer that completes
`initialize`: a read/write/delete filesystem API, arbitrary pty creation with a
client-chosen working directory, enumeration of every session on the machine,
and the ability to approve **any other client's** pending tool call — the
protocol's own validation table conditions tool-call approval on the call's
*status*, never on client identity.

This library's position:

- The core guarantees **protocol** invariants only — sequencing, reducer parity,
  state-transition validity, action-origin stamping, client-dispatch gating.
- Every **trust** decision — who may connect, who may see which channel, who may
  approve — is a **mandatory, no-default, embedder-supplied policy**.
- **There is no `serve()` one-liner that binds a socket.** Constructing a host
  requires a policy object.
- Default bind is loopback. Binding off-loopback without an explicit policy is a
  hard error, not a warning.
- The filesystem family and terminals **are** implemented, and every one of them
  is **off by default**: no filesystem without a resource provider, no writes
  without a second and separate `writable=True`, and no command execution
  without a terminal backend you construct by name. The default backend declines
  every terminal with a reason. Nothing arrives by upgrading.
- The jail **walks** — `openat` with `O_NOFOLLOW`, one component at a time, each
  resolved symlink re-checked against the root — rather than resolving a path
  and then opening it, which has a window in which a component can be swapped.
- `--wire-log` redacts credentials and writes owner-only. It still contains
  every message of every session, which is a transcript, not a trace.

**This is single-trust-domain.** It is not multi-tenant, and it is not safe to
expose to an untrusted network. Both known existing hosts punt on this too — VS
Code uses a single connection token; the one third-party host states outright
that remote and multi-tenant security are unimplemented — but that is context,
not reassurance.

[`SECURITY.md`](SECURITY.md) has the scope and the disclosure path;
[`docs/guide/deploying.md`](docs/guide/deploying.md) has the long version, with
examples the test suite executes.

## Conformance

"Conformant" without a conformance test is a lie, and this project's entire
value is that other implementations can trust it. So:

The reducers and the wire types now live in
[`agent-host-protocol`](https://github.com/jackhumbert/agent-host-protocol-py),
a separate package this one depends on, so that a Python *client* can share them
rather than fork them. Its conformance gates are listed here because they are
what this host stands on:

- Reducers are validated against **upstream's own 247-fixture corpus**, the same
  artifact the Rust, Go, Kotlin and Swift clients are gated on, consumed
  unmodified. All 247, not a subset.
- Wire types are validated against upstream's 39-fixture round-trip corpus.
- **The corpus's own blind spot is covered separately.** Its comparator drops
  `null`-valued keys on both sides, so it cannot express the difference between
  an absent key and an explicit `null` — the single most common porting defect
  here, and one an audit found in four reducers at once. So a second corpus is
  generated by running adversarial cases through the **real pinned TypeScript
  reducers** under Node and freezing their output verbatim, nulls and all
  Comparison is byte-for-byte, offline, and it runs in the protocol package.
- Integration tests drive the **real published Microsoft TypeScript client**
  against the host, and feed our live action stream through the **official
  TypeScript reducers**, diffing the result against a fresh snapshot.
- A golden trace captured from a live **VS Code 1.131** session is replayed on
  every run.
- Multi-client tests cover the thing AHP exists for: several clients over one
  session agreeing on order, origin and final state.

That last one matters because the reference client validates almost nothing —
in testing it accepted an unoffered protocol version, a missing required field
and a 94-wide sequence gap without complaint. Host correctness is simply not
observable by pointing a client at it, so it has to be asserted directly.

## Relationship to upstream

This project targets an external specification. Protocol changes come from
upstream, not from contributors' preferences. Where something is underspecified,
the question is filed upstream and recorded in `docs/research.md` — this project
does not fork the spec or diverge privately. See
[`CONTRIBUTING.md`](CONTRIBUTING.md) and [`UPSTREAM.md`](UPSTREAM.md).

Licensed **MIT**, matching upstream — this repository vendors upstream's
MIT-licensed conformance fixtures and ports its reducers, so identical terms
avoid any compatibility question.

[ahp]: https://microsoft.github.io/agent-host-protocol/
[transport]: https://microsoft.github.io/agent-host-protocol/specification/transport
