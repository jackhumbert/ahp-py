# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/) — **independently of the
protocol's own version**, exactly as upstream's clients do. Every release states
the spec revision it targets.

## [Unreleased]

### Added

- **`agent_host_protocol.conformance.schemas`** — the vendored JSON Schemas as
  an assertion, shipped rather than kept in `tests/`. A host and a client each
  need to prove the same thing about opposite directions of the same wire, and
  two copies of that file is exactly the drift this package exists to prevent.
  `jsonschema` is imported at module scope and is deliberately NOT a dependency:
  whether a missing validator should skip a test or fail a build is the caller's
  policy, not ours.
- `SECURITY.md`, and a disclosure path. This package performs no I/O, so its
  surface is not what it can reach but what it computes for the peers that trust
  it — the reducers, and `IS_CLIENT_DISPATCHABLE`, which is the table a host
  uses to decide whether a peer may dispatch an action at all.
- A tag-triggered publish workflow using PyPI trusted publishing (OIDC), with a
  wheel that is installed and RUN from outside the checkout before it is
  released (`scripts/smoke_wheel.py`).
- A non-blocking CI job that builds and tests both consumers against this
  package's `main`. It is a floor, not a leaf: a change here that breaks the
  host or the client has broken the point of the extraction, and finding that
  out at their next release is too late.

### Removed

- `scripts/check_sibling_drift.py` and its CI job. It existed to make divergence
  visible while the host still carried its own copy of this tree — and that
  premise stopped being true when the host migrated onto this distribution.
  Packaging enforces what the script watched for: there is one copy now, and you
  cannot import upwards across a wheel. Its docstring had become the thing
  `AGENTS.md` warns about, a document asserting the absence of something that
  shipped.

### Changed

- The version is single-sourced from `__init__.py` through hatchling's dynamic
  version. It was declared in two places, which is how the sibling host shipped
  `0.0.0` twice.

Extracted from [`agent-host-server-py`](https://github.com/jackhumbert/agent-host-server-py),
where all of this code was written. See [ADR 0002](docs/decisions/0002-extraction.md).

### Added

- Wire types, `TypedDict` views, `TypeSpec` validation and the generated
  upstream data tables (`types/`).
- All seven state reducers, the injectable clock and `js.py` (`reducers/`).
- Protocol-version negotiation (`versions.py`), the error taxonomy
  (`errors.py`), channel URIs (`channels.py`) and the transport abstraction with
  an in-process pair (`transport/`).
- `errors.from_json()` — the receiving half of the error taxonomy, which a host
  never needed. Accepts **any** integer code: upstream's own
  `errors.schema.json` omits `-32011 Conflict`, and third-party hosts ship their
  own maps, so rejecting an unrecognised code would turn someone else's
  extension into a crash.
- `channels.reducer_for_state()` — a state **shape** classifier, verified
  against all 247 upstream fixtures' declared reducers with no unclassifiable
  case (root 7, session 70, chat 123, terminal 19, changeset 16, resourceWatch
  2, annotations 10).
- The vendored conformance corpora ship **inside the wheel**, so a downstream
  implementation can run the same gate. `tests/unit/test_packaging.py` builds a
  real wheel and asserts they are in it.
- `scripts/check_sibling_drift.py` — reports divergence from the sibling host's
  still-unmigrated copy of this tree.
- `tests/docs/test_readme_is_true.py` — executes every fenced `python` block in
  the README and derives its counts and version lists from the code.

### Removed

- `channels.reducer_name_for(uri)`. It was dead code that picked a reducer from
  a URI **scheme**, which is the exact failure the sibling's own invariant 15
  forbids: VS Code mints `<provider>:/<uuid>` for sessions and three
  `agenthost-terminal:` forms for terminals, so scheme routing binds no reducer
  at all and state freezes silently while actions keep arriving. Use
  `reducer_for_state()`, or bind the reducer when you register the channel.

### Fixed

- `conformance.corpus.CORPUS_ROOT` resolved to a `vendor/` directory outside the
  package, so an installed wheel carried a fixture loader and no fixtures. It
  now prefers the packaged tree and falls back to the source checkout.

### Notes

- Targets spec `spec/v0.7.0` (`ea6fae670c4012721fdc02d587b3a46ecdc871c0`).
  `DEFAULT_SUPPORTED_VERSIONS` is `0.7.0, 0.6.0`.
- `agent-host-server-py` has **not** been migrated onto this package yet; it
  still carries its own copy. That migration is deliberately deferred.
