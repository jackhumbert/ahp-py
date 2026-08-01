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
