# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning is [SemVer](https://semver.org/), independent of the protocol's.
Every release states the protocol versions it speaks.

## [Unreleased]

Under construction. `docs/plan.md` is the design and its §12 is the build order.

### Fixed — the first interop run against a real host

Driven against the sibling [`agent-host-server-py`](https://github.com/jackhubert/agent-host-server-py)
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

### Changed

- An action the client deliberately does not surface — mostly its own writes
  echoing back — is now skipped rather than delivered as `UnknownEvent`.
  `UnknownEvent` is forward compatibility, and conflating the two meant a
  caller watching it for a version mismatch got one on every turn it approved
  a tool or answered a question. A *rejected* echo still reaches the caller,
  which is the case that matters.

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

### Changed

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
