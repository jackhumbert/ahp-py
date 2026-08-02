# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning is [SemVer](https://semver.org/), independent of the protocol's.
Every release states the protocol versions it speaks.

## [Unreleased]

Under construction. `docs/plan.md` is the design and its §12 is the build order.

### Added

- **M1 — scaffold.** Packaging, `mypy --strict`, ruff, three import-linter
  contracts, ADRs 0001–0007.
- **M2 — transport and client core.** `AhpClient` (single-shot, one reader and
  one writer task), the `BroadcastQueue` behind lossless per-channel delivery,
  the error taxonomy with `is_session_gone()`, all nine server notifications
  surfaced, a connecting WebSocket transport, and `agent_host_client.testing`
  as public API.
- **M3 — commands and parity.** All 27 client→server wrappers with channel
  scoping derived from upstream's own `*Params` types, and a generated
  `docs/parity.md` asserted against the vendored `messages.ts`.

### Notes

- Speaks protocol `0.7.0` and `0.6.0` (`DEFAULT_SUPPORTED_VERSIONS`), and
  **verifies the version a host answers with** — the reference client does not.
- `mcpApps` is not advertised and the `ahp-otlp:` channel is not subscribed to;
  see [ADR 0007](docs/decisions/0007-out-of-scope-for-0-1-0.md).
