# ADR 0005 — Verify the negotiated version; observe sequence gaps, never raise

**Status:** accepted · **Date:** 2026-08-02

## Two decisions, one theme

Both are places where the reference clients validate nothing and the failure is
silent. Both are fixed here in a way that is *observable* rather than fatal,
because a client that raises against real hosts is a client nobody uses.

## Version verification

`InitializeResult.protocolVersion` MUST be one of the entries the client
offered. The reference client does not check, and was measured accepting a
version it never offered (the host repo's `docs/experiments.md` E3).

**`verify_negotiated_version` defaults to `True`** and raises
`ProtocolVersionError` on a mismatch, using the shared package's
`is_compatible`. Set it `False` for bug-compatibility with a host that answers
loosely.

Two related rules that are part of this decision:

- **Default the offered list to `DEFAULT_SUPPORTED_VERSIONS`**, the shared
  package's claim about what its vendored tables cover — *not*
  `UPSTREAM_SUPPORTED_PROTOCOL_VERSIONS`, which is a fact about upstream and
  currently includes versions whose action and state tables are not vendored.
  Offering one of those means negotiating a protocol we cannot reduce: we pass
  our own compatibility check and then apply the wrong reducer branches. A test
  asserts the offered list is a subset of what the pin covers.
- **`-32005` responses may carry SemVer *range* constraints** in
  `data.supportedVersions` (`">=0.1.0 <0.3.0"`, `"^0.2.0"`), not just exact
  versions. Surface them verbatim on the exception rather than equality-matching
  and producing a confusing failure.

## Sequence gaps

`serverSeq` is host-global and monotonic. A gap in the sequence a client
observes means an envelope was lost, and after that the mirror is wrong until a
fresh snapshot. **No reference client detects this.**

`GapPolicy` is on by default at `WARN`:

| Policy | Behaviour |
|---|---|
| `IGNORE` | reference-compatible; apply and say nothing |
| `WARN` *(default)* | emit `SequenceGap(expected, received, channel)` on `diagnostics()`, then apply |
| `RESEED` | additionally re-`subscribe` the affected channel to re-seed from a snapshot |

**Never fatal, in any policy.** A gap is evidence of a lost envelope, but it is
also what a legal host produces when actions are filtered by subscription — a
client sees only the channels it subscribed to, so its view of a host-global
counter is *expected* to have holes. Raising would make the common case an
error. `WARN` is the honest default: the information exists, the consumer
decides.

This is why `RESEED` is not the default despite being the "correct" repair. It
costs a full state transfer per gap, and against a host with several busy
channels a client that reseeds on every hole never stops reseeding.
