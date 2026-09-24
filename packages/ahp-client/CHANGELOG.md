# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning is [SemVer](https://semver.org/), independent of the protocol's.
Every release states the protocol versions it speaks.

## [Unreleased]

### Changed — protocol 0.9.0

- **Built on `agent-host-protocol` at `spec/v0.9.0`.** The default offer is
  `0.9.0`, `0.8.0`, `0.7.0`, `0.6.0`; against the sibling host this client
  negotiates `0.9.0`. Where 0.9.0 moved a shape this client reads, it reads
  both, because it still negotiates older hosts:
  - `TurnFailed.reason` / `.error_type` read `chat/error`'s `part.error`
    (0.9.0) and fall back to the legacy `error`.
  - `Terminal.exit_code` / `.exit_reported` read `lifecycle` (0.9.0) and fall
    back to the top-level `exitCode`. A codeless exit is now visible in state
    against a 0.9.0 host.
  - `SessionClaim` gains `chat`, which a 0.9.0 host requires on a session
    claim; it is optional here so claims from older hosts still parse.
- **Three automation command wrappers:** `list_automation_trigger_definitions`
  (root-scoped), `run_automation` and `fetch_automation_runs` (forced to
  `ahp-automations://`, a new `AUTOMATIONS_SCOPED` set). 30 commands, all
  wrapped.
- `ContentNotFound` is removed — 0.9.0 dropped the code, and `-32006` stays
  reserved. `chat/turnResume` is deliberately not modelled as an event.
- `testing.FakeHost` negotiates `0.9.0` and sends `startedAt` / `duration` on
  its turns, since the chat's `modifiedAt` now derives from them.
- `docs/parity.md` regenerated: 96 action types, 44 client-dispatchable, nine
  reducers.

### Changed — protocol 0.8.0

- **Built on `agent-host-protocol` at `spec/v0.8.0`.** The default offer
  follows the pin's `DEFAULT_SUPPORTED_VERSIONS` and is now `0.8.0`, `0.7.0`,
  `0.6.0`; against the sibling host this client negotiates `0.8.0`. None of the
  0.8.0 wire changes touch a shape this client sends or reads — it has no
  customization-toggle or working-directory helpers, and `auth/required`
  params are surfaced raw on `AuthRequiredEvent`, so the new
  `ProtectedResourceMetadata` object arrives intact.
- `docs/parity.md` is regenerated: 86 action types, 39 client-dispatchable,
  adding `session/workingDirectoryReplaced`.
- `testing.FakeHost` answers `initialize` with `0.8.0` by default.

Under construction. `docs/plan.md` is the design and its §12 is the build order.

### Added — continuous integration

- **This repository had none.** 487 tests, `ruff`, `mypy` and `lint-imports`
  ran on no push and no pull request, while both sibling repositories checked
  *this* one out to prove they still worked with it. Four jobs now: `check`
  across 3.11 through 3.14 with the client alone, `against-the-sibling-host`
  with the real `agent-host-server` installed, `distributions-are-installable`,
  and a non-blocking `protocol-main` that surfaces drift the `~=0.1.0` pin
  hides.

- **`tests/client/_sibling.py`** — one decision about whether the sibling host
  is importable, made once. Four modules decided separately, two of them with a
  bare `importorskip`, which makes a job whose cross-repo install silently
  failed report exactly what a healthy one reports. Under
  `AHP_INTEROP_REQUIRED` — the same switch the host's own interop suite uses —
  a missing sibling is now a collection error instead of a skip.

- **`scripts/smoke_wheel.py`** — imports the built wheel from outside the
  checkout and runs the conformance probe through it, which is the only way to
  catch a name in `__all__` that is never bound, a subpackage the backend did
  not collect, or an absent `py.typed`. It found something immediately:
  `agent_host_client.ws` is the `[ws]` extra, so a `--no-deps` install cannot
  import it and the job has to supply `websockets` by hand — otherwise "not
  packaged" and "not installed" are the same red.

### Added — release engineering

- **The distribution model is settled: public on GitHub, deliberately not on
  PyPI.** Installs are two git lines, protocol package first —
  `pip install "agent-host-client[ws] @ git+https://…"` — pinned by tag for a
  release, with the built wheel and sdist attached to each GitHub release.
  There is no index upload, no trusted publisher, no `pypi` environment, and
  no secret anywhere in the pipeline; CI checks out the public siblings with
  the default token.
- **The release is a tag push.** `release.yml`: rebuild, `twine check
  --strict`, a tag-must-equal-`__version__` guard, a smoke install that
  supplies `agent-host-protocol` from its repository and then installs the
  wheel with `[ws]` — exactly the documented install path, proven before the
  release exists — and a GitHub release whose notes are the changelog section
  verbatim, refused if the section is missing. `RELEASING.md` is the
  checklist, including the next-`.dev0` bump that keeps a stray build from
  `main` from impersonating a release, and the recovery story a moved index
  could never offer: delete the release, delete the tag, fix, re-tag.
- **The version is written once.** `pyproject.toml` declares `version` as
  dynamic and hatch reads `__version__` out of `__init__.py`. Previously the
  two spellings agreed by discipline, which is to say: until a release day.
- **The sdist is an allowlist.** Hatchling's default file selection honours
  only the root `.gitignore`, and both `.hypothesis/` and
  `.import_linter_cache/` self-ignore with a *nested* one — invisible to
  `git status`, shipped by the build. The sdist carried ~350 Hypothesis
  example-database blobs, a linter cache and `.github/`. Named includes now,
  both caches in the root `.gitignore`, and a CI tripwire so the next tool's
  cache directory stays out too. `twine check` runs in CI on both
  distributions, and the wheel job builds the sdist as well.
- **`CONTRIBUTING.md`** — `docs/plan.md` §12 lists it in the M1 scaffold, and
  it did not exist — plus **`SECURITY.md`** stating this library's actual
  trust boundaries (the host is untrusted input; token redaction; the
  `serve/` mount roots; approval gating). CI runs under a least-privilege
  `permissions:` block, Dependabot watches actions and pip weekly, and the
  matrix and classifiers gain Python 3.14. The `LICENSE` now names this
  project's contributors; it named the sibling server's, verbatim, since the
  file was first copied over.
- **The empty `tests/unit/` package is gone** — scaffolding from a layout this
  repository never adopted, collected by every tool and populated by nothing.

### Fixed — "ahp-inspector-compatible" is now verified, and was false

- **The wire log's `_ahpLog.ts` was epoch milliseconds; the inspector requires
  an ISO string.** ahp-inspector 1.5.3's `extractWireMeta` demands
  `typeof ts === "string"` with `Date.parse` accepting it — its own fixture
  pins `{ts: 42} → null` — and a rejected sidecar takes the `dir` marker down
  with it. Every frame this client ever logged therefore rendered at
  *ingest* time rather than wire time, and direction fell back to structural
  inference, which classifies any request as client-sent and any response as
  host-sent — inverting exactly the logged host→client traffic this client is
  unusual in serving. The suite never noticed because `dir`, `connectionId`
  and `transport` were asserted and `ts` never was. The sidecar now mirrors
  the sibling host's `core/wirelog.py` byte for byte, a regression test pins
  the inspector's acceptance rule, and the claim is verified the honest way:
  the doctor's probe logged over a real transport and every frame run through
  the inspector's **own** `wire-meta.ts` — 10/10 accepted, directions
  preserved, with the structural fallback shown misclassifying the
  reverse-direction pair the sidecar saves.

### Fixed — a bounded close now releases its socket

- **`WebSocketClientTransport.close()` could abandon its socket forever.** The
  two-second bound was `asyncio.wait_for(self._socket.close(), 2.0)` — and
  `wait_for` *cancels* the closing handshake it interrupts, after which
  nothing ever aborts the TCP transport. The case that reaches it is real, not
  theoretical: once the read loop stops receiving — the malformed-frame limit
  is exactly such a stop — flow control pauses the reader, the peer's close
  frame sits unreadable behind the backlog, and the handshake can never
  complete. Every timed-out close leaked its socket, and a supervisor
  reconnecting to a hung host paid one per cycle, each surfacing only as a
  `ResourceWarning` at garbage-collection time attributed to nowhere useful.
  `connect()` now passes `close_timeout` so `websockets` itself aborts the
  transport after the budget, and the outer backstop — still there for
  `from_socket()` connections that keep their own — ends in
  `transport.abort()` instead of a suppressed timeout.
- **Warnings are errors** (`filterwarnings = ["error"]`). The suite was one
  warning away from clean, and that warning was the leak above; a policy that
  only reports would have kept scrolling past it. The loopback tests' servers
  now tear down with a close that *waits*, so a GC-time warning can no longer
  be misattributed to whichever unlucky test the collector runs during.

### Added — typed APIs for the surfaces that had none

`docs/plan.md` §1.3, written after the first interop run, measured the axis the
§1.1 table does not: of the seven channels, **four had no typed API at all** and
had to be driven through `client.protocol.request`.

- **Changesets.** `Session.changesets()` / `open_changeset()`, and a `Changeset`
  with typed files, operations and review. `invoke()` validates `operationId`
  and `target.kind` against what *that* changeset advertised — which is the
  whole reason the wrapper earns its place, since the alternative is learning
  your scope was wrong from a JSON-RPC error.
- **Terminals.** `Terminal` with the claim made explicit, because the state is
  *contended*: `terminal/claimed` is an arbitration and a refused claim is a
  thing a caller has to be able to see. Shell integration
  (`terminal/commandExecuted` / `commandFinished`) is surfaced, and
  `split_terminal_command` implements the `!` shorthand now that
  `terminalCommandPrefix` survives the handshake.
- **Resource watches: deliberately none.** `createResourceWatch` already
  returns the channel to subscribe to; a `watch()` helper would be four lines a
  caller can write, and hiding the receiver-assigned channel makes the spec's
  own advice — "do not derive it, subscribe to what comes back" — unfollowable.
  §1.3 states the reasoning. Absent is the answer, and a wrapper that exists to
  make a table look complete is a cost, not a feature.

### Fixed — a three-repo conformance review against the pin and VS Code

An adversarially-verified review of all three repos against the vendored
`spec/v0.7.0` sources and VS Code's own implementation. Every finding below was
confirmed by an independent second reading before it was fixed, and each fix is
pinned by a test.

- **`UnsupportedProtocolVersion.supported_versions` read a field no conformant
  host sends.** The pin names the `-32005` data field `supportedVersions`
  (`errors.ts:157`); the property read `supportedProtocolVersions` and returned
  `()` against every conformant host — including this project's own server. The
  legacy spelling is still accepted as a fallback.
- **Cancelling a `request()` caller leaked its pending entry** on the default
  timeout path: `asyncio.shield` kept the inner future pending, so the
  `finally` guard missed. The entry is popped through the shield, the id is
  burned, and a late response is dropped with an `UnknownResponse` diagnostic.
- **One malformed frame from an eagerly-parsing transport ended the read loop
  permanently** while the state stayed `connected` — the `JSONDecodeError`
  handler sat outside the loop. It now logs-and-continues per invariant 8, and
  hitting `MALFORMED_FRAME_LIMIT` also closes the transport (parity with VS
  Code's forced 4002 close). `WebSocketClientTransport.receive()` raises per
  undecodable frame instead of silently skipping, so the accounting fires over
  the real wire.
- **Reader/writer task failures surface onto the connection state** via a
  reaping done-callback, as the class docstring always claimed; a response
  frame with a boolean `id` no longer settles request 1; an idle bounded
  `BroadcastQueue` no longer accumulates and emits false `DroppedEvents`.
- **Request ids and `clientSeq` really do survive transport swaps now.**
  `AhpClient` grew `first_request_id`/`first_client_seq` seeds plus
  `next_request_id`/`next_client_seq`, and the supervisor threads each
  successor from where its predecessor stopped — the plan's "VS Code's first
  frame on a fresh socket carried id 66" promise, previously kept by nobody.
- **`HostConfig.initial_subscriptions` with a non-root URI wedged every later
  reconnect**: the URI was recorded with reducer name `""`, which fails
  `bind()` with a `KeyError` on each snapshot path until the policy exhausts.
  Entries now accept `(uri, reducer_name)`, and a bare URI shape-sniffs.
- **The snapshot-arm reconnect silently dropped a subscription made while the
  RPC was in flight** — the plan's "keep iff surviving or not prior" rule,
  stated in an inline comment the code did not implement.
- **No authentication re-check ran after a reconnect.** `HostConfig.auth_check`
  is awaited against the fresh client after every successful handshake, before
  the state flips to connected (plan §6.3; `authentication.md` Auth Expiry).
- **The mirror ports VS Code's `_promotePendingTurnStartIfTerminal`**: a
  server-originated terminal turn action retires the matching pending
  optimistic `chat/turnStarted`. Optimistic reads are cached per write, never
  reduce onto a missing snapshot, and assert the running loop on the read path;
  gap detection treats `fromSeq: 0` as a real baseline; the `ahp-root:`
  spelling is accepted on envelopes as VS Code accepts it.
- **The default approval policy silently denied every tool call.** It is now
  truly manual: the event surfaces, the drained stream waits
  `approval_timeout`, then raises `UnansweredToolCallError(tool_call_id,
  tool_name)` — plan §7's loud, specific error.
- **The client-tool pump ran gated calls before anyone approved them, ran other
  sessions' calls, and lost calls to tap eviction or a disconnected window.**
  It is level-triggered on confirmed mirror state now: a ready without the
  `confirmed` handover marker waits; scope is the session's own chats;
  recovery scans state after every wake and reconnect; executors run on their
  own tracked tasks so one slow tool blocks nothing. A `ContentRef` `toolInput`
  is resolved fresh via `resourceRead` before the executor runs, never cached
  across confirmation.
- **`Session.prompt(model=…)` put a bare string on the wire** where the pin
  requires a `ModelSelection` `{id, config?}` object; **`create_chat`'s
  string-convenience `initialMessage` omitted the required `Message.origin`**;
  **`fetch_turns` documented its channel as the session URI** when
  `FetchTurnsParams.channel` is the chat; `create_session` treats
  `creationFailed` (not `"failed"`) as the failure lifecycle and raises on
  timeout instead of returning a dead session; `>1 workingDirectories` is
  refused locally unless `capabilities.multipleWorkingDirectories` is
  advertised.
- **`TurnStream` leaked one events cursor per prompt** on long-lived
  connections; it detaches on every finish path and gained `aclose()`. Rejected
  envelopes no longer decode as ordinary turn events in `ChatWatch` or
  `TurnStream` — a host refusal is not a `TurnStarted`.
- **`serve/` answered wrong shapes on three reverse methods**: `resourceWrite`
  with `ifMatch` on a missing file re-created it instead of answering `-32011
  Conflict`; `VirtualResourceServer.resourceList` answered `{}` for a missing
  URI instead of `-32008`; read/write results carried an undeclared `etag`.
  `ClientToolHost.owns()` requires the pinned `clientId`.

### Fixed — the first interop run against a real host

Driven against the sibling [`agent-host-server-py`](https://github.com/jackhumbert/agent-host-server-py)
across ten protocol surfaces. Two implementations built independently from the
same spec, meeting for the first time; every disagreement adjudicated against
the vendored schema rather than against what the other peer happened to want.

The pattern in the worst of them: **a schema-required field omitted**, which
the reducer then refuses while the sender believes it succeeded.

- **`turnId` was missing from five different actions** — `Chat.cancel()`,
  `ToolCallReady.approve()`/`.deny()`, `ClientToolHost.execute()`'s completion,
  and the `chat/turnStarted` a `TurnStream` opens with. Each wedged something
  quietly: a cancel that no reducer applied while the host killed the turn
  anyway, a confirmation that landed in no mirror, and a client tool that RAN
  and had its result discarded as `skipped`.
- **`connect()` never returned when the handshake was permanently refused.**
  The supervisor reached `HostState("failed", …)` and returned without setting
  either of the two events `_await_connected` races, so `start(wait=True)` —
  the default, and what the context manager uses — was unsatisfiable. Every
  caller hitting an unsupported version or a policy refusal hung with no
  exception and no way to observe it.
- **The elicitation surface never sent `chat/inputCompleted`**, so every
  elicited turn hung forever.
- **`chat/error` was mapped to nothing**, so a failing turn never terminated
  its stream. `chat/reasoning`, `chat/toolCallDelta` and
  `chat/toolCallContentChanged` arrived only as `UnknownEvent`. The event
  mapper is now exhaustive over `ACTION_TYPES`, with a test holding both
  halves — modelled and deliberately-not — against it.
- **The reverse `resource*` family used the wrong field names throughout**:
  `content` where the schema says `data` + `encoding` (so a spec-shaped write
  truncated the file to zero bytes and reported success), and `kind` where it
  says `type` (so a host saw every client directory as a file).
- **A rejected action was applied by non-originating subscribers**, diverging
  from the host permanently — reconciliation skipped the echo only for the
  client that sent it.
- **`shutdown()` blocked for ten seconds after any abnormal teardown.**
  `websockets` runs a closing handshake with a 10s default and waits it out
  against a peer that is already gone, so an embedder calling `shutdown()` in a
  `finally` paid it per client — and `HostRuntime` paid it again on every
  reconnect. Measured at 9.99s; now bounded, with a healthy close still
  completing in microseconds.

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
- **M7 — the reverse direction.** All 10 `ServerCommandMap` methods routed, 9
  served (`createResourceWatch` declined on purpose — plan §1.3), a
  symlink-safe file server whose write half is a second opt-in, in-memory
  `virtual://` plugin content, client-owned tool execution, and the elicitation
  surfaces including `chat/toolCallResultConfirmed`.
- **M8 — logs, doctor, interop.** ahp-inspector-compatible wire logs with
  credential redaction and no opt-out, `agent_host_client.doctor` as a
  conformance probe for someone else's host, and a full turn against the sibling
  Python host.
- **Watching a turn this client did not start** — `Chat.watch()` /
  `Session.watch()` returning a `ChatWatch` that dispatches nothing and filters
  no turn id, with a synthetic `TurnInProgress` for mid-turn entry, and
  `event_for` exported. ([`docs/requests.md`](docs/requests.md) item 1.)
- **Answering somebody else's request from the front door** —
  `Session.pending_inputs()`, `Session.responder`, and `Session.inputs()` as an
  async iterator over the pending set. (item 2.)
- **`ssl=` on `connect()`** and `WebSocketClientTransport.connect`, so a private
  CA or a client certificate no longer forces a hand-written transport factory.
  (item 4.)
- **Multi-chat has a typed API.** `Session.create_chat()`, `Session.chats()`,
  `Session.open_chat()`, `Chat.dispose()` and `Session.capabilities`. Creation
  is refused locally when the agent advertises no `capabilities.multipleChats`,
  and a `ChatSource` is refused unless the matching `fork` / `sideChat` flag is
  set — all three are MUST NOTs the client is told about in advance, so making
  the host enforce them is a round trip spent proving we did not read the
  advertisement. `multipleChats: {}` is the ordinary advertisement and `{}` is
  falsy in Python, so the presence test is `is not None`. This is the first of
  the four surfaces the interop run had to drive through
  `client.protocol.request`; see `docs/plan.md` §1.3 for the other three and why
  they are not next.
- **Changesets have a typed API.** `Session.changesets()`,
  `Session.open_changeset()` and a `Changeset` with `files()`, `operations()`,
  `read()`, `mark_reviewed()`, `invoke()`, `changes()` and
  `wait_until_ready()`, plus `ChangesetInfo` / `ChangesetFile` / `FileSide` /
  `ChangesetOperation` views and `text_range()`. Four checks a caller cannot
  make from the params: `read()` takes a `FileSide` so the file URI cannot be
  passed where the `ContentRef` belongs (the bytes are in a store the host owns,
  and a diff's `before` no longer exists on disk); review is gated on
  `capabilities.review`, re-read from the *catalogue entry* on every call
  because the host validates against the current one; `operationId` and
  `target.kind` are checked against the changeset's own live `operations` and
  `scopes`; and an operation carrying a `confirmation` is refused until
  `confirmed=True`, which is a client MUST and marks the operation destructive.
  `uriTemplate` expansion covers the three defined shapes and percent-encodes
  its values; a template naming any other variable is `openable == False`
  rather than expanded. Second of the four surfaces §1.3 named.
- **Terminals have a typed API.** `Client.create_terminal()`,
  `Client.open_terminal()`, `Client.terminals()` and a `Terminal` with
  `write()`, `resize()`, `rename()`, `clear()`, `hand_to()`, `take()`,
  `output()`, `commands()`, `events()`, `wait_for_exit()` and `dispose()`, plus
  typed `ClientClaim` / `SessionClaim` and the terminal event family. The claim
  is read from **confirmed** state, never optimistic: `terminal/claimed` is an
  arbitration the host can refuse, and replaying our own un-echoed claim answers
  "you hold this" to a question that has not been decided — after which the
  keystrokes it authorises are the ones that silently vanish. A refused envelope
  is checked before the action type is looked up, so a rejected
  `terminal/claimed` can never decode as a `TerminalClaimed` for *any* peer,
  which is the interop defect this surface is named after; it arrives as
  `TerminalRefused`, with `mine` separating our own from somebody else's.
  `write()` raises `TerminalNotHeld` naming the holder rather than letting a
  refused `dispatchAction` notification fail invisibly (`force=True` for a host
  whose policy is more permissive); `hand_to()`, `resize()`, `rename()` and
  `clear()` are deliberately ungated, because the guide's detach flow has a
  client re-scoping and resizing a terminal it does not hold. Disposal is not
  claim-gated either — a session claim is held by no client, so a gate would
  make every handed-over terminal immortal. The refusal check runs **in the
  stream**, not only in the decoder: `terminal/input` is the one action type the
  stream filters out and also one of the two the host gates on the claim, so
  filtering by type first destroyed the refusal one line before the decoder that
  would have named it. `mine` asks one question on every arm of the union (did
  this client dispatch it); `TerminalClaimed.held_by_us` is the second question
  under its own name. `Terminal.alive` and `Terminal.exists` separate "this
  client unsubscribed" from "a peer disposed it" from "the process ended",
  because a dropped channel answers every read with a plausible empty value;
  `wait_for_exit` watches both and defaults to a timeout, since a disposal
  publishes no `terminal/exited`. `Client.terminals()` and `Terminal.commands()`
  are typed views for the reasons the claim and `durationMs` are, and
  `terminal_uris()` digs the host-assigned terminal channel out of a tool call's
  content parts.
- **The `!` shorthand is implementable for the first time.**
  `Client.terminal_command(text)` answers whether the host will run a chat
  message as a shell command instead of handing it to the agent, resolved
  against the *negotiated* `terminalCommandPrefix` — `None` when the host
  advertises none, because absence means no shorthand and a hardcoded `"!"`
  offers it to hosts that never claimed it. A blank remainder is a message, not
  a command. `FakeHost(terminal_command_prefix=…)` lets a downstream suite test
  both answers. Third of the four surfaces §1.3 named; resource watches stay
  absent, and §1.3 says why.

### Changed

- An action the client deliberately does not surface — mostly its own writes
  echoing back — is now skipped rather than delivered as `UnknownEvent`.
  `UnknownEvent` is forward compatibility, and conflating the two meant a
  caller watching it for a version mismatch got one on every turn it approved
  a tool or answered a question. A *rejected* echo still reaches the caller,
  which is the case that matters.
- **Subscriptions are refcounted in `HostRuntime`.** Two handles on one channel
  is the ordinary case — `open_chat`, `open_changeset` and `open_terminal` each
  mint a fresh object and none memoises — and an unrefcounted `unsubscribe` from
  either one blinded the other with no error anywhere: the survivor's state went
  empty, its waits returned instantly, and its dispatches vanished into a channel
  this client no longer received. `HostRuntime.subscribed()` is the predicate
  behind `Changeset.live` and `Terminal.alive`.
- **The runtime's `events()` tap reports what it drops.** It is bounded (4096)
  and had no `on_drop`, so a reader slower than a flooding pty was fast-forwarded
  past its own `TerminalRefused` in silence. `AhpClient` wires the identical
  queue to a `DroppedEvents` diagnostic; this one now does too.
- **`InvalidArgument` is both an `AhpClientError` and a `ValueError`.** The
  argument guards were the hole in "one `except` catches the whole library": the
  documented handler for the changeset review gate is `except AhpClientError`,
  which caught the capability refusal and missed the empty-batch refusal one line
  away. `Changeset.wait_until_ready` and `Terminal.wait_for_exit` raise
  `RequestTimeout` rather than the builtin `TimeoutError`, for the same reason.
- **`invoke_changeset_operation` names `operation_id` in its signature.** It is
  required by `InvokeChangesetOperationParams`, the host answers `-32602` without
  it, and a `**extra`-only signature type-checks clean under `mypy --strict`
  while omitting it — the sixth call site of the shape `client/actions.py` was
  extracted to stop.

- **A permanent connection refusal is no longer retried forever.** `TransportError`
  carries `kind="rejected"` with the HTTP `status` or WebSocket `close_code`
  instead of flattening a refused upgrade into `"io"`, and
  `ReconnectPolicy.should_retry` declines HTTP 401/403, a 1008 policy close and
  `-32005`. Previously an expired token produced one doomed handshake per
  backoff interval against the proxy already rejecting it, while the surface
  showed `reconnecting` forever. `retry_everything` restores the old behaviour.
  ([`docs/requests.md`](docs/requests.md) item 3.)
- **`Session.inputs()` wakes on the envelope rather than on an interval.** It
  waits on the event reader filtered to the session's channel and re-reads
  `pending_inputs()`; the mirror is still the source, the envelope is only the
  clock. `poll` keeps its place in the signature and becomes a ceiling on
  staleness — a backstop for anything that moves `inputNeeded` without an event
  scoped here, a resubscribe snapshot after a reconnect being the case that
  matters — and its default moves from `0.05` to `5.0` accordingly. A consumer
  holding a connection per user was otherwise paying a mirror read and a list
  comparison per session per 50 ms forever, on the one edge in this library
  where latency was a guess and the thing waiting was a human.
  ([`docs/requests.md`](docs/requests.md) item 9.)

### Fixed

- **Every turn-scoped action now carries the `turnId` the reducer matches on.**
  `Chat.cancel()`, `TurnStream`'s `chat/turnStarted`, `ToolCallReady.approve()`
  and `.deny()`, `ToolCallResultReview`, `InputResponder.confirm_tool()` /
  `.confirm_result()` and `ClientToolHost.execute()` each assembled their action
  as a dict literal and each omitted it. A host accepts, numbers and broadcasts
  such an action, and every chat reducer on the wire then returns its state
  unchanged — so a cancel left `activeTurn` open forever on both peers while the
  provider really was torn down, and a client tool ran, answered the agent, and
  was recorded everywhere as `cancelled`/`skipped`. Fixed in the same shapes:
  `startedAt` and `message.origin` on `chat/turnStarted` (turns were stored with
  `startedAt: null`), `confirmed` on an approval, `selectedOptionId` where the
  client sent `optionId` (so the chosen confirmation option was dropped), and a
  failed client tool's result, which was MCP's `{isError, content:[{kind}]}`
  rather than the protocol's `{success, pastTenseMessage, content:[{type}]}`.
- **Outbound actions are no longer assembled by hand.** `client/actions.py`
  constructs the turn-scoped ones with `turnId` as a required parameter, so the
  omission above is a type error rather than a silent no-op, and an empty id is
  refused instead of sent. `Chat.cancel()` also reports a `duration` measured on
  this client's own clock for a turn it started, and `0` otherwise — the spec
  forbids deriving it by subtracting another peer's `startedAt`.
- **An elicited turn can be answered at all.** Every path —
  `InputRequested.answer()/.decline()/.cancel()` and `InputResponder.answer()` —
  dispatched `chat/inputAnswerChanged` carrying an `answers` map and a `kind`,
  neither of which that action has, and nothing anywhere sent
  `chat/inputCompleted`. Draft sync does not resolve a request, so the host
  stayed parked on its own future, the chat stayed `InputNeeded`, and the turn
  hung forever. `InputRequested` now reads its id from `request.id` — the action
  has no `requestId`, so what went out was `""` — and exposes `message`, `url`,
  `questions` and `answers`, without which a consumer cannot render the prompt it
  is being asked to answer. Answer values are encoded from each question's kind,
  because the two vocabularies do not line up by name: a `single-select` answers
  `selected`, an `integer` answers `number`, a `multi-select` answers
  `selected-many`. `InputResponder.sync_draft()` is the real
  `chat/inputAnswerChanged`: one `questionId`, one singular `answer`.
- **`InputResponder` stopped reading a field that does not exist.** Its
  "merge rather than clobber another client's partial answer" read
  `ChatState.inputRequests`, which is in no version of the schema, so it always
  merged `{}`. A live request is the `kind: "inputRequest"` response part on the
  active turn — now `open_request()`, and the source of the question kinds an
  answer is encoded against. The merge itself is the reducer's: it overlays
  `chat/inputCompleted.answers` on the synced drafts, and the host reads the
  result back out of reduced state rather than off the action.
- **A name-based `ApprovalPolicy` no longer denies everything.**
  `ToolCallReady.tool_name` read `action["toolName"]`, which
  `chat/toolCallReady` does not carry — the name is on `chat/toolCallStart` and
  required on `ToolCallState` — and `auto(read_only=True)` read `annotations`
  off the action, where they never appear: `annotations` is a property of
  `ToolDefinition`, published in `SessionState.serverTools` and
  `activeClients[].tools`. Every `allow=`/`deny=` entry matched nothing and every
  hint was absent, so all three fell through to `otherwise` and
  `approvals="reads"` denied read-only tools — the exact opposite of its name,
  and indistinguishable from a deliberate refusal. `event_for` now resolves both
  from the mirror into `ToolCallReady.tool_name` / `.annotations`.
- **An auto-confirmed tool call is not an approval request.** A
  `chat/toolCallReady` carrying `confirmed` has already transitioned to
  `running`, and a host emits one for every call it does not gate — including
  every client-provided tool. The client surfaced them all as `ToolCallReady`
  and `TurnStream` answered them, so the ordinary path dispatched a confirmation
  per tool call which the host refused with "no tool call awaiting that id", and
  any consumer prompting a human was asked about a call nobody was asking about.
  They now decode as `ToolCallRunning`, carrying the `contributor` that says
  whether this client is the one meant to execute it.
- **Result review is reachable.** `event_for` was keyed on
  `chat/toolCallResultReview`, an action type in no version of the schema, so
  `ToolCallResultReview` could never be constructed and `TurnStream`'s
  `event.confirm()` arm was dead — the hang that class exists to prevent. A
  review arrives as `chat/toolCallComplete` with `requiresResultConfirmation`,
  and now decodes as one, carrying the `result` it is being asked to approve.
- **`connect()` no longer hangs when the handshake is permanently refused.**
  The supervisor reached `HostState("failed", …)` and returned without setting
  either event `_await_connected` races, so `start(wait=True)` — the default,
  and what `connect()` uses — was unsatisfiable on `-32005`, on a policy close
  with `reconnect=False`, and on an exhausted attempt budget. Both routes to
  terminal now release the wait and raise the classified error, and a refused
  `connect()` closes its runtime instead of leaking it. A refusal the policy
  still retries keeps waiting, unchanged: it is still trying.
- **`AhpClient.shutdown()` reaps the writer after a transport drop.** It
  early-returned on the `closed` state the read loop had already set, leaving
  `_write_loop` parked on the outbox forever — one abandoned task per reconnect,
  each holding a client and a dead transport.
- **A disposed session stops being mirrored.** `root/sessionRemoved` dropped the
  cached summary and nothing else, so the session and its chats stayed in the
  subscription set and the mirror, still reporting `lifecycle: "ready"`, and
  were resubscribed on every reconnect.
- **A turn survives its chat being torn down under it.** `TurnStream` waited for
  a `chat/turnComplete` that a dropped channel can never carry, blocking for the
  whole `idle_timeout` or forever where it is disabled. `root/sessionRemoved`
  and `session/chatRemoved` now end the stream with a `TurnFailed` naming what
  was disposed.
- **A spec-shaped `resourceWrite` no longer empties the file.** `serve/` read
  `params["content"]`; the schema's param is `data`. The absent key decoded to
  `b""` and `write_bytes(b"")` ran, so a conformant host's payload was dropped,
  the file was truncated to zero bytes, and the call answered success with a
  fresh etag. `data` is now required — a missing one is `-32602`, never an empty
  write — and `position` is honoured for every `mode` (`truncate` from the start,
  `append` counting back from EOF, `insert` splicing).
- **Every reverse `resource*` result now matches `commands.schema.json`.** The
  same wrong-key family, none of which anything but a real host could catch,
  because upstream's TypeScript client ships this half with zero
  implementations. `resourceRead` returned `content` where the required
  properties are `data` and `encoding`, so the sibling host answered
  `client returned no content for …` on every read, and `VirtualResourceServer`
  sent no `encoding` at all. `resourceList` entries carried `kind` and a `uri`
  where `DirectoryEntry` is `{name, type}`, so a host keying on `type` per the
  schema treated every client directory as a file and tried to *read* it instead
  of recursing — the plugin expansion this whole layer exists for.
  `resourceResolve` carried `kind` and epoch milliseconds where the schema wants
  `type` and an ISO 8601 `mtime`, and now implements `followSymlinks: false`
  (lstat semantics, `type: "symlink"`, the requested URI echoed back).
- **`resourceRequest` refuses instead of lying.** It answered a successful
  `{"granted": …}` — a property `ResourceRequestResult` does not have — where a
  denial "MUST respond with `PermissionDenied` (-32009)", and it never read the
  `read`/`write` flags, so it granted a write against a read-only mount that
  `resourceWrite` then refused: the deny/retry/deny loop the method exists to
  end. `PermissionDenied.data.request` is now a real `ResourceRequestParams`,
  carrying the required `channel`, rather than an invented `{"reason": …}`.
- **`resourceMkdir` creates the directory it was asked for.** It created the
  contained *parent*, so `mkdir -p a/b` produced `a` and reported success for
  `a/b`. A `uri` that exists and is not a directory is now `-32010`.
- **`failIfExists` is honoured on `resourceCopy`/`resourceMove`,** which
  overwrote the destination and reported success — the caller set the flag
  precisely because it did not want those bytes replaced. Relatedly,
  `resourceDelete` no longer demands `recursive` for an *empty* directory, which
  made the caller pass a flag that also authorises deleting a whole tree.
- **`ResourceRouter` can be installed through the front door.** It implemented
  `__call__` only, so `connect(resources=router)` — the documented way to serve
  more than one mount — answered `-32603 'ResourceRouter' object has no attribute
  'handle'` on every reverse call. It now answers both spellings. A call with no
  `uri` is `-32602` rather than `-32008`, which told the caller its URI was fine
  and the resource was gone, so it stopped asking.
- **A `virtual://` plugin lists its intermediate directories.** `put` registered
  only each blob's immediate parent, so a host walking down from the plugin root
  found `plugins` empty and never reached `plugins/skills/<name>/`.
- **`createChat` had no working call shape at all.** `create_chat(chat)` passed
  its one argument to `_scoped`, which writes it into `channel` — so the *chat*
  URI went out as the scoping channel and the required `chat` key was never
  emitted. Nothing at the call site could fix it: `channel=` in the kwargs was
  overwritten and `chat=` collided with the positional, so every spelling
  answered `-32602 channel and chat are required` and the whole multi-chat
  command was unreachable. It now takes both URIs, because `CreateChatParams`
  requires both and they are different things — the session that will contain
  the chat, and the chat's own client-chosen URI. It is the only caller-scoped
  command whose caller-chosen URI is not the thing being created.
- **The tools a session publishes are now executed.** `create_session(tools=…)`
  advertised a `ToolDefinition` list on `activeClient` and no code path in the
  library ever ran one — `ClientToolHost` was referenced by nothing outside its
  own tests. A client-provided call arrives already `running`
  (`confirmed: "not-needed"`), so there is no approval to give and `TurnStream`
  had nothing to answer with; the host parked the turn on a future nobody would
  resolve. `tools=` now takes a `ClientToolHost` and the session runs a pump
  that executes each call and reports the result, reading the call's `toolName`
  and `contributor` from the mirror because the `chat/toolCallReady` that hands
  execution over carries neither. Passing bare definitions still advertises them
  and now *denies* each call rather than dropping it: advertised-and-absent is
  worse than absent, and a denial at least ends the turn.
- **`terminalCommandPrefix` reaches a `connect()` caller.** The runtime kept
  `protocolVersion`, `defaultDirectory` and `completionTriggerCharacters` from
  the handshake and dropped this one, and re-exported none of them — so no
  consumer of the front door could implement the `!command` shorthand the host
  advertises, or the `@`-mention trigger it kept. All three are now properties
  on `Client`, and an absent or empty prefix reports `None` rather than `""`.

### Notes

- Speaks protocol `0.7.0` and `0.6.0` (`DEFAULT_SUPPORTED_VERSIONS`), and
  **verifies the version a host answers with** — the reference client does not.
- `mcpApps` is not advertised and the `ahp-otlp:` channel is not subscribed to;
  see [ADR 0007](docs/decisions/0007-out-of-scope-for-0-1-0.md).
