# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning is [SemVer](https://semver.org/), independent of the protocol's.
Every release states the protocol versions it speaks.

## [Unreleased]

Under construction. `docs/plan.md` is the design and its §7 is the build order.

### Added

- **The multiplexer** (`agent_host_broker.core.Broker`): one AHP endpoint for
  the surfaces, one `AhpClient` link per admitted node. It routes requests by
  channel owner, file-URI authority or provider; merges `listSessions` and the
  root channel across nodes; restamps actions onto one `serverSeq`; and relays
  host-initiated requests back to the surface. File URIs are shown to the
  surfaces as `file://<node>/path`. Speaks AHP 0.7.0 and 0.6.0.
- **The registry**: `NodeRecord`, `Principal`, and a declarative
  `StaticInventory` with group-based admission (closed by default).
- **WebSocket edges**: `WebSocketNodeConnector` dials nodes with the client
  sibling's transport, and `serve_broker` serves the surfaces with the server
  sibling's WebSocket server.

### Changed

- `agent-host-client` is pinned `>=0.1.0.dev0,<0.2` until it releases 0.1.0.
- CI checks out and installs the server and client siblings as well as the
  protocol.

- **Scaffold.** Packaging (hatchling, single-sourced `__version__`), the
  layer skeleton (`core` / `registry` / `ws` / `relay`), `mypy --strict`,
  ruff, two import-linter contracts, and the family's CI shape (sibling
  checkouts supplying the GitHub-distributed dependencies).
- **The design**, in `docs/plan.md`: two planes (AHP data plane unmodified,
  control plane beside it), the reachability split (dial-in vs. dial-out
  relay), the trust model (per-user node accounts; identity gates the session,
  the OS gates the filesystem), and the build order.
