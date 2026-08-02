# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this project follows
[SemVer](https://semver.org).

This project implements an external specification. Our version is independent of
the protocol version — see [`UPSTREAM.md`](UPSTREAM.md) for which protocol
versions each release speaks.

## [Unreleased]

## [0.1.0] - 2026-08-02

The first release. It speaks protocol versions **0.7.0 and 0.6.0**, answers all
29 client→host commands plus the reverse `resource*` direction, and is driven by
the real VS Code client and the published TypeScript client in CI.

What it is: a library you embed, and a demo host that exercises every surface so
you can see what a client does with each one. What it is not: an agent. There is
no model in here — you write a provider, and routing is yours.

### Added — the protocol surface

- **All 29 commands**: `initialize`, `ping`, `subscribe`, `unsubscribe`,
  `reconnect`, `listSessions`, `createSession`, `disposeSession`, `fetchTurns`,
  `dispatchAction`, `createChat`, `disposeChat`, `createTerminal`,
  `disposeTerminal`, `createResourceWatch`, `invokeChangesetOperation`,
  `resolveSessionConfig`, `sessionConfigCompletions`, `completions`,
  `authenticate`, and the nine `resource*` methods — which also run
  **host→client**, for clients that serve their own filesystem.
- **All seven reducers**, hand-ported, with **all 247 upstream corpus fixtures
  passing** plus a per-fixture non-mutation assertion the JSON fixtures cannot
  express.
- Version negotiation over a **set** of supported versions rather than the
  reference host's single-MINOR model, so one build serves both VS Code and the
  published npm client (ADR 0002).
- Sessions, chats and turns; multi-chat with the aggregation rules
  (`session/inputNeeded`, unread, activity) that nothing else in the stack
  enforces; `createSession.fork`; tool calls with confirmation, client-executed
  tools, and elicitation.
- **`terminalCommandPrefix`.** `!command` runs in a one-shot terminal claimed
  by the session and is reported as a tool call. It was advertised and acted on
  nowhere, so the input box promised a shortcut that silently went to the agent.
- **A chat catalogue that keeps up.** `SessionState.chats[]` is mirrored from
  the chat channels, so chat tabs show real titles, statuses and timestamps
  instead of whatever they were created with; and the default chat is called
  "New Chat" rather than being given the session's name.
- **Turn fidelity.** Queued follow-ups are consumed as soon as the chat goes
  idle instead of sitting in their chip forever; response parts are segmented by
  kind, so prose written after a tool call renders below it; `chat/usage` makes
  the client's context gauge exist at all; and `chat/toolCallDelta` /
  `chat/toolCallContentChanged` let a slow tool show progress rather than a
  static row followed by everything at once.
- **A session list that moves.** Sessions are named from the user's first
  message rather than all being called "New Session"; `session/activityChanged`
  carries the running tool's name instead of the client's "Working..."
  fallback; the host clears `session/isReadChanged` when the agent answers, so
  the unread dot comes back; and a side chat that is *working* promotes the
  session summary, not only one that is blocked or errored.
- The `resource*` family with a **jail that walks** — `openat` with `O_NOFOLLOW`,
  one component at a time, each resolved symlink re-checked against the root.
  Deliberately not realpath-then-open, which has a swap window; there is a test
  for the race.
- Resource watches with a lifetime and a budget; changesets, including content
  the filesystem no longer has; terminals, with a real pty backend behind
  `--terminal`.
- Session configuration, customizations, completions, `root/progress`, and the
  MCP surface with 0.6.0 step-up authentication.

### Added — the runtime

- **A single global sequencer.** `serverSeq` assignment, reducer application,
  replay append and fan-out all happen inside one critical section, with one
  outbound queue and one writer task per connection.
- Durable `serverSeq` across restarts, per-channel replay budgets, and a
  reconnect that tells a client **what it lost** rather than silently resuming.
- A durable session store, with an embedder-owned `metadata` mapping carried
  verbatim — so a restored session can still say who owns it.
- A bounded outbox (2048 frames, configurable). A peer that stops reading is
  disconnected rather than accumulating frames in host memory forever.
- `Host.counters()` for liveness, and a worked readiness example in the guide.

### Added — the embedder surface

- `Policy` — required, with no default. It is consulted on connection, on every
  channel it can see, and on session creation and restore, and it is told when a
  channel is created or dropped, which is the moment ownership becomes
  expressible.
- `AgentProvider` / `AgentSession` / `TurnSink` as `Protocol`s: if your object
  has the methods it is one, and there is nothing to subclass.
- One primitive for provider requests that wait on a client — elicitation, tool
  confirmation and client tool execution are the same shape (ADR 0005).
- `python -m agent_host_server`, a demo host with a flag per surface, which
  prints the VS Code settings block to paste.

### Added — foundations

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
- Client-dispatch gating from the generated table, the normative action
  validation rules, and `rejectionReason` echoes.
- The neutral provider interface and the offline `EchoProvider` (ADR 0003).
- Interop tests driving the real `@microsoft/agent-host-protocol` client over a
  socket, including feeding our action stream through the **official TypeScript
  reducers** and diffing against a fresh snapshot.

### Security

- **`chat/truncated` is refused unless the agent can actually forget.** The
  reducer drops the turns whatever the provider does, so edit-and-resend looks
  right while the agent goes on remembering — the user is shown a conversation
  being rewound that was not. Implement `TruncatesHistory` to opt in. Stricter
  than the spec, deliberately: a visible refusal beats a silent lie.
- **Every dangerous surface is off by default.** No filesystem access without a
  resource provider; no writes without a second, separate `writable=True`; no
  command execution without a terminal backend, which is constructed by name and
  never arrives by upgrading.
- The WebSocket server binds loopback-only unless `allow_remote=True`, with an
  optional bearer token rejected at the upgrade.
- The wire log redacts credentials and is written owner-only.
- [`SECURITY.md`](SECURITY.md) states the posture, the scope, and the
  disclosure path.

### Fixed

Findings from driving the real VS Code client, all of which failed **silently** —
the client renders nothing it does not recognise, so a wrong shape and an absent
feature look identical:

- `SessionStatus` constants were bound to the wrong values (the set was right but
  shifted a position), so new sessions reported `Error` instead of `Idle`.
- Six wire shapes a client silently dropped, and a further six sequencer and
  reducer defects found by differential audit against the reference.
- A failed turn rendered `Error: (undefined) …` — `errorType` is required.
- A tool call that went straight from start to completion was **silently
  dropped** and cancelled when the turn ended — the shape a first provider has,
  missing only the `chat/toolCallReady` transition that the confirmation and
  client-tool paths happened to publish for their own reasons.
- `changeKind` is the changeset's *identity* in the client, so two changesets
  sharing one kind collapse into one.
- A changeset is a **record** of changes already made, not a proposal: clients
  open `after.uri` and expect a file there.
- `createSession` could seize the connection-level channel.
- A reconnecting client was never told what it had missed.
- Terminals did not announce their exit, waited instead of hanging up, and leaked
  shells: interactive shells ignore `SIGTERM`, so disposal sends `SIGHUP` to the
  process group first. Measured 3.00s → 0.00s.
- Changeset operations that could not work were still offered, failures were
  invisible, and a republish wiped every `reviewed` tick.

### Documentation

- [`docs/guide/writing-a-provider.md`](docs/guide/writing-a-provider.md) and
  [`docs/guide/deploying.md`](docs/guide/deploying.md) — **every example in both
  is executed by the test suite**, so they cannot drift.
- The README's command list is derived from the dispatcher and its flags from
  `--help`, both asserted rather than maintained.
- Every published frame is validated against the vendored upstream JSON schemas.
- ADRs 0001–0005, the research log, the experiment log, the roadmap, and
  [`UPSTREAM.md`](UPSTREAM.md) recording which protocol versions we speak.

### Known limitations

- Some VS Code surfaces cannot be driven by any host: model-generated session
  titles, checkpoints, plan review, and `SessionState.serverTools` (rendered by
  nothing in any build). Picking a *local* folder in the Agents window will never
  offer a remote host — the two `resolveWorkspace` gates are mutually exclusive.
  Browse through the host's own picker instead; the guide explains why.
- Requires POSIX for the pty backend. Everything else is portable.


[Unreleased]: https://github.com/jackhumbert/agent-host-server-py/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/jackhumbert/agent-host-server-py/releases/tag/v0.1.0
