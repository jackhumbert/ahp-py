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
- **M4 — state mirror.** `confirmed`/`pending`/`optimistic` with the write-ahead
  reconciliation `docs/guide/reconciliation.md` specifies and no reference client
  implements. Reducers bind by name, never by URI scheme; pre-snapshot envelopes
  are buffered and replayed filtered on `fromSeq`; sequence gaps are reported and
  never fatal.
- **M5 — the front door.** `connect`, `Client`, `Session`, `Chat`, and a
  `TurnStream` that is both awaitable and async-iterable. Events are frozen and
  `match`-able and carry their own answers (`ToolCallReady.approve()`).
  `reconnect=True` by default.
- **M6 — the supervisor.** Reconnect, backoff with injectable jitter, replay,
  `clientId` persistence, and a `link()` context manager that makes the
  reference implementation's listener leak structurally impossible.
- **M7 — the reverse direction.** All 10 `ServerCommandMap` methods, a
  symlink-safe file server whose write half is a second opt-in, in-memory
  `virtual://` plugin content, client-owned tool execution, and the elicitation
  surfaces including `chat/toolCallResultConfirmed`.
- **M8 — logs, doctor, interop.** ahp-inspector-compatible wire logs with
  credential redaction and no opt-out, `agent_host_client.doctor` as a
  conformance probe for someone else's host, and a full turn against the sibling
  Python host.

### Notes

- Speaks protocol `0.7.0` and `0.6.0` (`DEFAULT_SUPPORTED_VERSIONS`), and
  **verifies the version a host answers with** — the reference client does not.
- `mcpApps` is not advertised and the `ahp-otlp:` channel is not subscribed to;
  see [ADR 0007](docs/decisions/0007-out-of-scope-for-0-1-0.md).
