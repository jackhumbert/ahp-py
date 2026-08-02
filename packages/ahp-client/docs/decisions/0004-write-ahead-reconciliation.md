# ADR 0004 — Implement write-ahead reconciliation, following VS Code

**Status:** accepted · **Date:** 2026-08-02

## Context

`docs/guide/reconciliation.md` specifies the algorithm: a client keeps
`confirmedState`, a queue of `pendingActions` applied optimistically, and a
computed `optimisticState`; server echoes retire pending entries, and an echo
carrying `rejectionReason` reverts one.

**No reference client implements any of it.** The TypeScript `AhpStateMirror`
has no pending queue, never reads `envelope.serverSeq`, ignores `ahp-chat:`
entirely, and covers four of seven reducers. Rust and Go ship a mirror of
similar scope. The only working implementation is VS Code's internal
`agentSubscription.ts`, which we can read but cannot run against.

## Decision

**Port VS Code's algorithm, not the TypeScript SDK's absence of one.**

Per inbound envelope:

- **Own echo** (`origin.clientId == ours`) **with `rejectionReason`** → drop the
  pending entry **without applying**, emit `ActionRejected` on `diagnostics()`.
- **Own echo without** → drop the pending entry, apply to confirmed.
- **Own echo with no matching pending entry and no rejection** → **still apply
  to confirmed.** This is `agentSubscription.ts:327-328` and it is the arm every
  design proposal missed.
- **Foreign or server-originated** (`origin` absent *or* explicitly `null` —
  treated identically) → apply to confirmed. Pending rebases for free, because
  `optimistic` is recomputed rather than stored.

Matching is on **exact `clientSeq`** (`agentSubscription.ts:321`), not Swift's
cumulative ack.

## Why exact match rather than cumulative

Cumulative ack — retiring every pending entry with `clientSeq <= echoed` — is
tempting because it bounds the queue when the host silently ignores an action on
an unknown channel (the spec makes that case emit *no* echo at all, which is an
asymmetry the host's own invariant 12 documents).

Rejected anyway. Cumulative ack silently drops a pending action whenever the
host echoes a later `clientSeq` first, which is legal if the host reorders. That
converts a bounded leak into silent state divergence, and no reference client
does it. The leak is instead bounded by dropping pending entries on reconnect
(ADR 0006) and by reporting queue depth through `diagnostics()`.

## The consequence nobody costed

The chat reducer resets `modifiedAt` from the clock at turn end
(`channels-chat/reducer.ts:133-190`) and recomputes `ChatState.status` from open
input requests. So optimistic replay stamps a **different** `modifiedAt` than
the server's echo, guaranteeing a visible optimistic-vs-confirmed diff at the
end of every turn.

Views therefore render `confirmed.modified_at`, and `docs/guide/state.md` says
why. This is not a bug to fix; it falls out of running a clock-reading reducer
twice over the same action, and any client that renders optimistic timestamps
has it.

## Risk

The reference is VS Code-internal and unversioned, and we cannot execute it.
Hypothesis properties test self-consistency — `optimistic == replay(confirmed,
pending)`, and pending never holds an acknowledged `clientSeq` — which is not
conformance. If our pop/rebase/revert semantics differ from VS Code's in a way
the sibling host does not exercise, a real host is where we find out.
