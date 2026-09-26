# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning is [SemVer](https://semver.org/), independent of the protocol's.
Every release states the protocol versions it speaks.

## [Unreleased]

Under construction. `docs/plan.md` is the design and its §7 is the build order.

### Changed

- **File URIs reach the surfaces as `ahp-file`, not `file`.** `ahp-file:///<node>/<rel>`
  is a path under the node's root (its `defaultDirectory`), and
  `ahp-file://<node>/<path>` anything outside it. `file:` is left to mean the
  client's own machine. Replaces `file://<node>/<path>`.
- A shared provider id across nodes is one agent offering every node's models;
  the working directory picks the node.

### Added

- **Each machine's own view, in its node-list entry.** A connected node's
  entry under `agent-host-broker/nodes` now also carries its `serverInfo`,
  its root `_meta` (as `meta`) and its root `config`, verbatim. The merge
  keeps none of these - `config` is still never advertised at the root
  (invariant 4) - so a host's extensions (a copilotd's per-process sealing
  keys in `_meta.copilot.encryptionKeys`, its host name and projects in
  `config.values.copilot`) previously vanished behind the broker. Carried,
  not interpreted.
- **Jitter on node redial.** `Broker(redial_jitter=0.25)` spreads each redial
  delay across +/- that fraction, so one node's recovery does not bounce
  every surface into reconnecting against every node at the same instant.
- **The machines behind the broker, in `RootState._meta`.** Under
  `agent-host-broker/nodes`, one entry per admitted node: `id`, `label` (the
  record's `metadata["label"]`, else the id), `folder` (`ahp-file:///<id>/`),
  `connected`, and the provider ids of the `agents` it runs. AHP has no notion
  of a machine, and a stock client needs none - an agent offered on several
  machines is one agent and the folder picks the machine - but a client can
  now say "Claude on studio", group by machine, and offer only the agents a
  folder's machine runs. Sent in the root snapshot only (there is no root
  action for `_meta`); the list is fixed for a connection's life.

### Fixed

- **A folder on another machine is refused out loud.** A `dispatchAction`
  naming another node's file (say a `session/workingDirectorySet` of a folder
  on node B, on a session owned by node A) is still not relayed, but is now
  echoed back to the dispatching surface with a `rejectionReason` and its
  `origin`, stamped from the broker's `serverSeq`. It used to be dropped
  silently, leaving the surface's optimistic prediction applied.
- **A machine that is off no longer costs every connection 10 seconds.** The
  handshake waits for its slowest node, and a node that never answers held
  every `initialize` and `reconnect` for the whole `connect_timeout`. The
  broker now remembers which nodes failed their last dial, from any
  connection, and gives those only `Broker(known_down_timeout=1.0)` at the
  handshake. The background redial still allows the full timeout, and a node
  that answers is no longer marked down. Measured on the deployed broker with
  one machine off: every handshake took 10.07 s. The log line for a failed
  dial now names the error (`TimeoutError()`), where it printed nothing.
- **`authenticate` works with more than one node.** It names only a resource,
  so the broker refused it ("cannot tell which node authenticate is for"),
  leaving an agent that signs in through it (VS Code's Copilot) unusable in a
  fleet. It now goes to every node whose agents advertise that resource, or to
  every node when none does, and succeeds if any accepts.

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

- **Reconnect**: answered with the snapshot arm from fresh node reads, so a
  surface can reconnect to any broker instance. Channels are found by asking
  the nodes when no node has named them yet.
- **Node recovery**: a lost or initially unreachable node is redialed with
  backoff, and the surface is bounced once it answers, so it resyncs from
  fresh snapshots. A lost node's channels are refused, never rerouted.

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
- VS Code's spelling of the tree, `file:///<node>/...` (it browses a host as
  `file:` from the path of its `defaultDirectory`), is accepted inbound as
  that node's `ahp-file` URI; `file:///` is the list of nodes for resource
  commands.
- `ahp-file:///`, answered by the broker: one directory per connected node, and
  the surfaces' `defaultDirectory` when more than one is connected.
- Plain chats and pre-folder session settings go to the first connected node
  offering the agent, instead of being refused as ambiguous.
