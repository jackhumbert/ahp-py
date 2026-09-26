# ADR 0007 — `mcpApps` and the telemetry channel are out of 0.1.0

**Status:** accepted · **Date:** 2026-08-02

## Context

Two documented surfaces are reachable from a client and are not being built for
0.1.0.

**The `mcp://` side-channel.** It speaks verbatim MCP minus
`initialize`/`initialized`, serves only methods in the
`AhpMcpUiHostCapabilities` union (`-32601` for anything else), and its
`McpServerCustomization.channel` MUST be re-read on every
`session/customizationUpdated` and treated as unavailable while absent.
Declaring `capabilities: {"mcpApps": {}}` obliges the client to host the MCP
Apps View sandbox and own all `ui/*` postMessage traffic on the App's behalf.

**`ahp-otlp:` telemetry.** `InitializeResult.telemetry` advertises RFC 6570
templates such as `ahp-otlp://logs{?level}` which the client must expand before
subscribing, with each distinct expansion a separate server-side subscription
and none of it replayed on reconnect.

## Decision

Both are out of 0.1.0. **Therefore `mcpApps` is never sent in
`ClientCapabilities`.**

That second sentence is the actual decision. The capability field is a promise,
and the protocol's own wording makes it one: "Servers SHOULD only advertise
features whose corresponding client capability is set here." A host that sees
`mcpApps` will populate `McpServerCustomization.mcpApp` and expose an `mcp://`
channel, and a client that cannot host the sandbox then leaves App-bearing tool
calls broken.

**A host must not claim what it cannot do, and neither must a client.** A gap
degrades; a false claim fails.

## What is still built

The three `otlp/export*` notifications **are** fanned out (ADR: we surface all
nine server notifications, where the TypeScript client surfaces five and drops
the rest at a `default:` branch that reaches neither subscriptions nor
`events()`). They simply never fire, because nothing subscribes to a telemetry
channel.

That is deliberate and is the honest half-measure: the plumbing costs nothing,
it means a consumer with its own template expansion can use
`client.protocol.subscribe(...)` and receive them, and it does not require us to
claim a capability. What we do **not** do is expand the template ourselves and
call it telemetry support.

Two other proposals in the design pass fanned out `otlp/*` without ever
subscribing and described that as covering telemetry. It does not; the handlers
can never fire. Saying so here is the point of the ADR.

## Revisit when

`mcpApps` when there is a client in this ecosystem that can host a View
sandbox — realistically a TUI or GUI consumer, not the library. Telemetry when
something wants the traces; VS Code currently discards them, so there is no
consumer to be wrong about yet.
