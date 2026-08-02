# ADR 0002 — Per-channel queues are lossless; only fan-in taps drop

**Status:** accepted · **Date:** 2026-08-02

## Context

Every reference client fans inbound events out through a bounded broadcast queue
that evicts the oldest entry when a reader lags. That is the right shape for a
*tap* and the wrong shape for the mutation stream.

A dropped `ActionEnvelope` is not a dropped notification. State is a fold over
the action sequence, so losing one envelope means the mirror is wrong from that
point until a fresh snapshot — and nothing tells the consumer it happened.
Rust's own module documentation admits its broadcast-backed surfaces permanently
desync the mirror on a drop. Swift made per-URI streams unbounded for exactly
this reason and documented why.

Bounding it is not a memory-safety win either; it converts a silent-corruption
bug into a silent-corruption bug that is harder to reproduce.

## Decision

**Per-URI subscription queues are unbounded. The fan-in taps — `events()` and
the host-event stream — are bounded with oldest-eviction, and every eviction is
reported.**

Defaults: `subscription_buffer = 0` (unbounded), `event_buffer = 4096`, host
fan-in `1024`.

## The cost, stated plainly

This trades a bounded failure for an unbounded one. A consumer that stops
draining a chat subscription during a long streaming turn grows the queue
without limit, and the only backpressure path the protocol offers is
`SubscribeParams.delivery.maxLatencyMs`, which is advisory and which VS Code
never sets.

We accept it because the alternative is worse in kind, not merely in degree: an
unbounded queue fails loudly, in a way a profiler finds, and only for a consumer
that has already stopped consuming. A dropped envelope fails silently, in the
rendered transcript, for a consumer that is behaving correctly.

## Mitigations that are part of this decision, not follow-ups

- Every drop on a bounded tap emits a diagnostic. A tap that silently skips is
  the same failure one layer up.
- `docs/guide/` states the rule for consumers: if you cannot keep up with a
  subscription, close it — do not hold it open and ignore it.
- The state mirror is not a consumer of these queues. It is fed on the read
  path, so state is correct even when every tap has been abandoned. That is what
  makes bounding the taps safe at all.
