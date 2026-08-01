# agent-host-server

A Python **host/server** library for the [Agent Host Protocol][ahp] (AHP) —
Microsoft's protocol for synchronized multi-client state over AI agent sessions.

> **"AHP" here means the Agent Host Protocol, not the Analytic Hierarchy
> Process.** The PyPI names `ahp` and `pyahp` belong to packages for the latter,
> a decision-making method with no relationship to this project — which is why
> this one is spelled out. If you want pairwise-comparison matrices and
> consistency ratios, you want one of those instead.

> ### ⚠️ Status: early construction. Not usable yet.
>
> The protocol type layer and the wire round-trip conformance corpus are in
> place; the reducers, host and transport are not. Nothing is published to PyPI
> and the API is not stable. See [`docs/plan.md`](docs/plan.md) §11 for the build
> order, [`docs/research.md`](docs/research.md) for why it is shaped this way,
> and [`docs/decisions/`](docs/decisions/) for the decisions taken so far.

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

### Planned for v0.1

Protocol **0.6.0** on the wire · root, session and chat channels · the eight
commands a real client actually issues · host-global sequencing with replay ·
ported reducers gated on upstream's own 247-fixture conformance corpus · a
pluggable agent provider with an offline echo implementation · WebSocket
transport behind a transport abstraction.

### Deliberately not in v0.1

Terminals · changesets · comments and annotations · OTLP telemetry · the MCP
channel · resource watches · the nine `resource*` filesystem methods · side
chats · multiroot sessions · authentication, including 0.6.0 step-up auth ·
`fetchTurns` pagination · completions · `resolveSessionConfig`.

Every one of these returns a proper JSON-RPC `MethodNotFound` (`-32601`). None
are silently stubbed. Where the protocol says a host may decline, it declines
loudly.

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
- v0.1 does not implement the filesystem family or terminals at all, so the two
  largest holes stay closed by construction.

**v0.1 is single-trust-domain.** It is not multi-tenant, and it is not safe to
expose to an untrusted network. Both known existing hosts punt on this too — VS
Code uses a single connection token; the one third-party host states outright
that remote and multi-tenant security are unimplemented — but that is context,
not reassurance.

## Conformance

"Conformant" without a conformance test is a lie, and this project's entire
value is that other implementations can trust it. So:

- Reducers are validated against **upstream's own 247-fixture corpus**, the same
  artifact the Rust, Go, Kotlin and Swift clients are gated on, consumed
  unmodified.
- Wire types are validated against upstream's 39-fixture round-trip corpus.
- Integration tests drive the **real published Microsoft TypeScript client**
  against the host.

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
