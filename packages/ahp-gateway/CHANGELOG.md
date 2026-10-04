# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning is [SemVer](https://semver.org/), independent of the protocol's.
Every release states the protocol versions it speaks.

## [Unreleased]

Under construction. `docs/plan.md` is the design and its §7 is the build order.

### Changed

- **Speaks AHP 1.0.0** through `ahp-protocol`'s `spec/v1.0.0` pin. A malformed
  entry in `protocolVersions` is refused `-32602` instead of being skipped.
- **Renamed from `agent-host-broker` to `ahp-gateway`** (import `agent_host_broker` → `ahp_gateway`), and moved into the `ahp-py` monorepo as `packages/ahp-gateway`. "Broker" is "gateway" throughout, including the root-state `_meta` key: `agent-host-broker/nodes` → `ahp-gateway/nodes`. Tags are now per package: `ahp-gateway/v<version>`.
- **File URIs reach the surfaces as `ahp-file`, not `file`.** `ahp-file:///<node>/<rel>`
  is a path under the node's root (its `defaultDirectory`), and
  `ahp-file://<node>/<path>` anything outside it. `file:` is left to mean the
  client's own machine. Replaces `file://<node>/<path>`.
- A shared provider id across nodes is one agent offering every node's models;
  the working directory picks the node.

### Added

- **The orchestrator** (`Gateway(orchestrator=OrchestratorConfig(...))`,
  `Gateway.start()` / `aclose()`). An `orchestrator` agent in the merged list
  whose sessions run a real agent with fleet tools: list machines and folders,
  start sessions, then message, read, wait for and stop the sessions it
  started. The gateway runs the tools over supervised links of its own, one
  per (principal, node), and rejoins after a restart or a dropped link.
  Workers run with a fixed config (default `permissionMode: auto`). Nothing
  answers their approvals, the orchestrator can only drive sessions it
  started, and `max_running` caps how many run at once. See `docs/plan.md`
  §10.
- **Automations across machines.** The gateway merges every node's
  `ahp-automations://` catalogue into one and advertises `automations` when
  any node hosts them. A new automation goes to the machine its folder names
  (else one running its agent, else the first that hosts automations); edits,
  runs, history paging, run channels and cancellation follow the automation's
  owner. See `docs/plan.md` §9.
- **Each machine's own view, in its node-list entry.** A connected node's
  entry under `ahp-gateway/nodes` now also carries its `serverInfo`,
  its root `_meta` (as `meta`) and its root `config`, verbatim. The merge
  keeps none of these - `config` is still never advertised at the root
  (invariant 4) - so a host's extensions (a copilotd's per-process sealing
  keys in `_meta.copilot.encryptionKeys`, its host name and projects in
  `config.values.copilot`) previously vanished behind the gateway. Carried,
  not interpreted.
- **Jitter on node redial.** `Gateway(redial_jitter=0.25)` spreads each redial
  delay across +/- that fraction, so one node's recovery does not bounce
  every surface into reconnecting against every node at the same instant.
- **The machines behind the gateway, in `RootState._meta`.** Under
  `ahp-gateway/nodes`, one entry per admitted node: `id`, `label` (the
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
  `origin`, stamped from the gateway's `serverSeq`. It used to be dropped
  silently, leaving the surface's optimistic prediction applied.
- **A machine that is off no longer costs every connection 10 seconds.** The
  handshake waits for its slowest node, and a node that never answers held
  every `initialize` and `reconnect` for the whole `connect_timeout`. The
  gateway now remembers which nodes failed their last dial, from any
  connection, and gives those only `Gateway(known_down_timeout=1.0)` at the
  handshake. The background redial still allows the full timeout, and a node
  that answers is no longer marked down. Measured on the deployed gateway with
  one machine off: every handshake took 10.07 s. The log line for a failed
  dial now names the error (`TimeoutError()`), where it printed nothing.
- **`authenticate` works with more than one node.** It names only a resource,
  so the gateway refused it ("cannot tell which node authenticate is for"),
  leaving an agent that signs in through it (VS Code's Copilot) unusable in a
  fleet. It now goes to every node whose agents advertise that resource, or to
  every node when none does, and succeeds if any accepts.

### Added

- **The multiplexer** (`ahp_gateway.core.Gateway`): one AHP endpoint for
  the surfaces, one `AhpClient` link per admitted node. It routes requests by
  channel owner, file-URI authority or provider; merges `listSessions` and the
  root channel across nodes; restamps actions onto one `serverSeq`; and relays
  host-initiated requests back to the surface. File URIs are shown to the
  surfaces as `file://<node>/path`. Speaks AHP 0.7.0 and 0.6.0.
- **The registry**: `NodeRecord`, `Principal`, and a declarative
  `StaticInventory` with group-based admission (closed by default).
- **WebSocket edges**: `WebSocketNodeConnector` dials nodes with the client
  sibling's transport, and `serve_gateway` serves the surfaces with the server
  sibling's WebSocket server.

- **Reconnect**: answered with the snapshot arm from fresh node reads, so a
  surface can reconnect to any gateway instance. Channels are found by asking
  the nodes when no node has named them yet.
- **Node recovery**: a lost or initially unreachable node is redialed with
  backoff, and the surface is bounced once it answers, so it resyncs from
  fresh snapshots. A lost node's channels are refused, never rerouted.

### Changed

- `ahp-client` is pinned `>=0.1.0.dev0,<0.2` until it releases 0.1.0.
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
- `ahp-file:///`, answered by the gateway: one directory per connected node, and
  the surfaces' `defaultDirectory` when more than one is connected.
- Plain chats and pre-folder session settings go to the first connected node
  offering the agent, instead of being refused as ambiguous.
