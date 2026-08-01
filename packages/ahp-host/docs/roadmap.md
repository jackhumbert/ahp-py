# Roadmap — from v0.1 to a full feature set

**Status:** proposed. Nothing here is implemented.
[`plan.md`](plan.md) scoped v0.1 and is done through step 6 of its build order;
this document scopes everything after it.

Derived from a scoping pass over all seven unimplemented areas plus an
adversarial critic pass, both grounded in the vendored upstream sources under
`vendor/upstream/` and the VS Code checkout under `.research/vscode`. Every
ordering claim below is argued from evidence, not preference.

**On the evidence.** File-and-line citations to `vendor/upstream/` and
`.research/vscode` were produced by the scoping pass. These were independently
re-verified before this document was written:

| Claim | Verified |
|---|---|
| Fixture corpus splits chat 123 / session 70 / terminal 19 / changeset 16 / annotations 10 / root 7 / resourceWatch 2 = 247 | ✅ counted from `vendor/upstream/test-cases/reducers/` |
| VS Code subscribes to `<session>/annotations` and we answer `{}` | ✅ `tests/integration/fixtures/vscode-1.131-client-requests.json` `requests[0]`, and `core/host.py:265` |
| The replay log is one host-global `deque(maxlen=4096)` | ✅ `core/sequencer.py:49,56` |
| The wire log copies frames verbatim | ✅ `core/wirelog.py:47` |
| `listSessions` discards `limit`/`cursor` | ✅ `core/host.py:270` |

One class of claim is **not** verifiable from this repository and is marked
inline wherever it appears: per-method refusal counts sourced from a live wire
log (`createResourceWatch` ×18, `resourceRead` ×22, and similar). No `.jsonl`
capture is committed and `core/wirelog.py` is untracked. The only committed
counts are in [`experiments.md`](experiments.md) §E12d, which records
`createTerminal` ×1. **Nothing in the ordering below rests on the uncommitted
numbers** — where an area's original scope leaned on them, this document
re-derives the priority from something a reviewer can see, and §8.1 makes
committing the capture a work item.

---

## 0. The target surface is the Agents app

Everything the user has observed — the customization tree, the agent picker, the
terminal toast — is in VS Code's **Agents app**, not the regular editor window.

The Agents app is not a separate product. It is a **window mode of the same
workbench**, selected by `IWorkbenchEnvironmentService.isSessionsWindow`
(`electron-browser/environmentService.ts:155`, `browser/environmentService.ts:272`,
declared `common/environmentService.ts:38`). The same contributions run; the same
`agentCustomizationItemProvider` builds the customization list.

The branch is real rather than cosmetic — `shouldSurfaceLocalAgentHostProvider`
selects *different settings keys* depending on it — so any claim about "what VS
Code does" needs to say which window mode it was measured in. Two consequences
for this roadmap:

1. **The session list is a first-class surface.** In the Agents app the list of
   sessions *is* the home screen. That promotes `root/sessionSummaryChanged` —
   which we never emit — from a nicety to a defect (§8.5).
2. **Features that only pay off in the main editor's chat are worth less.**
   Earlier framing in this session had this backwards. Model-picker registration
   is local-only (`agentHostChatContribution.ts:289,300`, both call sites driven
   by `IAgentHostService` with `LOCAL_AGENT_HOST_AUTHORITY`), so no amount of
   host work surfaces a remote host's models there. That is an upstream issue to
   file, not a roadmap item.

---

## 1. What remains

As of v0.2:

| | Done | Remaining |
|---|---|---|
| Reducers | **7 of 7** | — |
| Conformance fixtures | **247 of 247** | — |
| Commands | 10 | ~19 |
| Channels registered | root, session, chat, annotations | terminal, changeset, resource-watch, otlp |

The reducer work is finished. **Every remaining channel has a complete,
conformant reducer waiting for it** — which is the point of having done them
first: ADR 0004 makes registration all-or-nothing, so a channel can now be
turned on without a port blocking it.

What remains is commands, provider surface, and trust decisions. That is where
the risk lives, and it is why the rest of this document is mostly about
ordering rather than effort.

---

## 2. Release sequence

Five releases. The ordering rule throughout: **land what is silently wrong
before what is merely absent**, and **never register a channel before its
reducer exists**.

That second rule is forced by [ADR 0004](decisions/0004-reduce-every-action-in-scope.md)
— "a host does not choose which actions it reduces; it reduces what it is sent."
The moment `<session>/annotations` is registered, five annotation actions become
client-dispatchable at a host with no annotation reducer.

### v0.2 — Correct at rest ✅ landed

Nothing new on the wire that a client can attack. Every item is either a pure
port, a bug, or a bounded internal change.

| # | Item | Status |
|---|---|---|
| 1 | Port `terminal`, `changeset`, `annotations`, `resourceWatch` reducers — 247/247 | ✅ |
| 2 | Register `<session>/annotations` with `{annotations: []}` | ✅ + `SessionSummary.annotations` counts |
| 3 | Durable `serverSeq` high-water mark | ✅ `core/seq.py`, opt-in via `sequence_file=` |
| 4 | Redact tokens from the wire log | ✅ + owner-only file mode |
| 5 | Emit `root/sessionSummaryChanged` | ✅ as a projection of session state, not an action allow-list |
| 6 | Sequencer: per-channel replay budget, subscription lifecycle hooks | ✅ — `maxLatencyMs` deliberately **not** done, see below |
| 7 | `listSessions` `limit`/`cursor`/`nextCursor` | ✅ keyset cursor |
| 8 | `fetchTurns` | ✅ — implemented, not stubbed |
| 9 | Seed `SessionState.workingDirectories`; validate mutations | ✅ + `Policy.may_grant_working_directory` |
| 10 | Commit the wire capture | ✅ counts only — see §8.1 |

**One item dropped from #6, deliberately.**
`SubscriptionDeliveryOptions.maxLatencyMs` is the protocol's own coalescing
knob, and the critic pass was right that the sequencer is where it belongs. It
was still not built, because no in-tree client sends it — VS Code's `subscribe()`
transmits `{channel}` and nothing else — and honouring it means adding buffering
to the one code path that carries the ordering guarantee. Speculative work
against no caller, on the most safety-critical function in the codebase, is the
wrong trade. It stays listed in §5 as unowned surface.

**Also landed, and not on the original list: a fix for a defect class the
scoping pass did not know about.** An adversarial audit of the three new ports
found that all three — *and* `session.py`, which had already shipped —
conflated JavaScript's `undefined` with its `null` on unconditional spreads, and
used Python `==` where the reference uses `===`. Both are invisible to the
fixture corpus, whose comparator normalises `null` away on both sides. See §8.6.

**Deliberately not here: the terminal toast.** See §7.

### v0.3 — The session configures itself

The release that makes the Agents app render a host as a *configured* thing
rather than a bare transcript. **Total: L–XL.**

1. **[ADR 0005] The suspending provider request** — decided before any code.
   Four features need the identical missing primitive and three separate areas
   were each about to invent it privately (§9.1).
2. **The tool-call and provider surface** — the area the seven-way decomposition
   missed entirely, and which three of the seven declared a dependency on (§9.2).
3. **Root and session config** — schema publication behind a deny-by-default
   `may_set_root_config`; stop discarding `root/configChanged`;
   `SessionConfigState`; `resolveSessionConfig`; `sessionConfigCompletions`.
4. **Active-client lifecycle** — honour `createSession.activeClient` and
   `.config`; seed, update on subscribe, remove on disconnect.
5. **Host-owned customization publication and toggle plumbing**, from
   provider-declared data only. Not the MCP runtime (§10).
6. **`root/progress` and `CreateSessionParams.progressToken`** — notification
   only, no state, no reducer. The only way a host with slow bring-up shows
   anything.

### v0.4 — Files and changes

**Total: XL.** The first release that hands a peer a filesystem API, so every
item is behind a gate.

1. `ResourceProvider` protocol with a **null default**.
2. `Policy.may_access_resource(info, op, canonical_uri)` — a hard gate, not an
   observation (§6).
3. `RootedFilesystemResourceProvider`, read half only: `O_NOFOLLOW` +
   verify-after-open, `realpath`-resolved containment, `file://`-only.
4. `resourceResolve` / `resourceRead` / `resourceList`, plus an honest
   `resourceRequest`.
5. Changeset phases 1–2: catalogue, exact-URI channel registration,
   `changeset/cleared` teardown, an embedder API to publish a file list, and a
   scoped `resourceRead` over a host-owned blob store so diffs render.
6. The resource-watch channel — reducer (landed in v0.2), `createResourceWatch`
   with an **opaque receiver-assigned id**, change coalescing, per-connection cap.

### v0.5 — Durability and auth

**Total: L.**

1. Durable store proper: store protocol, filesystem store, per-channel snapshots
   on a debounce, catalogue restore, crash recovery, lazy provider resume,
   `may_restore_session`. **JSON only** — see §10.
2. Auth phase 1: 13 wire types, `AgentInfo.protectedResources`, `authenticate`
   with a token store and Policy gate, `-32007` with its mandatory `data`,
   `auth/required`.

Deliberately sequenced after v0.4, not before: the store's value is proportional
to how much state is worth persisting, and until config, customizations and
changesets exist a session has one chat and nothing else. Persisting the current
shape means a format migration for every subsequent release.

### v0.6+ — Conditional on demand

Ordered by whether a real consumer exists, not by protocol completeness.

| Item | Blocked on / conditional on |
|---|---|
| Terminal PR 2 (plumbing + refusing backend) | Its own merits — `ToolResultTerminalContent`, not the toast (§7) |
| Terminal PR 3 (PTY backend) | A **separate distribution or `[pty]` extra** (§6) |
| Multi-chat: `createChat`/`disposeChat`, chat catalogue, `AgentCapabilities` | The only item that changes the host's data model (`_Session.chat_uri` → a dict) |
| Elicitation | ADR 0005 |
| Changeset phases 3–5 (host-side diffing, operations, review) | The memory model in §5 |
| Auth phase 2 (0.6.0 step-up) | A provider that actually fronts MCP servers. Dead code until then |
| The `resource*` write half | A **second, separate** opt-in on top of the read opt-in |
| Server→client request direction | Its only consumer is client-published plugin ingestion |
| MCP server registry | The unresolved question in §9.3 |
| Telemetry (`ahp-otlp:`) | No consumer. VS Code discards traces and metrics |

---

## 3. Per-area detail

### 3.1 Terminal — L

11 actions, a 91-line reducer, 9 state types, 2 commands. Splits into three PRs,
and **the first two contain no process execution whatsoever**.

- **PR 1 (S)** — reducer only. 219/247. `createTerminal` still answers
  `MethodNotFound`. *(Folded into v0.2 item 1.)*
- **PR 2 (M)** — `createTerminal`/`disposeTerminal`, channel registration, the
  root catalogue, claim tracking and gating, `Policy.may_create_terminal`, the
  `TerminalBackend` protocol, and a `RefusingTerminalBackend`.
- **PR 3 (L)** — the PTY backend and the OSC 633 parser. Only this PR introduces
  execution, and it does not belong in this distribution (§6).

Two upstream facts make the port cheap: the terminal reducer, actions and
commands are **byte-identical** between the 0.6.0 npm package and the 0.7.0 spec
tag (only `TerminalState.isPty?` was added), and every terminal action is
registered `'0.1.0'` in `types/_generated.py:276-286`, so there is **zero**
outbound version-filter work.

**Do not route on the URI scheme.** VS Code uses three forms, none of them
`ahp-terminal:` — `agenthost-terminal:/<uuid>` (in our own captured fixture),
`agenthost-terminal://bang/<uuid>`, and `agenthost-terminal://shell/<sid>/<toolCallId>`.
`core/channels.py:29` defines `_TERMINAL_PREFIX = "ahp-terminal:"`, matching
none of them. Add `Sequencer.reducer_of(uri)` and validate against the bound
reducer. (Note `core/host.py:497` already gates chat validation via
`classify()`; that is safe *only* because the host mints its own chat URIs. Do
not extend the pattern.)

Four porting hazards, **none covered by the corpus**, because
`reduced_equal` normalises `None` away:

- `terminal/exited` spreads `{...state, exitCode: action.exitCode}` — an absent
  `exitCode` sets the key to `undefined`, which `JSON.stringify` **drops**.
  Python must delete the key, not write `None`. Same for both optional fields in
  `terminal/commandFinished`. Reuse the `_with_optional` pattern at
  `reducers/session.py:127-139`.
- `!tail.isComplete` is JS truthiness, and here it is the *intended* semantic
  (absent ≡ incomplete). A legitimate exception to invariant 5 — comment it, or
  someone will "fix" it.
- `terminal/cleared` is client-dispatchable, so **any peer can wipe another
  peer's scrollback**.
- `InitializeResult.terminalCommandPrefix` is a **separate opt-in**: it tells VS
  Code that a chat message starting with `!` is a shell command to run. Never
  advertise it without a real backend and an explicit policy — it converts
  arbitrary chat text into arbitrary shell.

### 3.2 Resources and watches — XL (not L)

Nine bidirectional methods, a provider abstraction, a TOCTOU-safe jail, a new
channel with subscription-scoped lifecycle, sequencer surgery, **and a new
transport direction**.

That last piece is the most under-rated item in the whole roadmap. Server→client
requests convert `Host.serve`'s read loop from request-in/response-out to full
duplex, and it collides with two invariants: **#10** (one queue, one writer task)
and **#17** (a notification handler must not let an exception escape) — because
now a *response* can arrive for a request whose connection died, and an in-flight
host-initiated request needs a timeout, a cancellation path, and defined
behaviour when the peer vanishes mid-call. **That is L on its own.**

The read increment is the right first slice for four reasons: it is the whole
`ContentRef` mechanism, without which any provider reporting file edits produces
unopenable diffs; its failure mode is information disclosure, which is bounded
and reversible, whereas the write half is neither; the read path is
TOCTOU-safe with `O_NOFOLLOW` + verify-after-open while the write path
additionally needs per-path locking, atomic create and destination re-checking;
and `resourceRequest` is cheap and stops the deny→retry→deny loop.

**One free win.** `CreateResourceWatchResult.channel` is specified as
*receiver-assigned* (`types/channels-resource-watch/commands.ts:79-85`), so
minting an unguessable opaque id is the **spec-conformant** behaviour — VS
Code's base64-descriptor scheme is an implementation choice we are not obliged
to mirror. Related, and worth filing upstream: VS Code's own `subscribe` handler
attaches a watcher with no permission check
(`protocolServerHandler.ts:1140-1145` → `agentService.ts:3448-3502`).

### 3.3 Changesets — L

Five phases; the first two are independently useful.

- **Phase 1 (S+M)** — reducer + 16 fixtures + catalogue + channel registration +
  `changeset/cleared` teardown + an embedder API to publish a file list.
- **Phase 2 (M)** — the content store and scoped `resourceRead`.
- **Phase 3 (L)** — neutral `file_edited` provider event + host-side diff.
- **Phase 4 (M+S)** — operations, `invokeChangesetOperation`, the Policy gate.
- **Phase 5 (M)** — review capability, `filesReviewChanged` validation.

Two corrections to the original scope. Phase 1's promise — "per-file paths and
+/- counts" — **overstates what Phase 1 delivers**: the counts require a diff,
and diffing is Phase 3. Phase 1 delivers counts only if the *embedder* computes
them. And Phase 3 hides a real memory model: host-side diffing means retaining
before *and* after bytes for every file the agent touches, for the life of the
session, in a host that is not yet durable.

The dependency on `resourceRead` is **one method plus a private-scheme
allow-list**, not the nine-method family. VS Code intercepts its own
`git-blob:`/`session-db:` schemes at the top of `resourceRead`
(`node/agentService.ts:3103-3117`) before touching the filesystem; we can do
exactly the same and expose no general filesystem API.

**Invariant 15 and the deferral of compare-turns.** Changeset URIs are minted by
the host, so an exact-string registry works and no parsing is needed —
`types/channels-changeset/state.ts:37` says a variable-free template "is itself a
subscribable URI". But that holds **only** while the catalogue contains no
`{turnId}`/`{originalTurnId}` templates, which are expanded *by the client* with
values the host cannot enumerate. So: the compare-turns deferral is what
preserves invariant 15, and un-deferring it requires an ADR amendment, not a
code change.

### 3.4 Authentication — Phase 1 is M, not L

The reducer cost is zero (all 14 fixtures already pass — `reducers/chat.py:904,940`,
`reducers/session.py:356,371`). Phase 1 is 13 wire types, one command, one error
code, one notification, one Policy hook, one root-state field, and the wire-log
redaction. **The L lives entirely in Phase 2, and Phase 2 is blocked on the
tool-call area** — `ToolCallAuthRequiredState.contributor` is narrowed to
`ToolCallMcpContributor`, and `core/turn.py:90` emits no `contributor` at all,
so step-up is structurally unreachable today.

The host is a **courier, not an authenticator**: `authenticate` pushes tokens for
upstream services the agent talks to. It never gates the AHP connection.

### 3.5 MCP, customizations and config — XL, with one unresolved question

The **ship-first** slice is genuinely valuable and needs no new transport
direction: root config schema behind a deny-by-default hook, `SessionConfigState`
+ `resolveSessionConfig` + `sessionConfigCompletions`, honouring
`createSession.activeClient`/`.config`, and the customization wire types. That
combination makes VS Code render config chips, populate the agent picker, and
stops us discarding ten policy-relevant actions per connection.

**Client-published plugin ingestion is genuinely blocked** on reverse
`resource*`. Until then, accept and store the `ClientPluginCustomization`
verbatim on `activeClients[].customizations` — the reducer already does this
correctly — and never expand it. That is spec-legal: absent `children` means
"the host has not parsed this container yet"
(`types/channels-session/state.ts:779-786`), which is exactly true.

**This also explains what the user observed.** Plugin *children* come from
filesystem expansion (`agentCustomizationContentExpander.ts:36-60,183` →
`fileService.canHandleResource` → `resolveAll` → `readFile`), not from the
protocol's `children` array — which is why Skill Charlie, Prompt Delta, the two
Instructions and Hook Golf never appeared, while AHS Skill Hotel did (directory
children *are* read from the array, `toDirectoryItems`, line 108). Directories
render transparently, and `mcpServer` entries hit an explicit `continue`.

The unresolved question is in §9.3, and it should be settled before any of this
area is estimated.

### 3.6 Durable state — L, minus the counter

Three ships; the first alone fixes the user-visible complaint. Ship 2 (durable
action log and replay across a restart) is honestly **the lowest-value third of
the area**: no client currently benefits — VS Code's own host cannot replay
across a restart either (in-memory `_serverSeq` and `_replayBuffer`, `NotFound`
for an unremembered client at `protocolServerHandler.ts:676`) — and its client
handles a post-restart snapshot fine. Build the log because it is also the
crash-recovery mechanism for the window between snapshots, not because replay
demands it.

**One item must be split out and shipped alone, now**: the durable `serverSeq`
high-water mark. See §8.3.

One concurrency correction: "durable seq allocation, durable log append" is not
plumbing. `Sequencer.publish` is *the* critical section (invariant 7). Awaiting a
disk write inside that lock serialises the entire host on fsync. Allocating
durably outside the fan-out path, or batching with a group commit, is a design
problem.

### 3.7 Remaining protocol surface — XL

Three tranches. **Tranche 1** is folded into v0.2 above (annotations, multiroot
seeding, the `fetchTurns` stub) — it removes three ways the host is quietly
wrong. **Tranche 2** is multi-chat. **Tranche 3** is optional, ordered by
consumer: elicitation first (it is the only ADR-0003 interface change, so getting
it wrong is expensive later), then completions alongside `resolveSessionConfig`,
telemetry last.

Two corrections. `AgentCapabilities` enforcement is **not S**:
`multipleChats.fork`/`sideChat` and `multipleWorkingDirectories.immutablePrimary`
each imply host-side enforcement of client MUSTs the protocol enforces nowhere.
And telemetry is S only because it emits nothing — the moment it does, you owe an
OTLP encoder.

**Annotations has no upstream prose at all.** `docs/specification/comments-channel.md`
and `docs/guide/comments.md` are both **zero-byte files** at the pinned tag, and
the URI table in `docs/specification/subscriptions.md:26-34` does not list an
annotations channel. The only normative-ish source for `<session>/annotations` is
a doc comment on `SessionSummary.annotations`. Our implementation therefore rests
on a doc comment plus an observed client — say so in the code, and file it
upstream (§11).

---

## 4. The missed area: tool calls and the provider surface

Three of the seven scopes declared a dependency on this and **none owned it**.
It is the single largest hole in the decomposition, and it must precede
changesets, auth phase 2 and multi-chat.

All of it is already reduced. Nothing emits or gates any of it:

- `chat/toolCallReady`, `chat/toolCallConfirmed`, `chat/toolCallResultConfirmed`
- `ToolCallPendingConfirmationState` (`types/channels-chat/state.ts:1258`)
- `ToolCallContributor` (`:1145`), `ToolCallClientContributor` (`:1125`)
- `SessionActiveClient.tools`

`core/turn.py` has `text_delta`, `reasoning_delta`, `tool_call_started`,
`tool_call_completed`, `turn_failed` — no confirmation, no contributor, no
client-tool round trip.

This is not theoretical. [`experiments.md`](experiments.md) §E12e and the
captured fixture show VS Code shipping its **entire client tool set** on
`createSession.activeClient.tools`, with full input schemas — which the host
discards.

**Client-tool execution is arguably a better first move than the resource
family.** The host emits `chat/toolCallStart` with
`contributor: {kind: 'client', clientId}`, the client executes, the client
dispatches `chat/toolCallComplete`. That gives an agent the *editor's own tools*
with **no filesystem API at all** — no jail, no TOCTOU, no new transport
direction.

And tool approval is the exact hole [`README.md`](../README.md) already
advertises as a live defect: "the ability to approve **any other client's**
pending tool call — the protocol's own validation table conditions tool-call
approval on the call's *status*, never on client identity." Seven areas, and the
one the README names as a security defect belonged to none of them.

Also in this area: the **chat-queue loop** — `chat/pendingMessageSet` /
`chat/pendingMessageRemoved` / `chat/queuedMessagesReordered`. That is a visible
feature (typing while the agent runs) and it requires the host to actually
dequeue and start the next turn.

---

## 5. Other unowned surface

Each of these is small, real, and belonged to no area:

- **`root/sessionSummaryChanged` is never emitted.** `grep` finds only
  `root/sessionAdded` at `core/host.py:371`. Every mutation of a summary field —
  title, status, `changes`, annotation counts, `isRead`, activity — must be
  mirrored to root. In the Agents app this is the home screen (§0).
- **`SubscribeParams.delivery` / `SubscriptionDeliveryOptions.maxLatencyMs`**
  (`types/common/commands.ts:376,410-416`). The protocol's own coalescing
  contract. Two areas hand-rolled coalescing while this went unclaimed.
- **`listSessions` `limit`/`cursor`/`nextCursor`** — discarded at
  `core/host.py:270`.
- **`InitializeResult.defaultDirectory`** (`types/common/commands.ts:239`) — one
  line, named nowhere.
- **Session lifecycle**: `session/isReadChanged` (in the live capture),
  `session/isArchivedChanged`, `session/activityChanged`, `chat/activityChanged`,
  `session/metaChanged`, `chat/usage`, `chat/draftChanged`. And `chat/truncated`,
  which is client-dispatchable and **destructive** — a peer can delete turns,
  with no identity check and no owner.
- **`AgentInfo.models`** — the model picker data. Related: `RootState.agents` is
  plural and `Host` holds a single `self.provider`; multi-agent on one host is
  unclaimed.

---

## 6. Security gates

Hard ordering constraints. Strictest first. These are gates, not preferences —
each names something that must exist *before* a feature ships, not alongside it.

**No PTY backend before** `may_create_terminal` with no permissive default · the
cwd jail checked on the `realpath`-resolved path with `file://`-only scheme
acceptance · an env allowlist rather than `os.environ` inheritance ·
`terminal/input` claim-gated · the `--allow-remote` × terminals hard error ·
**the sequencer replay budget** (a shell that floods the global log is a denial
of service against every *other* client's reconnect — a protocol-correctness
failure, not a performance one) · and **an exclusion rule in any durable action
log**, because `terminal/data` in a durable log is the full scrollback of every
command the agent ran, written unencrypted, outliving the connection.

**No `resource*` at all before `may_access_resource(info, op, canonical_uri)`.**
Everything in that family is on `ahp-root://`, so `may_see_channel` — the only
per-resource hook we have — buys literally nothing. **No write half before a
second, separate opt-in.**

**No `createResourceWatch` before** the sequencer lifecycle hooks and a
per-connection watch cap.

**No root config schema before `may_set_root_config` with deny-by-default, and
`mcpServers` never in a default schema.** This is the sharpest constraint in the
roadmap because the danger is *latent right now*: `root/configChanged` is already
client-dispatchable (`types/_generated.py:115`), VS Code already sends it ten
times per connect, and the only thing protecting us is that `RootState.config` is
absent so the reducer's guard drops it. Publishing a schema converts an accident
into a feature in one commit.

**No `authenticate` before wire-log redaction.** `core/wirelog.py:47` is
`entry: dict[str, Any] = dict(message)` — a verbatim frame copy. The first
`authenticate` writes a live bearer token in plaintext to a file that is
truncated on open but never mode-restricted. Fix it in the same PR, not after.
The related gate — excluding tokens from a durable action log by construction —
was claimed by both the auth and durability areas and owned by neither; it is
hereby assigned to **whichever ships second**.

**No `invokeChangesetOperation` handler registry before `may_invoke_operation`**,
and no built-in handlers ever.

**No honouring of `session/workingDirectorySet` before `may_grant_working_directory`.**
Note the hidden coupling: "stop discarding `createSession` params" is one line
away from "honour `workingDirectories`", and that one line crosses a security
boundary the config work does not otherwise touch.

### The one feature that does not belong in this distribution

**A real PTY backend in the core wheel.** The `--allow-remote` hard error is
necessary but **not sufficient**, because it defends only the configuration we
thought of. In a library whose README says *single-trust-domain*, and whose
`Policy` cannot authenticate a peer at all — `reconnect` resumes on a
client-asserted `clientId` with no credential, `core/host.py:449-455` — shipping
importable arbitrary command execution in the default wheel is the wrong default
*even behind a constructor argument*. The next person's mistake is
`TerminalBackend` being one import away, not `allow_remote=True`.

Put the POSIX PTY backend in a separate distribution or a `[pty]` extra, exactly
as [`plan.md`](plan.md) §11 step 7 does for the ACP adapter. `TerminalBackend` as
a protocol with no implementation in core is the honest shape and costs nothing.

Weaker but real, same argument: `resourceWrite`/`Delete`/`Move` in core, and the
`mcp://` proxy that forwards client-originated `tools/call` into a live MCP
server.

---

## 7. On the terminal toast

The user's stated irritant is the *"The terminal process failed to launch: Method
not found: createTerminal"* modal. **Fixing it first is a trap. Porting the
terminal reducer first is right, for a different reason.**

Three specifics:

1. **The committed evidence says one modal per session.**
   [`experiments.md`](experiments.md) §E12d records `createTerminal` ×**1**. The
   larger counts cited during scoping come from an uncommitted wire log (§0's
   evidence note).
2. **The proposed fix is ~250 lines** — channel plumbing, a claim model, a
   Policy hook, a backend protocol and a README rewrite — to convert one modal
   into a differently-worded in-terminal message. Compare v0.2 item 2: ~30 lines
   to fix a subscription that is **permanently broken on every connect**. The
   irritation-per-line ratio is an order of magnitude apart.
3. **The toast is not a bug — it is the protocol working.** AHP has no server
   capability object, so `MethodNotFound` *is* how a host says no, and invariant
   14 says we decline loudly.

**So:** ship the reducer in v0.2, ship a README sentence that says the toast is
intentional, and buy the plumbing later on its real merits — which are
`ToolResultTerminalContent` (streaming live command output into a tool call),
not the toast. Notably, VS Code's own *output-only* terminals have no PTY behind
them at all (`copilotNonPtyShellTerminals.ts:18-20,187-195`), so the
highest-value part of this channel needs no shell execution.

**One cheap check before spending PR 2:** whether VS Code's terminal-profile
registration (`agentHostTerminalContribution.ts:128-136`) is gated on anything
the host advertises. If it is, the cheapest fix is not implementing
`createTerminal` at all.

---

## 8. The ten v0.2 items, argued

### 8.1 Commit the wire capture — done, counts only

Two of the seven original scopes were ranked against evidence a reviewer could
not see. The `.jsonl` itself stays out: it is a full transcript of a real
conversation, and redacting credentials does not redact that. So
`tests/integration/fixtures/vscode-1.131-client-requests.json` gained a
`measured` block of **method names and counts only**, pinned by
`tests/integration/test_vscode_trace.py`.

Measured over three sessions of a live VS Code 1.131 Agents-app connection:

| Method | Calls | Per session |
|---|--:|--:|
| `ping` | 1057 | ~350 |
| `resourceResolve` | 86 | ~29 |
| `dispatchAction` | 38 | ~13 |
| `resourceRead` | 35 | ~12 |
| `createResourceWatch` | 27 | 9 |
| `resourceList` | 16 | ~5 |
| `resolveSessionConfig` | 3 | 1 |
| `createTerminal` / `disposeTerminal` | 2 / 2 | <1 |

Two corrections to this document fall straight out.

**The ordering holds, and now on evidence.** 164 `resource*` calls per three
sessions against 2 `createTerminal` — an eighty-fold difference. v0.4 being the
resource family, and §7 declining to spend 250 lines on the terminal toast, are
now measured rather than argued.

**The scoping pass's `createTerminal` ×7 was wrong**, as the critic suspected.
It is closer to one per session, matching [`experiments.md`](experiments.md)
§E12d's ×1. Nothing in the ordering depended on the inflated figure.

**One method nobody costed shows up once per session**: `resolveSessionConfig`.
It is already in v0.3, and this is a second reason for it to be there.

### 8.2 Annotations — the only measured client damage

`remoteAgentHostProtocolClient.ts:863-869` is `if (!result.snapshot) throw new Error(...)`.
Our `_subscribe` returns `{}` for an unregistered channel (`core/host.py:265`).
VS Code subscribes to `<session>/annotations` **unconditionally** — the committed
capture proves it on the *reconnect* path, `requests[0].params.subscriptions`.

So this throws on every connect **and** lands the URI in `missing[]` on every
reconnect. Register the channel with `{annotations: []}` at session creation.
~30 lines, needs no new Policy hook (`may_dispatch` already exists and is already
consulted), and it is the cheapest user-visible correctness win available.

### 8.3 The `serverSeq` high-water mark

`clients/typescript/src/client/hosts/runtime.ts:679-681` takes a **max** of the
sequence. Once we reset to 0 on restart, that client can never replay again —
permanently, not until the next reconnect. A durable monotonic counter closes it
and composes with the existing epoch check at `core/sequencer.py:230`
(`from_previous_epoch = last_seen_server_seq > self._seq`) with no generalisation
needed. Reserve in blocks; skipping numbers across a restart is legal.

~20 lines. It does **not** need the rest of the store, and bundling it there
delays it behind an L-sized rewrite for no reason.

### 8.4 One sequencer PR

`core/sequencer.py` should be touched once, in its own PR, before any channel
area goes near it. Three things belong in it:

- **A per-channel replay budget** inside the single `deque(maxlen=4096)`
  (`core/sequencer.py:56`). `terminal/data` at even 50 frames/s exhausts that
  buffer in about eighty seconds, after which **every** channel's `reconnect`
  degrades to a snapshot — including a chat that produced no traffic at all.
  `resourceWatch/changed` on a recursive watch has the same shape. This gates
  two areas and belonged to neither.
- **Subscription lifecycle hooks** — needed by the watch channel.
- **`SubscriptionDeliveryOptions.maxLatencyMs`** — the protocol's own coalescing
  knob, implemented in the one place it belongs.

This does **not** call for revisiting the host-global-`serverSeq` decision.
Invariant 7 stands; the fix is a budget inside the one log.

### 8.5 `root/sessionSummaryChanged`

Promoted from "nice" to "defect" by §0: in the Agents app the session list is the
home screen, and every summary mutation we fail to mirror leaves it stale.

Implemented as a **projection**, not an action allow-list. `SessionState`
"inlines (denormalizes) every `SessionMetadata` field directly onto itself", and
the spec says outright that "the host keeps the two in sync via
`root/sessionSummaryChanged`" — so the host diffs the session channel's own
state after every publish and emits whatever moved. That is automatically
correct for actions we do not emit yet, and cannot drift when one is added.
Turn-scoped `modifiedAt` churn is coalesced to the turn's two ends, which the
spec explicitly permits.

### 8.6 The defect class the scoping pass missed

Not in the original ten. Found by pointing an adversarial audit at the three
fresh ports, which then implicated a fourth reducer that had already shipped.

Two mistranslations, both systemic:

- **An unconditional JS spread writes `null` through.**
  `{...state, title: action.title}` drops the key only when `action.title` is
  *absent*; an explicit `null` is a value and survives `JSON.stringify`. Every
  reducer here used a helper that deleted the key whenever the value was `None`,
  merging the two cases.
- **`===` is not `==`.** Python matches `True` against `1`, matches an absent
  key against `null`, and compares dicts structurally where `===` compares them
  by reference. Every id lookup — `findIndex(x => x.id === needle)` — is over
  peer-supplied data, so this selects a *different entry* than the reference
  does. In `annotations` and `changeset`, both client-dispatchable, that let a
  peer remove or rewrite an entry it never named.

Neither is visible to the 247-fixture corpus: its comparator normalises `null`
away on both sides, by necessity, because the upstream fixtures encode an absent
optional *as* `null`.

The fix is `reducers/js.py` — one module with `UNDEFINED`, `get`, `assign`,
`strict_equal`, `index_of`, `key_of` and `to_string` — plus AGENTS.md invariant
19 requiring its use.

The verification is a second corpus. 43 adversarial cases are run through the
**real pinned TypeScript reducers** under `node --experimental-transform-types`,
and their output is frozen verbatim, nulls and all
(`scripts/regenerate_js_semantics.sh` → `tests/conformance/fixtures/js-semantics.json`).
The test compares byte-for-byte with no normalisation and runs offline. It was
confirmed to discriminate: against the pre-fix code these cases fail.

Hand-written expectations were not an option here — they would only restate the
reading under test, which is precisely what went wrong the first time.

---

## 9. Cross-cutting decisions to make once

### 9.1 ADR 0005 — the suspending provider request

The `TurnSink` today is strictly fire-and-forget. **Four features need the same
missing primitive**: a provider-initiated request that suspends until an action
arrives from *some other connection*, with cancellation and a turn-scoped
lifetime.

| Feature | What it suspends on |
|---|---|
| Elicitation | `docs/guide/elicitation.md` step 5, "resume the blocked operation" |
| Tool-call confirmation | A peer's `chat/toolCallConfirmed` |
| Auth step-up | A pushed token routed to the backend that wanted it |
| Terminal claim hand-back | A peer's `terminal/claimed` |

Three areas each proposed their own version: a challenge registry, "a
request/response neutral event under ADR 0003", and "neutral provider events so
an agent can own a terminal". Three registries, three lifetimes, three
cancellation semantics.

**This is the highest-leverage design decision in the roadmap.** Decide it once,
as ADR 0005, before any of the three lands. It is also why §4 exists.

### 9.2 The tool-call area needs an owner

See §4. It is a prerequisite for three later areas and it is where the README's
advertised security defect lives.

### 9.3 Who produces the MCP server stream?

`McpServerCustomization` **state** is in scope. The runtime that produces it may
not be. The scoping item "port `McpCustomizationController` as a host-side
`McpServerRegistry`" describes porting a 555-line controller that "takes a
neutral `{name, state, enabled}` stream" — true, but **something must produce
that stream**, and it does not exist here.

Two readings, and the spec contradicts itself across two pages:

- `docs/guide/customizations.md:16` — the host "resolves the containers, parses
  their contents, and exposes the result".
- `docs/guide/mcp.md:9` — everything below the AHP state "lives in the agent
  harness the host wraps"; the host's job is to "normalize whatever the harness
  exposes".

If the **provider** supplies it, the item is M and the host never spawns
anything. If the **host** implements an MCP client — stdio/HTTP transport,
JSON-RPC framing, `initialize`, `tools/list`, `tools/call`, lifecycle,
restart-on-crash — it is XL *and* it is process spawn, contradicting both this
project's security posture and upstream's own anti-goals.

**This project's answer is the provider** (§10), consistent with ADR 0003 and
with adapters living in their own distributions. Settle it in writing before
estimating any of §3.5.

Related and worth stating plainly: `mcpMethodCall`/`mcpNotification` are **not in
`CommandMap` at all** — they are the unmatched-method fallback
(`node/protocolServerHandler.ts:1447-1467`). That changes what "implementing"
them means.

### 9.4 ADR 0001 is strained by customization parsing

Expanding a `ClientPluginCustomization` means the host **constructs** `children`
and re-publishes them via `session/customizationsChanged`. ADR 0001's decisive
requirement is #6 — the host is authoritative for state it replays to clients
*newer than itself*. If the plugin parser builds that tree through closed models,
unknown fields in a client-published plugin are dropped and **the host silently
corrupts state for every newer client**. The parser must emit plain dicts and
preserve unknown keys verbatim, exactly like the reducers.

### 9.5 A checkpoint on ADR 0002, not a decision

Terminals add zero version-filter work today. But the debt compounds:
`TerminalState.isPty`, plural `SessionState.workingDirectories` and
`McpServerAuthRequiredState` are all 0.7.0-only, and every future area adds
outbound filtering. Measured evidence ([`experiments.md`](experiments.md) §E12c)
is that VS Code sends `['0.7.0']` alone, and the only 0.6.0 consumer is the npm
client in one interop test.

**Set an explicit checkpoint after v0.3** to re-ask whether 0.6.0 survives. Do
not decide it now — and do not let it be decided by accretion either.

---

## 10. Permanently out of scope

Measured against `docs/guide/doctrine.md:69-79`. These are not deferrals.

- **An MCP client runtime.** Spawning or connecting MCP servers, transports,
  `tools/list`, `tools/call`, restart-on-crash. The anti-goals name "a universal
  backend tool registry or tool schema" and "how agents reason, plan, call
  tools". If `McpServerRegistry` spawns processes, it does not belong in this
  distribution.
- **A PTY backend in core, and shell-integration injection.** The injection
  scripts are a VS Code product artifact keyed on `VSCODE_*` env vars and a
  nonce; vendoring them puts one vendor's shell hooks into a neutral library. The
  OSC 633 *parser* is a different matter — the MUST-strip rule
  (`terminal-channel.md:112`) applies whether or not we inject.
- **Anything git.** `branch`/`uncommitted` change kinds, merge-base baselines,
  checkpoint refs, `refs/agents/<sid>/reviewed`. The anti-goal is explicit: "a
  requirement that every workspace has a local filesystem or Git repository."
  The review store must not be git-backed either, and a git-backed changeset
  *producer* is an adapter distribution.
- **Built-in changeset operations.** `commit`, `sync`, `create-pr`,
  `discard-changes` are VS Code private string constants, not protocol names.
  `create-pr` is a credentialed network call; `discard-changes` irreversibly
  destroys user work. Not "off by default" — **no implementations in this
  repository at all.**
- **Model routing.** Pass `AgentInfo.models` through from the provider and stop.
  The host is a courier: it never authenticates, and it never selects a model.
- **Agent-to-agent coordination.** `chat-channel.md:131` makes chat fan-out the
  host's problem, so a per-session cap in `Policy` is in scope. Scheduling,
  delegation and result aggregation between chats are anti-goals.
- **A headless terminal emulator.** Cursor-position emulation is
  terminal-emulator work, not protocol work. Document that programs querying
  terminal state may hang; do not fake it.
- **`pickle`, `eval`, or any `__reduce__`-capable store format**, even as an
  option. JSON only. This is the single most likely Python-shaped
  remote-code-execution mistake in the whole roadmap, so it belongs in an ADR,
  not only here.

---

## 11. Upstream questions this raises

To be appended to [`research.md`](research.md) §11. Per the user's standing
instruction these are **logged, not filed**.

1. **`disposeChat` contradiction.** `chat-channel.md` §Disposal says "the
   protocol does not currently expose a `disposeChat` command", but
   `types/channels-chat/commands.ts:133` defines `DisposeChatParams`,
   `types/common/messages.ts:157` maps it, and the reference host implements it.
2. **`createChat` URI allocation.** `chat-channel.md:71` says "the server
   allocates the chat URI"; `CreateChatParams.chat` is documented "client-chosen"
   and VS Code's client sends one.
3. **`CompletionsParams.channel`** is documented as the *chat* URI; VS Code sends
   the *session* URI while upstream's own e2e suite sends a chat URI.
4. **The annotations channel has no specification.** `comments-channel.md` and
   `guide/comments.md` are zero-byte files at the pinned tag, and the URI table
   in `subscriptions.md:26-34` omits the channel.
5. **OTLP `{level}` template form.** `telemetry-channel.md:29,121` gives the
   RFC 6570 form-style `ahp-otlp://logs{?level}`; the reference host ships
   path-form `ahp-otlp://logs/{level}`. A host emitting one and a client
   expanding the other subscribe to different URIs and see nothing.
6. **Is `resourceResolve` normatively required to canonicalize?** The spec calls
   it "the combination of POSIX stat and realpath" and says the result is the
   canonical URI after symlink resolution; the reference host returns the
   requested URI verbatim and ignores `followSymlinks` entirely.
7. **Is there any size bound on `resourceRead`?** No Range parameter, no
   chunking, no documented limit. What should a host do with a 1 GB file?
8. **What is the stability contract on `etag`?** "An opaque per-provider version
   token" with no format and no stated invariant. `W/"size-mtimeMs"` cannot
   distinguish two same-size writes inside one millisecond — a real lost-update
   window for the `ifMatch` flow the etag exists to protect.
9. **Per-connection or host-global token store?** `authentication.md:209-211`
   says auth is per-connection; the reference keys a single host-global map with
   no client identity.
10. **`changeset/operationsChanged` with explicit `null`** leaves
    `operations: null` in the reference reducer's state, contradicting its own
    `operations?: ChangesetOperation[]` type. Fixture 141 exercises exactly this
    and passes only because the corpus comparator drops null-valued keys on both
    sides.
11. **VS Code's `subscribe` handler attaches a resource watcher with no
    permission check** (`protocolServerHandler.ts:1140-1145` →
    `agentService.ts:3448-3502`).
12. **Remote hosts register neither a language model provider nor a chat session
    provider** — both call sites are gated on `LOCAL_AGENT_HOST_AUTHORITY`, so a
    remote host can never appear in the model picker.
13. **`hiddenSections` keys on a hardcoded provider id** (`copilotcli`), which
    hides Tools and Prompts for every third-party host.

---

## 12. Effort summary

| Release | Theme | Effort | New attack surface |
|---|---|:--:|---|
| v0.2 ✅ | Correct at rest | M–L | **None** |
| v0.3 | The session configures itself | L–XL | Config keys, behind a deny-by-default gate |
| v0.4 | Files and changes | XL | A filesystem read API, behind a null-default provider and a hard gate |
| v0.5 | Durability and auth | L | Disk, and a token store |
| v0.6+ | Conditional | — | Per item; PTY never in core |

Revised effort ratings against the original scoping pass: resources **L → XL**,
auth phase 1 **L → M**, MCP/config **L → XL**, `AgentCapabilities` **S → M**,
OSC 633 **M → L**, backpressure **M → L** *(and reassigned from terminals to the
sequencer)*.
