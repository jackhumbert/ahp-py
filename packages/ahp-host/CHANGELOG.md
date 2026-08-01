# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this project follows
[SemVer](https://semver.org).

This project implements an external specification. Our version is independent of
the protocol version — see [`UPSTREAM.md`](UPSTREAM.md) for which protocol
versions each release speaks.

## [Unreleased]

### Added

- Phase-1 research (`docs/research.md`), the empirical log (`docs/experiments.md`),
  the v0.1 plan (`docs/plan.md`) and ADRs 0001–0004.
- Vendoring of the upstream conformance corpora and schemas at `spec/v0.7.0`
  (`scripts/vendor_upstream.sh`), committed under `vendor/upstream/`.
- Generation of the upstream data tables — action types, `IS_CLIENT_DISPATCHABLE`,
  `ACTION_INTRODUCED_IN`, error codes — from the vendored TypeScript source of
  truth (`scripts/generate_tables.py`), reproducibility enforced in CI.
- Wire value representation: plain dicts with `TypedDict` views, the `??`
  equivalent, a type-aware deep comparator, and the two opposing null-comparison
  rules the upstream corpora require (ADR 0001).
- Structural validation specs for the protocol types reachable from the wire
  round-trip corpus.
- The 39-fixture wire round-trip corpus passing, including the Group B
  preserve-vs-drop fork.

### Added — host runtime

- Version negotiation over a **set** of supported versions (`0.7.0`, `0.6.0`),
  a deliberate extension beyond the reference host's single-MINOR model, so both
  VS Code and the installable npm client can connect (ADR 0002).
- Hand-ported `root`, `session` and `chat` reducers. **All 200 in-scope fixtures
  from the upstream corpus pass**, plus a per-fixture non-mutation assertion that
  JSON fixtures cannot express.
- A single global sequencer: `serverSeq` assignment, reducer application, replay
  append and fan-out all inside one critical section; per-connection outbound
  queues with a single writer task each.
- The v0.1 command set — `initialize`, `ping`, `subscribe`, `unsubscribe`,
  `listSessions`, `createSession`, `dispatchAction`, `reconnect` — with
  `MethodNotFound` for everything else.
- Client-dispatch gating from the generated table, the normative action
  validation rules, and `rejectionReason` echoes.
- A required `Policy` with no default, and a WebSocket server that is
  loopback-only unless `allow_remote=True`, with an optional bearer connection
  token rejected at the upgrade.
- The neutral provider interface and the offline `EchoProvider` (ADR 0003).
- `python -m agent_host_server` — a demo host that prints the VS Code settings.
- Interop tests driving the real `@microsoft/agent-host-protocol@0.6.0` client
  over a socket, including feeding our action stream through the **official
  TypeScript reducers** and diffing against a fresh snapshot.

### Fixed

- `SessionStatus` constants were bound to the wrong values (the set was right
  but shifted a position), so the host reported new sessions as `Error` instead
  of `Idle`. Now pinned against the vendored TypeScript enum.
