# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this project follows
[SemVer](https://semver.org).

This project implements an external specification. Our version is independent of
the protocol version — see [`UPSTREAM.md`](UPSTREAM.md) for which protocol
versions each release speaks.

## [Unreleased]

### Added

- **`Denied("reason")`, returnable from any `may_*` policy hook**, so a refusal can
  carry the message a person actually reads. It is **falsy**, so every existing
  `if not policy.may_x(...)` branch and every policy returning plain `False` is
  unaffected — this is additive. Wired at the two refusals a client renders as a
  UI error: `createTerminal` and `createSession`. The terminal one is why it
  exists — VS Code renders a refused terminal as *"The terminal process failed to
  launch: Not permitted to create a terminal"*, which reads as a crash in the host
  rather than as a host that does not offer terminals. It does not close the
  underlying gap (a client cannot *learn* a host has no terminals; see
  `docs/deferred-upstream.md` U1), it just stops the message lying about whose
  fault it is.

- **`runs_commands` on a terminal backend** (feature-detected, defaults True) now
  gates whether the host advertises the `!command` prefix. The class check alone was
  not enough: a host may install a backend that deliberately executes nothing — to
  satisfy a client that opens a terminal unconditionally and explain itself in the
  panel rather than refusing and producing an error toast on every window focus.
  Advertising `!` for such a backend turns a working input into a dead end, which is
  the same reason the refusing default does not advertise it.

### Documentation

- **`docs/deferred-upstream.md`** — measured defects belonging to the spec or to VS
  Code, held until these repos are public. Seeded with U1 (no terminal capability
  in `AgentCapabilities`) and U2 (VS Code lowercases the `b64-` filesystem
  authority, which is case-sensitive base64, breaking every agent-host file open
  for every possible host address).

### Fixed

Found by driving the sibling Python client against this host — two
implementations built independently from the same spec, meeting for the first
time. All four needed a *second* chat or a *second* session to see, which is
why a suite that had only ever run one of each was green through them:

- **Cancelling one chat destroyed another.** The running-turn handle was one
  slot per session, overwritten per chat, so `chat/turnCancelled` killed
  whichever chat had started most recently — with no terminal action on the
  victim, which was then pinned at `activeTurn` forever and rejected every
  later turn. Unrecoverable short of disposing the session.
- **`createChat` with an `initialMessage` wedged the new chat from birth.** The
  turn was kicked with a stub action carrying no `turnId`, so the runner
  returned on its first line: the agent never saw the message and the chat
  could never be used. `createChat` answered `{}`.
- **One chat could answer another's question.** Pending-request ids are minted
  globally and the gate checked only that the id was live, so a misdirected
  `chat/inputCompleted` resolved the victim's turn while reading the answers
  out of the wrong channel — the answers reached nobody and the victim stayed
  in `InputNeeded` until disposal. Both the input and tool-call gates are now
  channel-scoped.
- **`createResourceWatch` escaped the filesystem jail.** Strict ancestors of
  the served root are resolvable so a directory picker can walk to them, and
  every other surface refuses them — but the watch path never applied the jail
  check, so a recursive watch on an ancestor (up to `file:///`) reported names,
  existence and change timing for files the same peer is refused a read of.

Then the rest of the interop list. The pattern in nearly all of them: a frame
that looks well-formed, which the *reducer* refuses, and which the host acted
on anyway — so the provider and every client ended up disagreeing about what
happened, silently.

**Resources** (all behind `writable=True` or an installed watcher):

- `resourceWatch/changed` emitted `created`/`changed` where the closed enum is
  `added`/`updated`/`deleted`, so a validating client saw only deletions.
- A watch rooted at a single *file* never reported anything, and a watch that
  was created but never subscribed was never released.
- A failed `ifMatch` write created the target before failing; an unrecognised
  `mode` truncated the file instead of failing; a negative `position` either
  NUL-padded past EOF or leaked a raw `OSError`.
- `resourceRead` had no size cap — a 64 MiB file drove host RSS from 31 MB to
  970 MB. Now bounded and configurable.
- `resourceRequest(write=true)` was granted by a read-only host that then
  denied every write.

**Tool calls:**

- The auto-confirming `chat/toolCallReady` omitted the required
  `invocationMessage` and dropped `toolInput`, nulling everything the provider
  had streamed at the moment the call was confirmed. `toolInput` was also
  published on `chat/toolCallStart`, which has no such field — between them, no
  non-confirming tool ever showed its input.
- Both streaming sink methods were no-ops in the mode the host itself used
  them: `tool_call_output` before confirmation and `tool_call_delta` after.
- A client tool-call action missing a schema-required field was broadcast
  instead of being echoed with a `rejectionReason`.

**The server→client direction**, which upstream's own reference client ships
with zero implementations, so nothing else exercises it:

- A `chat/toolCallConfirmed` aimed at a park waiting for a *result* resolved it
  anyway, so a refusal reached the provider as `ToolResult(value={})` — an
  agent told "the editor will not do that" reported an empty success. Parks now
  carry their kind and only the matching action answers them, and
  `ToolResult.response` distinguishes a refusal from an empty result.
- `chat/toolCallComplete` was accepted from a client that did not own the call.
- A parked client tool was never failed when its owner disconnected **or
  removed itself** — `session/activeClientRemoved` was not even routed — so the
  session advertised a tool nobody could run, stayed pinned at `InputNeeded`,
  and the park outlived `disposeSession`.

**Sessions, changesets, terminals and the handshake:**

- Changeset operations stayed `disabled` for the life of the session, and
  `invokeChangesetOperation` never checked the operation was one the changeset
  declared — which also disarmed every scope and target check.
- Cancelling a `!command` turn leaked the child shell past `Host.aclose()`.
- `RootState.terminals` went stale after a title change or a claim.
- The `-32005` error data used `supportedProtocolVersions`; the schema says
  `supportedVersions`. This is the one frame a client reads to tell a user
  which versions to install.
- A repeated URI in `reconnect.subscriptions` multiplied every replayed
  envelope, corrupting chat text and amplifying a 50 KB request into 62 MB.
- `chat/turnCancelled` was accepted with a `turnId` naming no active turn.
- `disposeSession`/`disposeChat` during a turn published nothing terminal, so a
  subscribed client's stream hung forever.

**And one nothing found by interop at all**, in the WebSocket transport:
`receive()` recursed on every malformed frame. Measured: 100 junk frames were
fine and 5000 raised `RecursionError` inside the read task — a remote crash
from unauthenticated input. Now a loop with a bounded run of unusable frames.

### Fixed — a three-repo conformance review against the pin and VS Code

An adversarially-verified review of the host against the vendored `spec/v0.7.0`
sources, the spec prose and VS Code's client. Each fix is pinned by a test.

- **Outbox overflow never actually closed the connection.** The close sentinel
  was enqueued into a queue that was by definition full, so `put_nowait` always
  raised and was suppressed — the writer never learned it should stop. The
  sentinel now evicts the oldest frame to guarantee itself a slot, a
  non-`TransportClosed` send failure closes the connection instead of silently
  skipping one frame, and `close()` tears the writer down.
- **`authenticate` refused resources the host itself had asked about.** Only
  the static `AgentInfo.protectedResources` list was accepted, so a token for a
  resource advertised through a live `chat/toolCallAuthRequired` or MCP
  `authRequired` challenge — the spec's step-up flow — bounced. And a pushed
  token resolved **every** parked challenge regardless of resource; it now
  wakes only the calls whose challenge named that resource.
- **`createSession.activeClient.clientId` was never checked** against the
  connection's clientId, though the pin says it MUST match; a mismatch is
  `-32602` now. `provider: ""` no longer resolves as "absent" (`-32002`).
- **Four commands answered `{}` where the pinned `CommandMap` says `null`**:
  `createTerminal`, `disposeTerminal`, `createChat`, `disposeChat` — the
  session pair already answered `null`, so the host disagreed with itself.
- **`terminal/commandFinished` published a schema-invalid explicit
  `exitCode: null`** when the shell reported none, and the unconditional spread
  wrote it into `TerminalCommandPart` where it persisted in snapshots. The
  field is omitted now, which the reducer's delete-on-`undefined` respects.
- **Disposing a session dropped its changeset channels with no terminal
  action**, leaving subscribers rendering the last file list forever;
  `changeset/cleared` is published first. A duplicate chat URI is refused with
  `AlreadyExists` (`-32010`), not `SessionAlreadyExists` and a message naming a
  session that does not exist.
- **A refused `reconnect` still marked the connection initialized** (and
  audited `connection.resumed`); the snapshot arm of `reconnect` carried an
  undeclared `missing` member the pinned `ReconnectSnapshotResult` does not
  define — it is replay-arm only now, and the sibling client was verified to
  read it only there.
- **Terminal output between process spawn and channel registration was
  silently dropped** — buffered and published in order now.
- **`Host(claim_gated_actions=…)`** makes `STRICT_CLAIM_GATED_ACTIONS` wirable,
  as the terminals module docstring had promised without a seam; the local
  `-32005` emitter workaround was retired in favour of the shared package's
  now-correct `supportedVersions` field.

### Fixed — tests

- **The pty backend's controlling-terminal test could only ever fail**, so a
  clean `pytest` on any POSIX host reported one failure. It asserted the literal
  string `/dev/tty` in the output of `tty`, which prints the *device* it is
  attached to (`/dev/pts/N` on Linux, `/dev/ttysNNN` on macOS) and never that
  string. It now checks the two things that actually separate a controlling
  terminal from none — a zero exit status and a real device name — and was
  verified to fail all three assertions against a child spawned without a pty.
  Test-only; the backend itself was correct.

### Changed

- **The protocol layer is now a separate package.** Wire types, the seven
  reducers, the transports and the vendored conformance corpora moved to
  [`agent-host-protocol`](https://github.com/jackhumbert/agent-host-protocol-py),
  which this package depends on (`~=0.1.0`, tight because the spec lands
  breaking changes in MINOR bumps). The reason is a Python *client*: the
  1,161-line chat reducer has to exist exactly once, and a fork with
  drift detection makes drift *detectable* rather than impossible.

  Embedders importing `agent_host_server.types`, `.reducers`, `.transport`,
  `.core.errors`, `.core.channels` or `.core.versions` should import
  `agent_host_protocol.…` instead. Everything re-exported from
  `agent_host_server` itself is unchanged.

  Bumping the spec pin is no longer a change to this repository.

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
