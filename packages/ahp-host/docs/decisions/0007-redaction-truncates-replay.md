# ADR 0007 — Redaction truncates the replay log

**Status:** accepted · **Date:** 2026-10-17 · **Builds on:** the sequencer's replay
log (invariants 7 and 8), [ADR 0004](0004-reduce-every-action-in-scope.md)

## Context

AHP cannot remove history. A part's content is spread through the channel's
replay log in many action types, and a reconnecting client is sent that log. To
redact a part after publishing (`Host.redact`, README "Redaction after
publishing") the host must change the stored state, tell the clients that can
understand it, and make sure no later replay returns the original. The
extension action `ahp-py/chat/partRedacted` is unknown to a stock client, which
must never be sent it.

## Options

1. **Rewrite the logged actions for the part.** Rejected. The content sits in
   `chat/delta`, `chat/reasoning`, `chat/responsePart`, `chat/toolCallStart`,
   `chat/toolCallDelta`, `chat/toolCallReady`, `chat/toolCallComplete`,
   `chat/toolCallContentChanged`, the message in `chat/turnStarted`, and in
   whatever action types a later protocol version adds. A rewritten stream has
   to reduce to exactly the redacted state, and any action type the rewriter does
   not know silently leaks.
2. **Truncate the channel's log and mark it evicted through the redaction's
   `serverSeq`, so any client below it gets a snapshot.** Chosen. It is correct
   by construction: a snapshot is the authoritative redacted state, and the
   ordering of `Snapshot.fromSeq` (invariant 8) is untouched.

## Decision

**Truncate, then snapshot.** The redaction consumes exactly one `serverSeq`,
taken after the transform succeeds (invariant 7), so a refused redaction burns
none. The channel's log is dropped up to and including that sequence and the
channel is marked evicted through it; the existing replay path already answers a
client below the eviction point with a snapshot. "The channel's log is dropped"
means the WHOLE replay log of that channel: every logged envelope is discarded
(not just those that mention the part), and the eviction watermark is raised to
the redaction's `serverSeq`. Actions published afterwards are logged as usual and
reduce onto the redacted state. The redaction envelope itself is not logged,
because the snapshot covers it.

**Selective delivery.** Connections that advertised `ahp-py/redaction` receive
the extension action as an ordinary sequenced action. Every other connection is
sent nothing, so an unknown action never reaches a stock client. Its live view
stays stale until it resubscribes.

**Owed snapshots.** A stock client may have passed the redaction's `serverSeq`
through traffic on other channels, so its `lastSeenServerSeq` would not look
behind. The sequencer keeps a per-client set of channels for which a snapshot is
owed, and the next reconnect or subscribe answers with one regardless of the
sequence it presents. The set is bounded: an entry for a client that disconnects
is dropped after a reconnect window (`Sequencer(owed_ttl=...)`, default one hour),
and the host cannot tell two connections that assert the same client id apart (a
`reconnect` carries no credential), so one of them resubscribing clears the id's
debt.

**Refused, not no-op'd.** If the target is no longer there when the transform runs
under the sequencer's lock (it vanished after the caller validated it), the
redaction raises before a `serverSeq` is taken: nothing is sequenced, delivered,
owed or audited (invariant 7). During shutdown a redaction is refused before
anything is published, because the store is no longer written; if the store write
fails after publishing, the audit entry records `persisted: false` and the error.

## Consequences

- A lagging reconnect on a redacted channel costs a snapshot instead of a replay.
- A stock client's live view is stale until it resubscribes or reconnects, and
  it may observe a `serverSeq` gap, which is legal.
- Wire logs keep the frames written before the redaction; the host cannot edit
  them, and embedders rotate them.
- The agent's own context and provider state are not touched: only the shared
  view is redacted.
