# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this project follows
[SemVer](https://semver.org).

This project implements an external specification. Our version is independent of
the protocol version — see [`UPSTREAM.md`](UPSTREAM.md) for which protocol
versions each release speaks.

## [Unreleased]

### Changed

- **Renamed from `agent-host-server` to `ahp-host`** (import `agent_host_server` → `ahp_host`), and moved into the `ahp-py` monorepo as `packages/ahp-host`. Commands: `agent-host-server` → `ahp-host`, `agent-host-node` → `ahp-node`; the node's launchd label is `io.ahp.node`, and example configs live under `~/.config/ahp/`. Tags are now per package: `ahp-host/v<version>`.

### Added

- `FollowsActiveClients.active_clients_changed(clients)`: a provider hears the
  session's whole `activeClients` list after `session/activeClientSet`,
  `session/activeClientRemoved`, a client disconnecting, and a restored session
  getting its agent back. Without it an adapter knew only the creator's tools
  (`AgentSessionContext.client_tools`), and a restored session knew none.
- **Automations** (protocol 0.9.0), behind `Host(automations=...)`:
  the `ahp-automations://` catalogue and `ahp-automation-run:` channels,
  `automation/createRequested` / `updateRequested` / `removed`,
  `automationRun/cancelRequested`, and `runAutomation`,
  `fetchAutomationRuns` and `listAutomationTriggerDefinitions` - which until
  now were declined. Each run creates a session from the definition's
  template, stamped with `SessionMetadata.origin`, and sends the saved message
  as its first turn; the run completes, fails or is cancelled as that turn
  does. Schedule triggers use AHP's five-field cron in a named time zone,
  evaluated by a scheduler the host starts itself, with `misfirePolicy`
  honoured across restarts. Event triggers are refused: this host defines no
  event types. `FileAutomationStore` and `InMemoryAutomationStore` in
  `ahp_host.core`; `Host.run_due_automations(now)` drives schedules
  by hand. Off without a store, which is what an absent `automations`
  capability means.
- `ahp-node` keeps automations under `<state_dir>/automations`.
- On Windows, a dependency on `tzdata`: `zoneinfo` needs a time-zone database
  and Windows has none.
- `SessionSummary.origin` in `listSessions` and `root/sessionAdded`, for a
  session an automation created.
- `OpensSessions.attach_directory(directory)`: a provider that lists
  sessions of its own accord (Claude Code sessions started elsewhere, on
  claude.ai) is given a `SessionDirectory` - `open`, `close` and `uris`,
  scoped to that provider - once `Host.restore()` has brought back what was
  saved. Until now only an embedder holding the `Host` could open a session,
  and an agent plugged into `ahp-node` never holds it.
- `FollowsWorkingDirectories.working_directories_changed(directories)`: an
  agent session that implements it is told the session's whole folder set
  after a client adds, removes or replaces one
  (`session/workingDirectorySet` / `Removed` / `Replaced`, accepted only for
  an agent advertising `multipleWorkingDirectories`), and the session is then
  saved. Until now a folder added to a running session reached state and
  every client, and never the agent.
- `ArchivesSessions.archived_changed(is_archived)`: an agent session that
  implements it is told when a client archives or unarchives it
  (`session/isArchivedChanged`), and the session is then saved. For an agent
  whose session also lives elsewhere (Claude Code on claude.ai), which can
  file it away there too.
- `DisposesSessions.disposed()`: an agent session that implements it is told
  when it is being deleted (`disposeSession`, `Host.close_session`), before
  `aclose`. `aclose` alone also runs at shutdown, and an agent whose session
  lives somewhere else too (Claude Code on claude.ai) must keep it on one and
  end it on the other.
- `SessionPublisher.config_changed(values)`: the provider-side twin of a
  client's `session/configChanged`, for an agent whose setting moved
  somewhere else (Claude Code's permission mode, switched on a phone under
  Remote Control). Published, merged and saved like a client's change; the
  provider is not called back with it.
- `TurnSink.tool_call_confirmed(call_id, approved=, reason_message=)`: for an
  agent that puts the same approval to this host and to somewhere else (Claude
  Code under Remote Control asks a phone too). When the other side answers
  first, the provider cancels its `confirm_tool_call` and reports the answer
  here; the host withdraws the prompt and its `session/inputNeeded` entry and
  publishes `chat/toolCallConfirmed`. Before, every client kept an approval
  prompt for a call that was already running until the turn ended. A no-op if
  a client here answered first.
- One host can serve several agents: `Host` takes a list of providers as well
  as one. `RootState.agents` lists them all, in order; each session is served
  by the agent it was created with; the first is the default, both for a
  `createSession` that names no provider and for a restored session whose
  provider id this host no longer serves (so renaming an agent keeps its old
  sessions working). `Host.providers` maps id to provider; `Host.provider` is
  still the default. `open_session` takes `provider_id`.
- `ahp-node` (`python -m ahp_host.node`): a machine's node --
  one port, folder tree, session store and token -- serving every agent listed
  in its config's `[[agents]]` tables. Agent packages plug in through the
  `ahp_host.agents` entry-point group (`create(options, NodeContext)`);
  `type = "echo"` is built in. The folder tree (`[roots]`, named or single)
  moved here from ahp-host-claude as `ahp_host.node.roots`.
- `ahp-node supervise | install | uninstall`: keeping a node running
  without wrapper scripts. `supervise` runs the node as a child and restarts
  it (and the config's `tunnel` command, e.g. an `ssh -N -R` to a gateway)
  with backoff, logging each exit to `<log_file>.supervisor.log`. `install`
  starts the supervisor at login as the current user -- a Scheduled Task
  running `pythonw.exe` directly on Windows (no console, no PowerShell), a
  launchd agent with `KeepAlive` on macOS. `log_file` (and `--log-file`)
  logs to a rotated file, which a windowless `pythonw` needs.
- Steering: `SteersTurns.steer(chat_uri, message) -> bool` offers a chat's
  steering message to the turn already running. Taken, it is removed from the
  chat and noted in the transcript as a `systemNotification` part with
  `_meta.steering`; not taken, or set while idle, it runs as the next turn
  (ahead of queued messages) instead of sitting in the chat forever. The echo
  agent can be steered while it streams.
- Sessions that live somewhere else can be mirrored here. `Host.open_session`
  lists and serves a session no client created (its agent comes from
  `resume_session`, and it persists and restores like any other);
  `Host.close_session` removes it. `SessionPublisher.external_turn` shows a
  turn that started elsewhere -- say, typed on another device -- as a turn in
  the chat, cancellable like any other, and `SessionPublisher.title_changed`
  renames the session.
- `ReconfiguresSessions.config_changed(values)`: a session hears about a
  `sessionMutable` property a client changed mid-session
  (`session/configChanged`), after the host validated and applied it. The
  echo agent's `prefix` now actually changes its replies.

### Fixed

- **Named roots accept a plain absolute URI inside a root.** A node with
  named roots took only its tree spelling (`file:///llm/...`), so a session
  stored with `file:///G:/llm` -- from before the roots were named -- failed
  every start-up as "outside this host's root". `Roots.real_path` now also
  takes an absolute URI, accepted only once resolved inside a root, and the
  named-roots resource provider reads it through that root's jail.
- **`ahp-node install` on Windows runs at normal priority** (4). Task
  Scheduler's default, 7, also lowers I/O priority, and a supervisor started
  at logon sat in an I/O wait for minutes before starting anything.
- The `toolClientExecution` entry a client tool call adds to
  `SessionState.inputNeeded` now carries the call's `contributor`, which the
  spec requires. A client not subscribed to the chat reads that entry to find
  its own calls, and without the `contributor` it never ran them.
- A client tool that ran and reported `success: false` reached the provider as a
  refusal with no reason. `ToolResult.reason` now carries the result's text, or
  its `error.message`.
- A turn started by `SessionPublisher.external_turn` carries the
  `Message.origin` the schema requires (`{"kind": "user"}`). Without it,
  strict clients could not decode the chat at all (the iOS client failed its
  `subscribe`). Turns saved before the fix get the same origin on restore.
- A `createSession` that names no working directory gets none, instead of the
  served root (`default_directory`). The root is still the fallback for a
  client that asked for folders the jail refused (VS Code's `file:///`), which
  is what it exists for; a client that names none is asking for a plain chat,
  and the agent decides what that may touch.
- `session/workingDirectorySet` and `...Replaced` answer to the same jail as
  `createSession`: a folder outside the served ones is rejected, where before
  only the policy was asked.
- Restored sessions could never take a turn: the host did not resume their
  agents (every turn failed `provider.resumeSession`) and never asked a
  `ResumableAgentProvider` for its resume state, so nothing was stored to
  resume from. It now captures `resume_state_of` whenever it saves a session,
  and resumes a restored session's agent on its first turn with its stored
  resume state and the config values its state holds now. The echo agent is
  resumable.
- A restored session's config schema is refreshed from the provider, keeping
  its values, so a property made `sessionMutable` (or relabelled) since the
  session was created becomes changeable on old sessions too.
- On Windows the host silently dropped every working directory a client picked
  under the served root (and fell back to the root itself): its check compared
  POSIX paths, so `G:\llm\proj` was never "under" `G:\llm`. Jails now answer
  `serves(uri)` themselves - the Windows one drive-aware and case-insensitive -
  and the host asks them.


### Added — Windows

- **`RootedFilesystemResourceProvider` works on Windows, read-only.** It used to
  construct and then fail at first use, because Windows' Python has no `dir_fd`
  and no `O_NOFOLLOW`. On `win32` the same class now builds a separate jail
  (`core/resources_windows.py`) that walks NT handles with `NtCreateFile`
  relative opens and `FILE_OPEN_REPARSE_POINT`, holds components without
  `FILE_SHARE_DELETE`, verifies every handle with `GetFinalPathNameByHandleW`,
  and checks the root by file identity. `resourceList`, `resourceRead`,
  `resourceResolve` and the strict-ancestor chain are served; symlinks and
  junctions are followed only while they stay inside the root; `..`, alternate
  data streams, device names, trailing dots/spaces, `\\?\` and UNC spellings
  are refused. It accepts and produces `file:///C:/…` URIs. `writable=True`
  raises `ValueError` on Windows. POSIX behaviour is unchanged. Limits are in
  `SECURITY.md`.
- A `windows-jail` CI job attacks it on `windows-latest` with real junctions,
  symlinks and a racing swapper; the rest of the suite runs there informationally.

### Changed — protocol 0.9.0

- **Depends on `ahp-protocol` at `spec/v0.9.0`, and offers `0.9.0`,
  `0.8.0`, `0.7.0` and `0.6.0`, preferring `0.9.0`.** The interop suite drives
  `@microsoft/agent-host-protocol@0.9.0` from npm, negotiates `0.9.0`, and the
  official 0.9.0 reducers agree with the host's state.
- **A failed turn publishes its error as an `ErrorResponsePart`**
  (`chat/error.part`), so it stays in the transcript. It is never marked
  `resumable`, and a client's `chat/turnResume` is rejected so the client
  reverts rather than showing a reopened turn nothing is running.
- **Terminals carry a lifecycle.** New terminals start `{status: "running"}`,
  `terminal/exited` moves them to `exited`, and the root catalogue's
  `TerminalInfo` carries `lifecycle` in place of `exitCode`.
- **Session terminal claims name their chat.** `TerminalSessionClaim` takes a
  required `chat` (second positional field), the `!command` terminal claims
  its chat, and a wire claim without one is not a claim.
- **The session's client-execution entry is a full `ToolCallRunningState`**,
  adding the `invocationMessage` and `confirmed` it was missing.
- `createSession`'s session-level `fork`, removed from the 0.9.0 params, is
  still honoured for the 0.7.0 and 0.8.0 peers that send it.
- The three automation commands are declined with `-32601`: this host does not
  host automations.
### Changed — protocol 0.8.0

- **Depends on `ahp-protocol` at `spec/v0.8.0`, and offers `0.8.0`,
  `0.7.0` and `0.6.0`, preferring `0.8.0`.** The interop suite drives
  `@microsoft/agent-host-protocol@0.8.0` from npm, and negotiates `0.8.0`.
- **`session/workingDirectoryReplaced` is accepted and validated.** It needs
  `multipleWorkingDirectories`; replacing the primary (index 0) needs
  `primaryReplacement`, which wins over `immutablePrimary` when both are
  advertised; the replacement answers to
  `Policy.may_grant_working_directory` exactly as a set does. With
  `primaryReplacement`, a generic `session/workingDirectoryRemoved` of the
  primary is rejected — the spec's MUST.
- **`session/customizationToggled` takes the 0.8.0 `enablement` decision
  list.** A toggle without a well-formed one — including the 0.7.0 `enabled`
  shape — is rejected, so the client reverts; previously-accepted old-shape
  toggles would now reduce to a no-op while telling the provider `False`.
  `HandlesCustomizations.customization_toggled` keeps its signature and
  receives the effective value, `enablement?.[0]?.enabled ?? true`.
- **`auth/required` carries the complete `ProtectedResourceMetadata`** in
  `resource`, not its identifier. `auth_required_params` takes a
  `ProtectedResource`; `Host.notify_auth_required` still accepts a bare
  identifier and resolves it against the advertised resources.
- The demo plugin and MCP server no longer publish `enabled` — both carry
  scoped `enablement` since 0.8.0, and absent means enabled.
- A `toolClientExecution` input entry no longer raises the session's
  `InputNeeded` bit (a 0.8.0 reducer change, from the dependency); the test
  that asserted the old behaviour now asserts the new.

## [0.1.0] — pending

The first release, staged: **no `v0.1.0` tag exists yet**, and
[`RELEASING.md`](RELEASING.md) is the procedure that cuts it — which also sets
this heading's real date. It speaks protocol versions **0.7.0 and 0.6.0**,
answers all 29 client→host commands plus the reverse `resource*` direction,
and is driven by the real VS Code client and the published TypeScript client
in CI.

It will be released on GitHub, deliberately not on PyPI: install the protocol
package from its repository first, then
`pip install "ahp-host[ws] @ git+https://github.com/jackhumbert/ahp-py@ahp-host/v0.1.0#subdirectory=packages/ahp-host"`.
The built wheel and sdist will be attached to the GitHub release.

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
- `python -m ahp_host`, a demo host with a flag per surface, which
  prints the VS Code settings block to paste.
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
- **A validator also receives the upgrade HEADERS**, `(str | None, Mapping[str, str])`,
  because `?tkn=` is the wrong place for a credential and a host that can only read
  the query value is stuck with it. **A reverse proxy writes the full request URI to
  its access log**, so a query token sits in cleartext in a log file on every single
  connection — measured on a real deployment at 262 of 262 requests, token included.
  `Authorization` is redacted by the same proxy (`['REDACTED']`, verified). VS Code's
  own client can only send `?tkn=`, so both paths must keep working and the validator
  decides which it accepts.
- **`connection_token` on the WebSocket server also accepts a validator callable**,
  so a host with **per-user tokens** can refuse an unknown one
  at the handshake with 403. The alternative — admit every peer and refuse in
  `Policy.authorize_connection` — works, but leaves an unauthenticated peer holding
  an open socket and turns a 403 at the upgrade into a connection that dies a moment
  later. The callable owns its comparison, so it must use `secrets.compare_digest`
  or a hash lookup; said in the docstring because a validator written the obvious way
  is timing-attackable where the string branch is not. `WebSocketServer.url` returns
  the bare base for a validator, since printing one peer's token would be worse than
  printing none.
- **`runs_commands` on a terminal backend** (feature-detected, defaults True) now
  gates whether the host advertises the `!command` prefix. The class check alone was
  not enough: a host may install a backend that deliberately executes nothing — to
  satisfy a client that opens a terminal unconditionally and explain itself in the
  panel rather than refusing and producing an error toast on every window focus.
  Advertising `!` for such a backend turns a working input into a dead end, which is
  the same reason the refusing default does not advertise it.

### Added — foundations

- Phase-1 research (`docs/research.md`), the empirical log (`docs/experiments.md`),
  the v0.1 plan (`docs/plan.md`) and ADRs 0001–0004.
- Vendoring of the upstream conformance corpora and schemas at `spec/v0.7.0`
  (`scripts/vendor_upstream.sh`), committed under `vendor/upstream/`.
  **Since moved** to `ahp-protocol`; neither path exists here now.
- Generation of the upstream data tables — action types, `IS_CLIENT_DISPATCHABLE`,
  `ACTION_INTRODUCED_IN`, error codes — from the vendored TypeScript source of
  truth (`scripts/generate_tables.py`), reproducibility enforced in CI.
  **Since moved** to `ahp-protocol`, where CI still enforces it.
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

### Changed

- **The protocol layer is now a separate package.** Wire types, the seven
  reducers, the transports and the vendored conformance corpora moved to
  [`ahp-protocol`](https://github.com/jackhumbert/ahp-py/tree/main/packages/ahp-protocol),
  which this package depends on (`~=0.1.0`, tight because the spec lands
  breaking changes in MINOR bumps). The reason is a Python *client*: the
  1,161-line chat reducer has to exist exactly once, and a fork with
  drift detection makes drift *detectable* rather than impossible.

  Embedders importing `ahp_host.types`, `.reducers`, `.transport`,
  `.core.errors`, `.core.channels` or `.core.versions` should import
  `ahp_protocol.…` instead. Everything re-exported from
  `ahp_host` itself is unchanged.

  Bumping the spec pin is no longer a change to this repository.

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

### Fixed — findings from the real VS Code client

All of these failed **silently** — the client renders nothing it does not
recognise, so a wrong shape and an absent feature look identical:

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

### Fixed — a security survey and the first sibling-client run

- **An empty `clientId` made every peer that sent one the same peer.** The host
  minted a UUID for a `clientId` that was not a string and kept one that was,
  including `""` — which is what an unset config value looks like on the wire.
  `holds_claim` then handed two connections each other's terminals, the
  active-client gate let one assert the other's role, and the removal on
  disconnect skipped them entirely, leaving the session advertising tools nobody
  could execute. An empty id is now the absence it is, and an `activeClient`
  claim on `""` is refused rather than quietly rewritten — the client asked to
  be an identity the host cannot give it, since `InitializeResult` has no field
  to hand a minted one back. `terminals.claim_from_wire` refuses it too, which
  the sibling client's parser has always done: the host used to accept an empty
  claim as real while every client rendered the same payload as *unclaimed*,
  and because `terminal/claimed` is itself claim-gated, no peer could ever take
  the terminal back.

- **An activity a provider set itself was never retracted.** `session/activityChanged`
  has two writers — the turn sink, and `SessionPublisher.activity_changed`,
  which the provider guide tells providers to call and which works outside a
  turn. The sink deduped against a cache only it wrote, so a string it had not
  published looked like no string at all and the `set_activity(None)` in the
  turn's `finally` deduped itself away. A session went idle still claiming to
  be editing a file, and only a later tool call could clear it. The sink now
  reads what is published from the session's own state, which is the thing both
  writers write to.

- **The connection token was logged verbatim**, at DEBUG and again at WARNING
  when a handshake was refused — the latter on by default. `?tkn=` is a bearer
  credential in a query string, and the same repository already says so about
  somebody else's reverse proxy. Redacted at both sites and on the public
  `last_handshake_path`, leaving `tkn=<redacted>` so a reader can still tell a
  rejected token from a missing one.

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

### Fixed — CI and documentation

- **Every CI job failed at a checkout, not at a test.** All five cross-repo
  checkouts here name a *private* sibling, and `GITHUB_TOKEN` is scoped to the
  repository the workflow runs in — so each one failed with "Repository not
  found" and nothing downstream of it ever ran. They now take
  `secrets.SIBLING_REPO_TOKEN` with a fallback to the default token, which is
  what works once the siblings are public: right in both worlds, no second edit.

- **The README's only install line could not work.** `pip install -e '.[ws]'`
  resolves `ahp-protocol~=0.1.0` from an index that has never heard of
  it and stops. It now installs the sibling checkout first, and a new derived
  check in `tests/docs/test_readme_is_true.py` reads the dependency list from
  `pyproject.toml`, so a second sibling dependency cannot be added without the
  README learning about it.

- **The README called `chat/usage` unimplemented** — "no producer, so no token
  counts or cost attribution" — long after one shipped, with tests asserting
  where it lands in the turn. That section is the one a reader uses to decide
  whether to build around a gap, so a stale entry costs somebody real work.
  A second derived check now reads each "genuinely absent" bullet that names a
  bare action and looks for a publisher of it.

- **The CHANGELOG dated a release that never happened.** No `v0.1.0` tag was
  ever cut and nothing was uploaded, so its two link definitions pointed at a
  tag and a release page that do not exist, and two of its entries described
  `vendor/upstream/` and `scripts/generate_tables.py` as things this package
  has — both moved to `ahp-protocol` in the extraction above.

### Documentation

- [`docs/guide/writing-a-provider.md`](docs/guide/writing-a-provider.md) and
  [`docs/guide/deploying.md`](docs/guide/deploying.md) — **every example in both
  is executed by the test suite**, so they cannot drift.
- The README's command list is derived from the dispatcher and its flags from
  `--help`, both asserted rather than maintained.
- Every published frame is validated against the vendored upstream JSON schemas.
- ADRs 0001–0005, the research log, the experiment log, the roadmap, and
  [`UPSTREAM.md`](UPSTREAM.md) recording which protocol versions we speak.
- **`docs/deferred-upstream.md`** — measured defects belonging to the spec or to VS
  Code, held until these repos are public. Seeded with U1 (no terminal capability
  in `AgentCapabilities`) and U2 (VS Code lowercases the `b64-` filesystem
  authority, which is case-sensitive base64, breaking every agent-host file open
  for every possible host address).

### Known limitations

- Some VS Code surfaces cannot be driven by any host: model-generated session
  titles, checkpoints, plan review, and `SessionState.serverTools` (rendered by
  nothing in any build). Picking a *local* folder in the Agents window will never
  offer a remote host — the two `resolveWorkspace` gates are mutually exclusive.
  Browse through the host's own picker instead; the guide explains why.
- Requires POSIX for the pty backend. Everything else is portable.


<!-- No v0.1.0 tag exists yet, so a compare/v0.1.0...HEAD link and a
     releases/tag/v0.1.0 link would both 404 today. Both definitions point at
     the branch until the tag is real; RELEASING.md's release-day steps flip
     them to the compare and tag URLs. -->

[Unreleased]: https://github.com/jackhumbert/ahp-py/commits/main/packages/ahp-host
[0.1.0]: https://github.com/jackhumbert/ahp-py/commits/main/packages/ahp-host
