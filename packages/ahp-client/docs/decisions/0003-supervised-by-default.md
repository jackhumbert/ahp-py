# ADR 0003 — `connect()` is supervised by default

**Status:** accepted · **Date:** 2026-08-02

## Context

TypeScript, Rust and Swift all make their `AhpClient` single-shot: it owns one
transport, and when that transport dies the client is dead. Reconnect,
backoff, `clientId` persistence and subscription replay live in a separate
`hosts` layer that builds a fresh client per attempt.

That split is architecturally right. A dead transport cannot be revived, so
recovery genuinely means a new transport and therefore a new client, and mixing
the two concerns is how reconnect logic ends up interleaved with request
correlation.

It is also a bad default. A five-line script that talks to a host over a laptop
that sleeps should not have to learn a supervisor.

## Decision

**Keep the split. Change the default composition.**

`AhpClient` remains single-shot with no reconnect logic. `HostRuntime` remains
the supervisor. `connect()` — the front door — composes them, and
`reconnect=True` is the default. `connect(url, reconnect=False)` yields the
single-shot client for anyone who wants to drive reconnection themselves.

## Consequences

`Client` always carries a supervisor task, a generation counter and a
`ClientIdStore` — even for a script that runs for two seconds. That is real
overhead for the simplest case, and it means a bug in the `hosts` layer is a bug
in the default path rather than an opt-in one.

Accepted, because the failure it prevents is worse than the cost: a client that
silently stops receiving actions after a network blip, with a mirror that is
still confidently serving stale state, is indistinguishable from a hung agent.

**If the supervisor proves flaky the fix is to fix it, not to flip the
default.** Flipping it after 0.1.0 is a breaking change for every consumer who
wrote no reconnect handling because they did not need any. That is the trade
being made now, deliberately, while it is still free.

`Session` handles are generation-checked, but unlike the reference
`HostClientHandle` they **retarget transparently** across a reconnect rather
than raising — subscriptions were replayed and the mirror re-seeded, so the
handle is still valid. Only a session the host reports in `missing[]` raises
`SessionGone`. A `TurnStream` alive across a reconnect emits `Reconnected()` and
continues; if the replayed state shows no active turn it emits
`TurnFailed(reason="disconnected")`, because the spec says in-progress turns
SHOULD be considered failed after an unexpected termination.
