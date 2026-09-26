# ADR 0001 — Depend on `ahp-protocol`; never fork it

**Status:** accepted · **Date:** 2026-08-02

## Context

A client needs the same wire types, the same seven reducers, the same version
negotiation and the same error codes as a host, and needs them to behave
*identically* — that agreement is the entire point of a protocol library.

All of it already exists, written and gated in `ahp-host`. The
options were to fork it with a drift-detecting CI job, or to extract it into a
distribution both peers depend on.

## Decision

**Depend on `ahp-protocol ~= 0.1.0`. Copy nothing.**

The extraction is done and is argued in that repository's
[ADR 0002][adr2]. What matters here is the consequence: **no wire type, reducer,
error code, version constant, transport ABC or conformance fixture is defined in
this repository.** A PR that adds one is wrong even if it is correct.

## Why a fork was rejected

The decisive argument is the failure *mode*, not the effort. A sync script makes
drift detectable, never impossible — and the 247-fixture comparator drops
`null`-valued keys on both sides, so a `js.assign` null-passthrough fix landing
on one side leaves both suites green while the two implementations disagree
about what a peer just sent. Six defects of exactly that class already lived
inside that blind spot in the host.

The reducers are ~2,500 lines of hand-ported JavaScript semantics. Re-porting
them would be the single largest avoidable mistake available to this project.

## Consequences

**Wire values are plain dicts, and that is inherited, not chosen here.** There
is no parse-into-objects step; static typing comes from `TypedDict` views. See
the shared package's ADR 0001. It applies to a client *more* strongly than to a
host: a client mirrors state it re-renders and may relay to plugins, and a peer
authoritative for state it relays to peers newer than itself must not drop what
it does not model.

**Ergonomics come from derived things, never from decoding.** Two of them, and
neither copies or mutates the wire:

- zero-copy `__slots__` **views** exposing snake_case properties over the
  underlying dict, with `.raw` always available — types as a *lens*;
- frozen, `match`-able **event** dataclasses, each carrying its `envelope`.

`UnknownEvent(envelope)` is the forward-compat arm, and it is the one place the
shared package's no-`Unknown`-class rule inverts: events are *ours*, not the
wire's.

**The pin is asserted, not assumed.** `tests/docs/` asserts
`ahp_protocol.UPSTREAM_PROTOCOL_VERSION`, so a dependency bump that moves
the spec under us fails loudly rather than silently changing what we speak.

**The WebSocket transport is ours.** The shared package holds the `Transport`
ABC and the in-memory pair; a connecting socket is this repository's, and a
serving one is the host's. See the shared ADR 0002 for why that is not a fourth
distribution.

[adr2]: https://github.com/jackhumbert/ahp-py/blob/main/packages/ahp-protocol/docs/decisions/0002-extraction.md
