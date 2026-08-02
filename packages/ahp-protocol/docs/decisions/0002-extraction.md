# ADR 0002 — The shared protocol layer is its own distribution

**Status:** accepted · **Date:** 2026-08-02
**Supersedes nothing. Extends `agent-host-server-py`'s `docs/plan.md` §2.**

## Context

`agent-host-server-py` is the only AHP host library in any language. A sibling
AHP *client* is now being built. Both need the same wire types, the same seven
reducers, the same version negotiation, the same error codes, and the same
transport abstraction — and, critically, both need them to behave *identically*,
because the whole point of a protocol library is that two peers agree.

The server's `docs/plan.md` §2 anticipated this exactly:

> The internal boundaries are enforced by an import-linter rule instead
> (`types` and `reducers` may not import `core`, `transport` or anything doing
> I/O), so the split remains cheap to perform later if a Python *client*
> appears.

and reserved the PyPI name:

> `agent-host-protocol` … describes the *protocol*, not a host — taking it would
> squat the name a future Python *client* or types package should have.

The window is open and closing: the server is version `0.0.0` with no git tags
and no downstream consumers, and its `import-linter` contract already
machine-proves the boundary being cut.

## Decision

**Extract the pure layer into `agent-host-protocol-py`, publishing the
distribution `agent-host-protocol`, and have both peers depend on it.**

Extracted: `types/`, `reducers/`, `conformance/`, `transport/`, and
`core/{versions,errors,channels}.py` promoted to the package root.

Not extracted: `core/seq.py` (durable `serverSeq` allocation is a host concern
— a client never allocates one) and `ws/` (see below).

## Alternatives rejected

**A fork with a drift-detecting CI job.** The decisive argument is the failure
*mode*, not the effort. A sync script makes drift detectable, never impossible,
and the 247-fixture comparator drops `null`-valued keys on both sides — so a
`js.assign` null-passthrough fix landing on one side leaves both suites green
while the two implementations disagree about what a peer just sent. Six defects
of exactly that class already lived inside that blind spot in the server.

**A git submodule or subtree shared into both repos.** Solves source identity,
not *distribution* identity. Two wheels would still ship two module objects and
two `reducers/clock.py` globals, so `frozen_clock()` in one would not freeze the
other — which breaks any application embedding a host and a client together.

**The client depending on `agent-host-server`.** Inverts the dependency and
drags `core/host.py`, a PTY backend and a filesystem jail along to get
`chat_reducer`.

**A monorepo with two distributions.** The closest call. It genuinely simplifies
release coordination and would let one CI run gate both peers against each other
on every commit. Rejected because the repos already exist as siblings with
separate `AGENTS.md`, `CHANGELOG.md`, ADR series and issue trackers, and merging
is a strictly larger disruption than extracting. Recoverable later; a
cross-repo CI job recovers most of the benefit meanwhile.

## Consequences

**The WebSocket transport stays with each peer.** Upstream splits it out
(`ahp-ws`, `ahpws`), and we do not: the frame codec is common but a serving
`ws.serve()`/upgrade path and a connecting `ws.connect()` path share almost
nothing else, and a fourth distribution to hold ~80 lines of codec is not worth
a release axis. Revisit if a third transport (stdio, Unix socket) appears in
both.

**Two defects are fixed on the way out, not inherited.**

- `conformance/corpus.py` resolved `CORPUS_ROOT` to a `vendor/` directory the
  wheel did not contain, so an installed package had a fixture loader and no
  fixtures. Fixed by mapping the vendored tree into the wheel and preferring it,
  with `tests/unit/test_packaging.py` building a real wheel to prove it — a
  source checkout structurally cannot catch this.
- `core/channels.py::reducer_name_for(uri)` was dead code that routed on the URI
  scheme, which is the exact failure the server's own invariant 15 forbids.
  Deleted and replaced with `reducer_for_state(state)`, a shape classifier
  verified against all 247 fixtures' declared reducers.

**`importlib.resources` is deliberately not used** to find the corpus. It
returns a `Traversable`, not a `Path` — no `.glob`, `.resolve` or `.parents` —
and reaching a real path means `as_file()` inside an `ExitStack`, turning
module constants into context managers for no gain. The cost is that a
zip-imported package cannot find its corpus, which is not a supported way to run
a fixture corpus.

**The server is not migrated yet, and that is deliberate.** This repository
exists and is green; the server still carries its own copy of the extracted
tree. Until the migration lands the two can drift, and that is a real, accepted,
temporary liability. The migration is one PR whose acceptance criterion is
stated in advance: **the server's existing suite passes with zero changes to any
test assertion — only import lines move.** If an assertion needs touching, the
extraction changed behaviour and must be reverted.

**Version coupling.** This distribution's SemVer is independent of the spec's,
but its MINOR moves whenever the vendored spec tag's MINOR moves. Both peers pin
`~= 0.1.0` — compatible-release, so patch fixes flow without a consumer release
while a spec-shaped change is a deliberate upgrade. Each peer asserts
`UPSTREAM_PROTOCOL_VERSION` in its own README test, so a dependency bump that
moves the spec under it fails loudly.
