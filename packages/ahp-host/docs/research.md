# Phase 1 — Research

**Status:** living document. Last verified **2026-08-01** against
`microsoft/agent-host-protocol` @ `bd27d354b39c1b2090fbcc6db392d406b743280c`
(2026-07-31), latest released spec tag `spec/v0.7.0`, `main` declaring
`PROTOCOL_VERSION = 0.8.0` (unreleased).

Companion documents:

- [`experiments.md`](experiments.md) — the empirical log. Every claim here that
  is marked *measured* is reproduced there with commands and output.
- `plan.md` — phase 2. Not written until this document is reviewed.

Citation convention: paths are relative to the upstream checkout unless
prefixed. `.research/` holds pinned clones and is git-ignored; see
`UPSTREAM.md` for the pinning procedure.

---

## 0. Executive summary

Nine things determine the shape of this project. In rough order of consequence:

1. **Target 0.7.0 *and* 0.6.0.** **VS Code** — the client that matters — vendors
   upstream's `types/` directly and offers the full list
   `['0.7.0','0.6.0','0.5.2','0.5.1']`, preferring 0.7.0. The newest client
   installable from **npm** is only **0.6.0** (there is no `typescript/v0.7.0`
   release), so supporting both covers VS Code and keeps the CI counterparty
   working. The cost is near zero: the whole 0.6.0→0.7.0 action delta for our
   channels is six actions, all out of v0.1 scope. *(measured, E1, §1a)*

2. **Reducers cannot be generated — every non-TypeScript client hand-ports
   them** and gates the port on a shared 247-fixture JSON corpus. That corpus
   is language-neutral, and Python consumes it unmodified. *(measured, E6/E7)*

3. **The reducers are not pure.** `chatReducer` reads the wall clock in six
   places. Every port injects a clock; the corpus pins it to `9999`.
   *(measured, E5)*

4. **The reference client validates nothing.** It accepted an unoffered
   protocol version, a missing required field, a malformed `reconnect` result,
   a `serverSeq` gap, and a `createSession` addressed to the wrong channel.
   Conformance must be self-enforced. *(measured, E3)*

5. **`serverSeq` is a single host-global counter**, not per-channel and not
   per-connection — `reconnect` carries one scalar `lastSeenServerSeq` for all
   subscriptions. This is a hard structural constraint on the concurrency
   design.

6. **There is no server capability object.** `InitializeResult` has no
   `capabilities` field. A host declines a feature by returning
   `MethodNotFound`, not by negotiating it away.

7. **AHP has no security model, and says so.** Connection admission is
   explicitly out of scope. A spec-literal host hands any TCP peer a full
   filesystem API, arbitrary pty creation, session enumeration, and the ability
   to approve any other client's tool call.

8. **The JSON Schemas are a derived, demonstrably buggy artifact.** Upstream
   generates every client *and* the schemas from `types/` via ts-morph, and
   states outright that `types/` is canonical. The 0.6.0 schemas mark
   `T | undefined` fields as `required`; `errors.schema.json` is missing
   `-32011`. Generation from schema is the wrong source. *(measured, E8)*

9. **There is no host-authoring documentation at all.** `docs/guide/hosts.md`
   is a seven-line redirect stub, and `implementations.md` lists exactly one
   server. That is the gap.

**Recommended v0.1:** speak protocol **0.7.0 and 0.6.0** on the wire, with the
reducer port validated against the fixture corpus at `spec/v0.7.0`. Ship root + session
+ chat channels and a stub echo provider. Scope detail in §10.

---

## 1. Q1 — Current spec version, the delta, and which version to target

### Release state, measured

| Artifact | Version | Date |
|---|---|---|
| `types/version/registry.ts` on `main` | `0.8.0` (unreleased) | HEAD |
| newest `spec/v*` tag | `0.7.0` | 2026-07-31 |
| npm `@microsoft/agent-host-protocol` | **`0.6.0`** | 2026-07-20 |
| GitHub release `typescript/v0.7.0` | **does not exist** | — |
| Go module proxy, max version | `0.6.0` | — |
| `@tylerl0706/ahpx@0.5.1` resolves to | `0.5.2` (`^0.5.0`) | — |

The installed 0.6.0 client's `SUPPORTED_PROTOCOL_VERSIONS` is
`['0.6.0','0.5.2','0.5.1']`. It will never negotiate 0.7.0 or 0.8.0.

### VS Code — the client that actually matters

VS Code does **not** depend on the npm package. It vendors upstream's entire
`types/` tree into `src/vs/platform/agentHost/common/state/protocol/`, pinned by
a `.ahp-version` file.

| Fact | Value |
|---|---|
| `.ahp-version` | `8e0a9bbf` → upstream commit `8e0a9bbf497e01d1868a2d4d990ae71c99684a9d` (2026-07-29), contained in `spec/v0.7.0` |
| `PROTOCOL_VERSION` | **`0.7.0`** |
| `SUPPORTED_PROTOCOL_VERSIONS` | **`['0.7.0', '0.6.0', '0.5.2', '0.5.1']`** |
| VS Code version sampled | 1.132.0 (`main`) |

Unlike `MultiHostClient` and `ahpx`, which each offer a **single** version, VS
Code offers the **full list** — so it can negotiate down to any of four
versions.

**Connecting VS Code to a third-party host is a first-class, supported feature**
— no extension required (`common/remoteAgentHostService.ts:92-107`):

| Setting | Purpose |
|---|---|
| `chat.remoteAgentHostsEnabled` | enable remote agent host connections |
| `chat.remoteAgentHosts` | the list of **WebSocket** remote agent host addresses |
| `chat.remoteAgentHostsAutoConnect` | auto-connect configured hosts at startup |

`RemoteAgentHostEntryType` is `websocket | ssh | wsl | tunnel | cloudSandbox`;
the plain WebSocket entry is `{ type: 'websocket', address: string }`. Remote
sessions get the URI scheme `remote-<authority>-<provider>`.

#### The canonical host-side negotiation algorithm

`common/state/protocol/version/negotiation.ts` — written by the reference host
author, and directly portable:

```ts
isCompatibleProtocolVersion(offered, current):
  majors must match
  if major === 0, minors must also match     // every 0.x minor bump is breaking
  offered MUST NOT be greater than current   // a 0.1.0 server can't claim 0.1.5

negotiateProtocolVersion(offered[], current):
  pick the HIGHEST compatible entry          // client preference order ignored
  undefined ⇒ respond -32005
```

**Note the consequence:** this models a host that speaks exactly **one** MINOR.
A host with `current = '0.7.0'` *rejects* an offered `0.6.0`. Supporting several
MINORs at once requires holding a **set** of supported versions rather than a
single `current` — a deliberate extension beyond the reference host. See
§1a.

#### Two VS Code-specific extras

- **`_vscodeUpgrade`.** On `UnsupportedProtocolVersion`, VS Code reads
  `_meta.vscodeUpgradeMethod` off the error's `data` to offer a one-click
  "update server" action. It is for hosts spawned by the VS Code CLI; *"servers
  without a managing CLI omit it"* (`common/state/protocolUpgrade.ts`). We omit
  it. Returning a well-formed `-32005` with `UnsupportedProtocolVersionErrorData`
  still renders a proper incompatibility message.
- **VS Code's own local endpoint is a useful security precedent**
  (`agentHost/LOCAL_ENDPOINT.md`): a WebSocket over a **Unix domain socket or
  named pipe**, never a TCP port, with a random bearer `connectionToken` passed
  as `?tkn=<token>` on the upgrade; wrong or missing token ⇒ **HTTP 403** during
  the upgrade. Metadata lives in
  `<userDataPath>/agent-host/local-endpoint/metadata.json`, user-restricted, and
  is written only after the endpoint is listening. That is a good model for our
  own default posture.

**Caveat.** VS Code has its own client implementation (`agentSubscription.ts`,
`sessionTransport.ts`), so the `MultiHostClient` hard requirements in §2a are
*not* known to apply to it. Its real requirements have not been measured — see
§11.

### 1a. Can a host support multiple protocol versions?

Yes, and for v0.1's scope it is nearly free. Measured from
`registry-snapshot.json` (`actionIntroducedIn`), restricted to root/session/chat:

| Introduced in | Actions |
|---|---|
| ≤ 0.5.1 | 52 |
| 0.5.2 | `session/mcpServerStartRequested`, `session/mcpServerStopRequested` |
| 0.6.0 | `chat/toolCallAuthRequired`, `chat/toolCallAuthResolved` |
| 0.7.0 | `chat/workingDirectorySet`/`Removed`, `session/workingDirectorySet`/`Removed` |

The entire 0.5.1 → 0.7.0 delta for our channels is **8 actions, all additive** —
and **all 8 are outside the v0.1 scope** (MCP, step-up auth, multiroot). Every
version from 0.4.0 up shares the same session/chat channel split, so there is
**one state model** across the whole range.

So the cost of multi-version support is:

1. Hold a **set** of supported versions instead of a single `current`, and pick
   the highest offered member of that set.
2. An **outbound action filter** keyed on the negotiated version, driven by
   `actionIntroducedIn` — data, not a hand-maintained table. This is exactly the
   spec's rule that a host "only sends action types known to the negotiated
   version".
3. Per-version audit of **state shapes and command params**, which
   `actionIntroducedIn` does *not* cover. This is the real cost, and it is why
   the floor should not be pushed below 0.6.0 without a reason: 0.6.0 relocated
   input requests into turn `responseParts`, so 0.5.x is a genuinely different
   state model for elicitation.

**Recommendation: support `{0.7.0, 0.6.0}`.** VS Code negotiates its preferred
0.7.0; the npm client negotiates 0.6.0; both work against one host and one state
model. Add 0.5.2/0.5.1 only if something real needs them — that buys `ahpx`,
which offers a single version and is pinned to a version upstream has already
dropped.

### The compatibility rule that makes this matter

`docs/specification/versioning.md`: pre-1.0, two peers are compatible only when
the **MINOR** matches. So `0.5.2` and `0.6.0` are *not* compatible. Supporting
both means implementing both, not implementing a range.

Negotiation: client sends `protocolVersions[]` most-preferred-first; the host
picks one and returns it as `InitializeResult.protocolVersion`; if it can speak
none, it **MUST** return `UnsupportedProtocolVersion` (`-32005`) and close.
The client does **not** verify the answer (E3), so this is entirely on the host.

### Breaking changes since 0.3.0

| Version | Date | Breaking change that matters to a host |
|---|---|---|
| 0.4.0 | 2026-06-19 | **The session/chat split.** `SessionState.turns`, `activeTurn`, `steeringMessage`, `queuedMessages`, `inputRequests` removed; relocated to `ChatState`. `inputRequests` later deleted outright. |
| 0.5.0 | 2026-06-26 | removals (see CHANGELOG) |
| 0.5.1 | 2026-07-02 | removals |
| 0.5.2 | 2026-07-09 | `InputRequestResponsePart`; `serverInfo`/`clientInfo`; `ToolResultTerminalCompleteContent` |
| 0.6.0 | 2026-07-20 | **step-up auth** (`ToolCallStatus.AuthRequired`, `chat/toolCallAuthRequired`/`Resolved`, `McpAuthRequirement`, `toolAuthentication` input request); input requests moved into turn `responseParts`; `changeset/filesReviewedChanged` → `filesReviewChanged`; `listSessions` pagination |
| 0.7.0 | 2026-07-31 | multiroot sessions; side chats; `ToolResultTerminalCompleteContent` **removed**; schema `required` bug fixed |

Cadence: 0.3.0 → 0.7.0 is five releases in eight weeks, and the changelog states
outright that breaking changes may land in MINOR bumps.

### Recommendation

**Support both `0.7.0` and `0.6.0`, preferring 0.7.0.**

- **0.7.0** is what **VS Code** speaks and prefers, and VS Code is the target
  client. It is also the newest released spec.
- **0.6.0** is what the installable npm client speaks, which keeps the CI
  interop counterparty working.
- The two share one state model, and the entire action delta between them is
  two step-up-auth actions and four multiroot actions — all outside v0.1 scope
  (§1a). Supporting both is close to free.

Do **not** target `0.8.0`: it is unreleased, and nothing speaks it.

Do not add 0.5.x for now. It buys only `ahpx`, which offers a single version,
is pinned to `0.5.0` — a version upstream has already dropped from
`SUPPORTED_PROTOCOL_VERSIONS` — and would drag in a second elicitation state
model.

An earlier draft of this document recommended 0.6.0 alone, on the evidence that
no installable client spoke 0.7.0. That was correct about npm and wrong about
the client that matters: VS Code vendors the types directly and speaks 0.7.0.

---

## 2. Q2 — What the shipped clients do versus what the prose says

### 2a. The client has two layers with very different demands

**Bare `AhpClient` validates essentially nothing.** `connect()` only starts a
receive loop; every byte is application-driven. Measured (E3):

| Probe | `AhpClient` reaction |
|---|---|
| host answers a protocol version never offered | accepted |
| host omits the required `InitializeResult.snapshots` | accepted |
| host returns a malformed `reconnect` result | accepted |
| host emits `serverSeq` `1, 2, 97, 98` (a 94-wide gap) | all delivered, no error |
| host sends an unknown notification method | ignored |
| host addresses `createSession` to the wrong channel | session created and driven to completion |

**`MultiHostClient` / `HostRuntime` — the layer real applications use — has
three sharp hard requirements**, and violating them does not produce an error;
it produces an **infinite reconnect-backoff loop**, because the failure happens
inside `connectOnce`, which `runSupervisor` catches and retries:

| Requirement | Failure mode | Evidence |
|---|---|---|
| `InitializeResult.snapshots` **must be an array** | `initSnapshots` is `undefined`, the guard is `!== null` (undefined passes), `.find()` throws `TypeError` | `hosts/runtime.ts:602,618,682-688`, caught at `:449,462-466` |
| A **successful** `listSessions` must carry iterable `items` (an error *response* is fine) | the `try/catch` wraps only the request; the `for…of summaries.items` sits outside it | `hosts/runtime.ts:627-638` vs `:689-692` |
| the root snapshot's `resource` must be the exact string `'ahp-root://'` | `===`, no normalization, no trailing-slash tolerance | `hosts/types.ts:111`, `runtime.ts:683,821`, `state-mirror.ts:33` |

Two more host-visible behaviours from that layer:

- It offers **exactly one** protocol version — `protocolVersions: [PROTOCOL_VERSION]`,
  never `SUPPORTED_PROTOCOL_VERSIONS`, contradicting its own README
  (`hosts/runtime.ts:596,612`). So the 0.6.0 npm client's runtime offers
  `["0.6.0"]` and nothing else.
- It **re-subscribes only through the handshake** — after connecting it never
  re-issues `subscribe`. The host **must** honour `initialSubscriptions` and
  `ReconnectParams.subscriptions`, or the client is silently deaf on those
  channels (`runtime.ts:594-623`).
- `reconnect` itself is **optional**: any `RpcError` makes the runtime fall back
  to `initialize` with the same subscription list (`runtime.ts:586-607`).
- `clientId` stability across reconnects is a host-visible contract — the host
  keys its replay buffer and active-client identity by it
  (`hosts/client-id-store.ts:1-16`).

**Consequence:** "it works against the reference client" is not evidence of
conformance — and the converse is worse, because the three hard requirements
fail *silently, as a retry loop*, not as a diagnosable error. This project's own
conformance suite is the product.

One more caveat worth designing around: both event surfaces are **lossy**.
`AsyncBroadcastQueue` drops oldest entries and fast-forwards laggard cursors at
4096 events per subscription (`AhpClient`) and 1024 for the cross-host fan-in;
the SDK documents this as an unfixed gap. A host that emits a very high-rate
delta stream can permanently desync a client's mirror through no fault of its
own. Coalescing via `subscribe`'s `delivery.maxLatencyMs` is the intended
mitigation.

### 2b. Prose/type/code divergences found

| # | Divergence | Evidence |
|---|---|---|
| 1 | `AhpStateMirror` does not track chats **at all** — `applySnapshot`/`apply` silently drop every `ahp-chat:` snapshot and action, six weeks after 0.4.0 moved turns into `ChatState`. `chatReducer` is exported but never called by the mirror. | `clients/typescript/src/client/state-mirror.ts:80-133` (HEAD); confirmed at runtime, E3 |
| 2 | `isClientDispatchable`'s signature omits `ChatAction`, though `ClientChatAction` is generated and 15 chat actions are client-dispatchable. The runtime map is complete; the type is stale. | `types/common/reducer-helpers.ts:44` |
| 3 | `errors.schema.json` is missing `-32011 Conflict` — the generator hardcodes the enum. | `schema/errors.schema.json` vs `types/common/errors.ts:88` |
| 4 | `chat-channel.md` says the *server* allocates the chat URI and that `disposeChat` does not exist; the TypeScript says the *client* chooses it (`CreateChatParams.chat`) and `disposeChat` is a registered command. | `docs/specification/chat-channel.md` vs `types/channels-chat/commands.ts`, `CommandMap` |
| 5 | `docs/guide/actions.md:202-210` lists four reducers; seven exist. | `types/reducers.ts:8-15` |
| 6 | `session/defaultChatChanged` is server-only in `IS_CLIENT_DISPATCHABLE` despite prose implying otherwise. | `types/action-origin.generated.ts` |
| 7 | Upstream's own `--branches 100` reducer coverage gate is vacuous — it still globs `types/reducers.ts`, which became a 16-line re-export shim in `ad3f9b96`. | `package.json:26` |

Where prose and TypeScript disagree, **the TypeScript wins** — upstream's
`CONTRIBUTING.md` states `types/` is canonical.

### 2c. Reducers: ported, not generated

Proven three ways:

- Generated files carry a header and a `generated` path segment
  (`clients/go/ahptypes/state.generated.go:1`,
  `.../generated/State.generated.kt`, `.../Generated/State.generated.swift`).
  **The reducers do not**: `clients/go/ahp/reducers.go:1` is bare `package ahp`;
  Kotlin's says *"Hand-written Kotlin port of the per-channel reducers"*;
  Swift's says *"Hand-written Swift port of types/reducers.ts"*; Rust's says
  *"ported from `types/reducers.ts`"*.
- `grep 'reducer' scripts/generate-*.ts` finds only doc-comment mentions — zero
  emission sites.
- Port size is small: **1,683 lines** of TypeScript total (chat 884, session
  397, changeset 120, annotations 113, terminal 91, root 42, resource-watch 36).

**The portability mechanism is the fixture corpus, not codegen.**

### 2d. The conformance corpus

`types/test-cases/reducers/` — 247 fixtures, uniform five-key schema:

```json
{ "description": "...", "reducer": "chat",
  "initial": {...}, "actions": [{...}], "expected": {...} }
```

Distribution: chat 123, session 70, terminal 19, changeset 16, annotations 10,
root 7, resourceWatch 2. **All 85 `ActionType` values have at least one
fixture.** Every official client is gated on it.

`types/test-cases/round-trips/` — 39 fixtures for *serialization* conformance,
with **stricter** null semantics: `null` and absent are distinct
(`KNOWN-FIDELITY-GAPS.md`). Two comparators are needed, clearly named.

Neither corpus is published as a release asset — vendor from a `spec/vX.Y.Z`
tag.

### 2e. Feasibility, proven *(measured, E7)*

Three reducers hand-ported to ~100 lines of Python, run against the unmodified
corpus: **28 pass, 0 fail.**

### 2f. Porting hazards the corpus does *not* pin

These are where a Python port diverges silently. Each needs a hand-written test.

| Hazard | Why | Rule |
|---|---|---|
| **Empty-array truthiness** | `...(tc.content ? {content} : {})` — `[]` is **truthy** in JS, **falsy** in Python. `content`/`options` are arrays. No fixture supplies an empty one. | never `if x:`; always `if x is not None:` |
| **`??` vs `or`** | `??` falls through only on null/undefined; Python `or` also falls through on `''` and `0`. Used pervasively. | a `coalesce(a, b)` helper at every `??` site, greppable |
| **Signed-int32 bitwise** | JS coerces bitwise operands to **signed** int32; Go/Rust/Kotlin/Swift all use **unsigned** 32-bit. For `SessionStatus` with bit 31 set the bits agree but the emitted number's sign does not. No reducer fixture exceeds status `65`. | mask `& 0xFFFFFFFF` (agrees with 4 of 5 clients); file upstream |
| **ISO timestamp format** | `datetime.isoformat()` emits 6-digit microseconds and `+00:00`. The corpus expects `1970-01-01T00:00:09.999Z`. | hand-rolled formatter, 3-digit ms, `Z` |
| **`null` ⇄ absent** | fixture `null` means TS `undefined`/absent. | recursively drop `None` before comparing (the Go/Rust/Swift route) |
| **`True == 1`, `0 == 0.0`** | Python dict equality would pass `{'reviewed': True} == {'reviewed': 1}`. Corpus contains real floats (`0.0`, `0.25`, `1.5`). | type-aware comparator; reject bool/int cross-matches |
| **`Math.max(0, undefined)` → `NaN` → `null`** | TS silently writes `null`; Python raises `TypeError`. | decide and document missing-`duration` behaviour |
| **No `chat` unknown-action fixture** | the other six reducers have one; chat — the largest — does not. | add a Python unit test; contribute the fixture upstream |

### 2g. Absorbing a new upstream release

1. Bump the pin in `UPSTREAM.md`; re-vendor `types/test-cases/**` via
   `git archive spec/vX.Y.Z types/test-cases | tar -x`.
2. Run the corpus. New fixtures fail → port the corresponding reducer branches.
3. Diff `types/channels-*/reducer.ts` between the old and new tag; upstream's
   branch-coverage gate is vacuous, so a new branch can land unfixtured.
4. Re-derive `IS_CLIENT_DISPATCHABLE` and the version registry from
   `registry-snapshot.json`.

---

## 3. Q3 — The real minimum viable channel set

This is the highest-value output. Derived from the type definitions, fixture
`161-chat-turn-lifecycle-on-chat.json`, and the measured wire trace (E2).

### Required state shapes (only the required fields)

```
RootState      { agents: AgentInfo[] }                        # that is all
AgentInfo      { provider, displayName, description, models: SessionModelInfo[] }
SessionState   { provider, title, status, lifecycle, activeClients, chats }
ChatState      { resource, title, status, modifiedAt, turns }
```

`SessionState` carries **no** `resource`, `createdAt` or `modifiedAt` — those
live only on `SessionSummary` in the root catalogue.

### Commands a v0.1 host must answer

| Method | Channel | Result |
|---|---|---|
| `initialize` | `ahp-root://` | `{protocolVersion, serverSeq, snapshots[], serverInfo?}` |
| `ping` | `ahp-root://` | `null` |
| `subscribe` | any channel URI | `{snapshot?}` |
| `unsubscribe` *(notification)* | any | — |
| `listSessions` | `ahp-root://` | `{items: SessionSummary[], nextCursor?}` |
| `createSession` | **`ahp-session:/<uuid>`** (client-chosen) | `null` |
| `dispatchAction` *(notification)* | target channel | echoed `action` envelope |
| `reconnect` | `ahp-root://` | `{type:'replay',actions[],missing[]}` or `{type:'snapshot',snapshots[]}` |

Everything else returns `MethodNotFound` (`-32601`) and is listed as
unimplemented in the README.

### The canonical minimal turn — exactly four actions

Verified byte-for-byte by fixture `161`:

```jsonc
{"type":"chat/turnStarted",  "turnId":"turn-1","startedAt":"…",
                             "message":{"text":"Hello","origin":{"kind":"user"}}}
{"type":"chat/responsePart", "turnId":"turn-1",
                             "part":{"kind":"markdown","id":"md-1","content":""}}
{"type":"chat/delta",        "turnId":"turn-1","partId":"md-1","content":"Hello from chat"}
{"type":"chat/turnComplete", "turnId":"turn-1","duration":8999}
```

Notes that catch people out:

- There is **no `sendMessage` or `createTurn` command.** A turn begins when the
  client dispatches the client-dispatchable `chat/turnStarted` action.
- `chat/toolCallStart` creates its own `toolCall` response part — emitting an
  extra `chat/responsePart` for it duplicates the part.
- Cancellation, error and completion all funnel through one `endTurn` helper
  that force-cancels non-terminal tool calls with reason `skipped`; a host does
  not clean those up itself.

### Session bring-up sequence

`root/sessionAdded` (root channel) → `session/chatAdded` →
`session/defaultChatChanged` → `session/ready` (or `session/creationFailed`).
A host seeds exactly one default chat; there is no chat-enumeration command —
clients discover chats through `SessionState.chats` + `defaultChat`.

### Non-negotiables for `MultiHostClient` (the layer real apps use)

Get these wrong and the client retries forever instead of reporting an error:

1. `InitializeResult.snapshots` is **always an array** — `[]` if there are no
   initial subscriptions, never omitted.
2. A successful `listSessions` **always** carries `items` (`[]` is fine).
   Returning a JSON-RPC *error* is safer than returning `{}`.
3. The root snapshot's `resource` is byte-exactly `"ahp-root://"`.
4. Honour `initialSubscriptions` and `ReconnectParams.subscriptions` — the
   client never re-issues `subscribe` after the handshake.
5. Key the replay buffer and active-client identity by `clientId`, which is
   stable across reconnects.

### Two host obligations that are easy to miss

1. **Stamp `origin` yourself.** `DispatchActionParams` carries only
   `{channel, clientSeq, action}` — no `clientId`. The echoed `ActionEnvelope`
   needs `origin: {clientId, clientSeq}`, so the host must remember each
   connection's `clientId` from `initialize`. Getting this wrong breaks
   optimistic reconciliation for every client except the originator, silently.
   *(measured, E2a)*

2. **Validate client actions and echo rejections.** Both
   `session-channel.md:132` and `chat-channel.md:221` give normative tables:
   invalid actions **MUST** be echoed back with `rejectionReason` on the
   envelope; actions on a non-existent channel **MUST** be silently ignored
   with no echo. `chat/toolCallConfirmed` on a tool call not in
   `pending-confirmation` **MUST** be rejected; `chat/turnCancelled` with no
   active turn **MUST** be rejected.

### The sequencing model (the main phase-2 input)

- **`serverSeq` is a single host-global monotonic counter.** Not per-channel,
  not per-connection. It is stamped on every `ActionEnvelope` on every channel,
  the same value goes to every subscriber, and `reconnect` carries exactly one
  scalar `lastSeenServerSeq` covering *all* subscriptions. A per-channel counter
  design would be unable to answer `reconnect` at all.
- **The replay buffer is therefore also global**, ordered by `serverSeq`, and
  `reconnect` filters it by the caller's subscription set. `missing[]` reports
  subscriptions that cannot be resumed (disposed sessions, revoked access).
- **Protocol notifications are never replayed** (`root/sessionAdded` etc.), and
  stateless channels are not replayed at all — clients re-subscribe and resume
  from the live edge. After a replay the client is expected to re-`listSessions`.
- **The client detects nothing.** No in-tree client buffers, reorders, or
  gap-checks `serverSeq` (measured, E3; confirmed in `state-mirror.ts`, which
  never reads `envelope.serverSeq`). Total ordering is a guarantee the host must
  *structurally enforce* — a single point of assignment, not a lock hoped to be
  held.
- **`ClientDispatch` has no reply.** `dispatchAction` is a notification; the
  only feedback path is the echoed envelope, optionally carrying
  `rejectionReason`. A host that drops a dispatch silently leaves the client's
  optimistic state applied forever.

### The ordering rule

`Snapshot.fromSeq` carries the protocol's only formal ordering guarantee —
*subsequent actions will have `serverSeq > fromSeq`*. No in-tree client buffers
pre-snapshot envelopes. Therefore a host **must** capture snapshot-and-seq
atomically with subscription registration, and must emit the
`subscribe`/`initialize` response before any `action` for that channel. This is
a structural requirement, not a best effort.

### Channel surface, measured, with the v0.1 call

Line counts are of the upstream TypeScript; "types" counts top-level
`export interface|type|enum` in that channel's `state.ts`.

| Channel | actions | state.ts | reducer.ts | types | v0.1 |
|---|---:|---:|---:|---:|:--|
| `ahp-root://` | 4 | 244 | 42 | 9 | **in** |
| `ahp-session:` | 27 | 1363 | 397 | 51 | **in** |
| `ahp-chat:` | 29 | 1571 | 884 | 85 | **in** |
| `ahp-terminal:` | 11 | 172 | 91 | 9 | out |
| `ahp-changeset:` | 8 | 276 | 120 | 8 | out |
| annotations | 5 | 133 | 113 | 4 | out |
| `ahp-resource-watch:` | 1 | 72 | 36 | 3 | out |
| `ahp-otlp:` (stateless) | 0 | 68 | — | 1 | out |
| **totals** | **85** | | **1683** | | |

Root + session + chat is **60 of 85 actions** and **1,323 of 1,683 reducer
lines** — the large majority of the protocol's substance, and the only part a
client needs to render a conversation. The excluded channels are cheap to add
later (the four of them together are 360 reducer lines) but each drags in its
own state vocabulary.

The type surface is where the real cost sits: chat 85 + session 51 = 136
exported types before anything else. This is the main argument for a v0.1 type
layer covering only the fields the required-shape set in this section names,
with `Unknown` arms everywhere else.

### What degrades gracefully

Terminals, changesets, comments/annotations, telemetry, MCP, resource-watch,
side chats, multiroot, risk assessments, `ContentRef` lazy inputs. Multiroot and
side chats are genuinely capability-gated (`AgentCapabilities`); the rest are
simply absent from state, and a client that calls their commands gets
`MethodNotFound`. Because there is no `ServerCapabilities` object (§0.6), that
error *is* the negotiation mechanism.

---

## 4. Q4 — Which client can be driven in CI

### Recommendation, ranked

**1. `@microsoft/agent-host-protocol@0.6.0` + a ~60-line Node driver script.**
This is the answer. It is the upstream-designated tie-breaker for protocol
disputes, it is the version we target, and it is **already proven to work
against a Python host** — E2 drove a full session against a 200-line stub. The
cost is one `devDependency` and a `node` step in CI. For a project whose entire
value proposition is that other implementations can trust it, a real
first-party client in the loop is worth far more than that cost.

Assert on **the raw action stream**, not on `AhpStateMirror` — the mirror
cannot represent a chat at all (§2b#1), so it can verify connection, root and
session state but never conversation state.

**2. `@tylerl0706/ahpx` — not usable for v0.1.** It is otherwise an excellent
CI citizen: `--format text|json|quiet`, `--json-strict`, semantic exit codes
(`0` ok, `1` runtime, `2` usage, `3` timeout, `4` no session, `5` permission
denied, `130` interrupted — `src/errors.ts:15-23`), and non-interactive
`connect`, `agents`, `session new|list|show|close|history`, `exec`.
`--format quiet` prints only the final response text, the cleanest possible
single-string assertion for an echo host.

The blocker is version. `src/client/index.ts:169` sends

```ts
protocolVersions: [PROTOCOL_VERSION],   // a single entry, no fallback list
```

and `PROTOCOL_VERSION` comes from whichever `@microsoft/agent-host-protocol` it
resolved:

| How ahpx is installed | Resolves to | Offers |
|---|---|---|
| `npm ci` in the ahpx repo (lockfile pin, `package-lock.json:681`) | **0.5.0** | `["0.5.0"]` |
| `npx @tylerl0706/ahpx` (range `^0.5.0`) | **0.5.2** | `["0.5.2"]` |

Either way it is a 0.5.x MINOR, and pre-1.0 compatibility is per-MINOR — so a
0.6.0 host is *required* to reject it with `-32005`. Worse, **0.5.0 is no longer
in upstream's own `SUPPORTED_PROTOCOL_VERSIONS`** (`['0.8.0','0.7.0','0.6.0','0.5.2','0.5.1']`),
so the repo build of ahpx speaks a version upstream has already dropped.

Defer it. It could still earn a place as a *negative* test — assert the host
returns a well-formed `-32005` — which is cheap and exercises a path nothing
else does.

**2a. Steal ahpx's test harness shape regardless.** Its real fake-host harness
is `src/__tests__/integration/cli.test.ts`, which spawns the built binary as a
subprocess against `createMockServer()` and asserts exit code, stdout content,
and per-line JSON parseability. Inverting it — swap the mock server for
`subprocess.Popen` of the Python host on an ephemeral port — is a drop-in CI
shape. (`e2e/` is *not* this; it runs offline with no server and gives zero
protocol coverage.)

`src/__tests__/helpers/mock-server.ts` is also the most readable
minimum-host reference in the ecosystem — `initialize`, `createSession`,
`subscribe`, `listSessions`, `fetchTurns`, `reconnect`, `dispatchAction` echo,
and `action` envelopes, in one file. Worth reading before writing ours, with the
caveat that it derives the envelope `channel` from `action.session ?? action.terminal`
(0.5-era); a current host takes `channel` straight from
`dispatchAction.params.channel`.

Practical notes if an ahpx job is ever added: it hard-codes
`~/.ahpx` with no env override, so CI **must** set `HOME=$(mktemp -d)`; and
`AHPX_DEBUG_PROTOCOL=1` dumps the `initialize` params and result to stderr,
which is exactly what you want in a failing job log.

**3. No client ships a conformance suite.** The nearest things are upstream's
own two fixture corpora (§2d) — which are *better*, because they are the same
artifact every official client is gated on. Those are the primary conformance
gate; the live client test is the integration proof on top.

**4. A zero-Node gate, as a complement.** The five `schema/*.schema.json` files
are JSON Schema 2020-12, which Python's `jsonschema` supports natively. Vendor
them at the pinned spec tag and validate every frame the host emits. This runs
on every PR with no Node at all and catches shape drift the moment the pin
moves. It does **not** replace the live-client test — §5 documents that the
published schemas have been wrong — but as an *emit-side* assertion against the
tag we pin, it is cheap and effective.

### The strongest available check

The npm client exports `rootReducer`, `sessionReducer`, `chatReducer` and
`ActionType` from its root entrypoint, and `InMemoryTransport.pair()` from
`/client`. So the CI driver can do more than smoke-test: **feed the host's own
action stream through the official TypeScript reducers and diff the resulting
tree against a fresh `subscribe` snapshot from the host.** That is a direct,
end-to-end assertion that the Python reducers are equivalent to the reference
ones over real traffic — complementing the fixture corpus, which only proves
equivalence over pre-recorded inputs.

### Adopt the inspector's log format

`roblourens/ahp-inspector` reads JSONL where each line is the raw JSON-RPC
message plus an `_ahpLog: {ts: <ISO8601 string>, dir: "c2s" | "s2c"}` sidecar
(`packages/parser/src/wire-meta.ts:3-25`; the parser tolerates the field being
absent or malformed and falls back to ingest time). Emitting it is a three-line
change in a Python frame logger and buys free interop with the debugging GUI
written by the author of the VS Code reference host. **Recommend adopting.**

---

## 5. Q5 — Codegen from the JSON Schemas

**Recommendation: do not generate from the published JSON Schemas.**

Three independent reasons:

1. **They are a derived artifact.** `scripts/generate-json-schema.ts` parses
   `types/` with ts-morph — exactly like `generate-go.ts` and
   `generate-rust.ts`. The schemas are a *sibling output* of the Go and Rust
   clients, not their input. `CONTRIBUTING.md` states it plainly: *"The
   TypeScript types under `types/` are the canonical source of truth; everything
   else is generated from them or hand-maintained against them."*

2. **They are wrong at the version we target.** *(measured, E8a)* At
   `spec/v0.6.0`, `ActionEnvelope.origin` and `Turn.usage`/`ActiveTurn.usage`
   are marked `required` although they are absent on the wire — the exact
   traffic the real client accepted in E2 would be rejected by models generated
   from them. Fixed only at 0.7.0. Separately, `errors.schema.json` is missing
   `-32011`.

3. **`actions.schema.json` is malformed, and has been in every release.**
   `StateAction.oneOf[0]` is the literal `{"$ref": "#/$defs/"}` — an empty
   pointer, a generator artifact of a leading `|` in the TypeScript union
   (`scripts/generate-json-schema.ts`). Verified present at **`spec/v0.5.0`,
   `v0.5.2`, `v0.6.0` and `v0.7.0`**. This is the root cause of the measured
   `RecursionError` in `datamodel-code-generator` 0.71.0 (E8), and it makes
   Python's `jsonschema` raise `PointerToNowhere` on any `StateAction`.

4. **`SessionStatus` is a bitset emitted as a closed enum.** The schema says
   `{"enum": [1,2,8,24,32,64], "type": "number"}` while its own `description`
   reads *"Bitset of summary-level session status flags. Use bitwise checks
   instead of equality."* The status values in upstream's **own golden corpora**
   are `1, 2, 8, 24, 33, 40, 56, 65` (reducer corpus) and `72, 2147483720`
   (round-trip corpus) — **six of those eight, and both round-trip values, are
   not in the enum.** Schema validation would reject upstream's own conformance
   fixtures.

The schemas also cannot express three signals the TypeScript AST carries and
every other generator consumes: `int64` vs float numerics, the "Bitset" marker,
and the unknown-discriminator fallback variant the protocol *requires*. Boolean
literal types (`approved: true | false`) erase to "any". And the entire JSON-RPC
envelope and method-registry layer (`CommandMap`, `ServerNotificationMap`,
`JsonRpcRequest`, `ActionEnvelope`-as-notification-params) is absent from the
schemas entirely.

Structurally they are otherwise fine — JSON Schema 2020-12, `oneOf` + `const`
discriminated unions, no `allOf`/`anyOf`/`if-then-else`/`patternProperties`, and
**no `additionalProperties: false` anywhere**, so unknown fields survive. With
the malformed `$ref` stripped, pydantic v2 / msgspec / dataclass / TypedDict
models all generate. That is not enough to make them the right source.

### What to do instead

Hand-write a thin, hand-maintained Python type layer for the v0.1 surface,
generated *from `types/`* where automation pays. Two viable routes, to be
decided in the plan:

- **(a)** Vendor `registry-snapshot.json` + `types/action-origin.generated.ts`
  as *data* (they are already machine-readable), and hand-write the ~30 state
  and action types v0.1 actually needs.
- **(b)** Contribute a `scripts/generate-python.ts` upstream, alongside the
  five existing generators. Higher effort, but it makes Python a first-class
  citizen of upstream's own release process and eliminates drift permanently.

Either way the type layer must have an **`Unknown(raw: dict)` arm on every
discriminated union** — forward compatibility is fixture-tested
(`002-state-action-unknown-variant-preserved.json`,
`103-delta-skips-parts-without-id.json`), and a strict pydantic union with
`extra='forbid'` and no fallback would fail the corpus and violate the
protocol. Retrofitting that later means rewriting every union.

Also tolerate **unknown enum values**: upstream issue
[#366][i366] (open, flagged a 1.0.0 blocker) records that unknown enum values
hard-fail deserialization in *every* generated client today, contradicting the
additive-change guarantee. Do better than the reference clients here.

[i366]: https://github.com/microsoft/agent-host-protocol/issues/366

---

## 6. Q6 — The agent-side boundary

Upstream's doctrine is explicit (`docs/guide/ahp-and-acp.md`):

> **AHP is a coordination layer. ACP is a communication layer.**
> … the host speaks AHP to its clients and ACP to its agents. This is the
> architecture that the AHP reference implementation targets.

The mental model it offers is **"AHP is a mutex over ACP"** — ACP defines a 1:1
conversation; AHP wraps it so N clients can observe and participate. That yields
three concrete host rules:

- one turn at a time per chat;
- **first `chat/toolCallConfirmed` wins**, subsequent ones rejected — the host
  arbitrates;
- any client may cancel; the host sequences `chat/turnCancelled` and forwards
  cancellation downstream.

The host-internal flow upstream describes is: client dispatches → host
sequences and broadcasts → host translates to `session/prompt` → agent streams
`session/update` → **agent event mapper** converts to AHP actions → host
broadcasts.

**Assessment.** The provider interface should be *ACP-shaped but not ACP-typed*
— the mapping is close enough that an ACP adapter is nearly mechanical, but
binding the core to ACP would violate the doctrine the core exists to respect
and would couple a vendor-neutral library to a second moving spec. Keep the
core independent; make an ACP-backed provider the **first concrete adapter, in
a separate package**.

### The Python ACP landscape, checked 2026-08-01

| Package | Version | Last release | What it is |
|---|---|---|---|
| **`agent-client-protocol`** | **0.11.1** | **2026-07-27** | **first-party** — `agentclientprotocol/python-sdk`, linked from `agentclientprotocol.com/libraries/python`, "mirrors the official ACP schema". 17 releases. Requires Python `>=3.10,<3.15`; sole hard dependency `pydantic>=2.7`. |
| `acp-sdk` | 1.0.3 | 2025-08-21 | **not this protocol** — IBM's *Agent Communication Protocol*. |
| `acp` | 0.0.0 | 2016 | unrelated clipboard tool. |
| `agentclientprotocol` | — | — | available on PyPI |

Two things follow. First, an ACP-backed provider is immediately viable against
a maintained, first-party SDK whose release cadence tracks ACP itself — this is
a real "works with every ACP-compatible agent CLI" payoff for modest effort.
Second, **"ACP" is ambiguous in Python**: `acp-sdk` is a different protocol from
a different vendor. Our adapter package must not be named `acp-*` without
qualification.

Note also that `agent-client-protocol` ships *generated Pydantic models
validated against the canonical ACP schema*. That is exactly the approach §5
rejects for AHP — and the difference is instructive: ACP treats its schema as a
first-class published artifact, whereas AHP's schemas are a derived sibling of
the Go and Rust clients. The tooling choice follows the source of truth, not the
language.

The `pydantic>=2.7` / Python `>=3.10` floor is also a reasonable baseline for
this project, since any host embedding an ACP provider will pull it in anyway.

---

## 7. Q7 — Authentication

**A v0.1 host can conformantly implement zero authentication.** Every discovery
field is optional: a host that declares no `AgentInfo.protectedResources` and
runs no MCP servers never enters the flow. The one extant third-party host has
no `authenticate` case at all and answers `-32601`.

The surface is small: one command (`authenticate`), one ephemeral notification
(`auth/required`), one error code (`AuthRequired` `-32007`, whose `data` **MUST**
be `AuthRequiredErrorData`), one optional root-state field, plus the 0.6.0
step-up branch.

**There is no OAuth machinery in the protocol.** The host never talks to an
authorization server, never redirects, never sees a code. The client does the
entire dance out-of-band and pushes an opaque Bearer string via
`authenticate({channel:'ahp-root://', resource, token, scopes?})`. Discovery
metadata is RFC 9728 shaped and carried over JSON-RPC, not HTTP.

Connection-level access to the endpoint is expressly out of scope —
`docs/specification/transport.md:36`.

**The hard part is 0.6.0 step-up auth**: not one action but a four-envelope
choreography across two channels (chat + session), with several silent reducer
no-op traps. v0.1 should not implement it; it should model
`ToolCallStatus.AuthRequired` in the type layer so the state machine is not
retrofitted later, and document it as unimplemented.

---

## 8. Q8 — Security and multi-tenancy

**AHP has no security model, and the omission is deliberate and documented.**
`SECURITY.md` is unmodified MSRC vulnerability-reporting boilerplate with zero
protocol content. `transport.md:36` places connection admission outside the
wire protocol. Across all four checked-out repositories there are **zero**
occurrences of "threat model", "tenant", "confused deputy", "same-origin",
"CORS", "CSRF", "DNS rebinding", or bind-address guidance.

### What a connection actually grants

A spec-literal host gives any peer that completes `initialize`:

| Surface | Capability |
|---|---|
| `resource*` (9 methods, `ahp-root://`) | full read / write / delete / move / mkdir filesystem API |
| `createTerminal` + `terminal/input` | arbitrary pty with a client-chosen `cwd`, plus keystroke injection — neither capability-gated nor claim-gated |
| `listSessions` | enumeration of every session, with titles and absolute working directories; no filter parameter exists |
| `subscribe` | any channel URI — the only major command with no documented `PermissionDenied` |
| `chat/toolCallConfirmed` | approval of **any other client's** pending tool call: the normative validation table conditions only on tool-call *status*, never on identity |

`clientId` is a client-asserted string validated nowhere, and `reconnect`
carries no credential — it resumes on `clientId` alone. There is no
`ServerCapabilities` object, so a host cannot negotiate these away; it can only
answer `MethodNotFound`.

### Prior art punts

- VS Code agent host: a single connection token, `--tunnel` to expose.
- `wyrd-company/ahp-server`: README states *"Remote and multi-tenant security
  are not implemented"*, and the code matches — no auth,
  `subscribe`/`listSessions`/`disposeSession` unconditional, `reconnect` sets
  `connection.clientId` and `initialized = true` with no prior `initialize`.
  Its one real control is a `realpath`-based root jail on `resource*`.

### The boundary this project draws

The core can guarantee only **protocol** invariants: sequencing, reducer
parity, state-transition validity, action-origin stamping, client-dispatch
gating. Every **trust** decision — who may connect, who may see which channel,
who may approve a tool call, what the process may touch — is a **mandatory,
no-default, embedder-supplied policy object**.

Concretely, for v0.1:

- **No `serve()` one-liner that binds a socket.** Constructing a host requires
  passing a policy; there is no default.
- Default bind is loopback, and binding off-loopback without a policy is a
  hard error, not a warning.
- The `resource*` family and terminals are **not implemented** in v0.1 and
  answer `MethodNotFound` — the two largest holes stay closed by construction.
- The README says, loudly and above the fold, that AHP defines no
  authentication and that this library is single-trust-domain in v0.1.

This is the failure mode that would make the library unsafe to adopt, so it is
written down before any code exists.

---

## 8a. Prior art — `wyrd-company/ahp-server` and `ahp-provider-kit`

The only extant non-VS-Code host. **It is functionally abandoned at protocol
0.3.0**: `@wyrd-company/ahp-provider-kit@0.4.1` — the newest published version —
still pins `@microsoft/agent-host-protocol: ^0.3.0`, and `@wyrd-company/ahp-server`
stopped at `0.3.0` on npm. That is three MINOR versions behind the installable
client and four behind the spec, i.e. pre-chat-split and wire-incompatible with
every current client. The core is small: 1,577 lines across `src/`.

Its `AgentProvider` contract is nonetheless the most useful artifact in the
ecosystem, and worth reproducing:

```ts
interface AgentProvider {
  readonly agent: AgentInfo
  resolveSessionConfig?(p): { schema: SessionConfigSchema; values: Record<string, unknown> }
  createSession(context: AgentSessionContext): AgentSession
}
interface ResumableAgentProvider extends AgentProvider {
  resumeSession(context: ResumableAgentSessionContext): AgentSession   // + state, resumeState?
}
interface AgentSession {
  sendUserMessage(message, sink: AgentTurnSink, signal: AbortSignal, turnId?): Promise<void>
  setActiveClientTools?(tools): void
  getResumeState?(): ProviderResumeState | undefined     // opaque Record<string, unknown>
  cancel?(reason?): void
  dispose?(): void
}
interface AgentTurnSink { emit(action: StateAction): void; fail(error: Error): void }
```

### Adopt / adapt / reject

| Decision | Verdict | Why |
|---|---|---|
| One `sendUserMessage(message, sink, signal, turnId?)` as the whole turn API | **adopt** | Small, complete, and maps cleanly onto ACP `session/prompt`. |
| `AbortSignal` for cancellation + optional `cancel()` | **adopt** (as asyncio cancellation + explicit `cancel()`) | Two-level cancellation is right: cooperative first, hard second. |
| Opaque `ProviderResumeState = Record<str, unknown>`, persisted by the host, interpreted by the provider | **adopt** | Correct division of labour for durable resume across host restarts. |
| Separate `ResumableAgentProvider` rather than optional methods on one interface | **adopt** | Lets the host feature-detect resume without a capability flag. |
| `MarkdownTurnEmitter` — a helper that guarantees `chat/responsePart` precedes any `chat/delta` | **adopt the idea** | Exactly the fixture-161 ordering; the easiest thing for a provider author to get wrong. |
| **Provider emits raw AHP `StateAction`s into the sink** | **reject** | Couples every provider to the AHP action vocabulary and to a specific spec version. Upstream's own doctrine describes an "agent event mapper" *inside the host*. Our sink should take neutral provider events; the host maps them to actions and owns sequencing. This is the single most important departure. |
| `AgentSessionContext.workingDirectory?: URI` (singular) | **reject** | Pre-0.7.0. Must be `workingDirectories: URI[]`. |
| No chat concept in the context at all | **reject** | Pre-0.4.0 split; a provider must be told which chat it is serving. |
| `activeClientId` + `ActiveClientToolSink` — client-contributed tools routed to one "active" client | **adapt, defer** | A real problem (the editor owns some tools), and their answer — route to the single active client — is reasonable. Out of v0.1 scope, but the provider interface should not preclude it. |
| `export type ServerTransport = AhpTransport` — it reuses upstream's **client-side** transport interface verbatim for the server side | **adopt** | The single best structural decision in the repo. A symmetric transport interface means WebSocket / stdio / in-memory all drop in unchanged, and an in-process pair gives free end-to-end tests. |
| `SessionStore` — a 6-method `get/add/remove/update/list` boundary | **adapt** | Right shape, but make it `async`; its synchronous signature is why the filesystem implementation blocks. |
| …but `FileSystemSessionStore.updateSession` **rewrites the entire session JSON on every action**, including every streaming delta | **reject** | O(state) write amplification per token. |
| Active-client tool routing via server-owned trusted correlation | **adopt the principle** | A `dict[(channel, tool_call_id)] -> Future[ToolCallResult]` resolved by the authorised client's completion action, never trusting a client-supplied correlation id. Under the current spec, replace the single `activeClient` with a set keyed by `clientId` and mirror to `session/inputNeededSet`. |
| **Broadcast is fire-and-forget and not serialised per connection** — `void connection.send(...)` in a loop, unawaited | **reject** | `serverSeq` ordering *is* the correctness model. Unordered delivery breaks every client mirror. A Python host must give each connection an `asyncio.Queue` plus a single writer task. |
| **The per-connection read loop awaits each handler inline** | **reject** | Head-of-line blocking: a slow `createSession` (which awaits the agent runtime) blocks a subsequent `chat/toolCallComplete` on the same connection. Read loop enqueues; requests run as separate tasks. |
| It mutates `modifiedAt` on state **outside** the reducer | **reject** | Any host-side mutation that bypasses the reducer guarantees divergence from every client. |
| No action-origin authorization — any client can forge `session/turnComplete` | **reject** | Exactly what `IS_CLIENT_DISPATCHABLE` exists to prevent. |
| Rejected actions are silently dropped instead of echoed with `rejectionReason` | **reject** | Leaves the client's optimistic state applied forever; the spec mandates the echo. |
| No sequence log and no replay: `serverSeq` is a bare in-memory counter that resets to 0 on restart, and `reconnect` unconditionally returns `{type:'snapshot'}` | **adapt** | Snapshot-only reconnect is **spec-legal** — `lifecycle.md` allows it when the gap exceeds the replay buffer — so it is a defensible v0.1 simplification. Resetting the counter to 0 on restart is not: `serverSeq` must be durable or the host must force a snapshot on every reconnect after restart. |
| Its security posture | **reject** | README states remote/multi-tenant security is unimplemented; `reconnect` sets `clientId` and `initialized = true` with no prior `initialize`. Its one real control is a `realpath` root jail on `resource*` — that part is worth keeping. |

---

## 9. Naming and ecosystem position

PyPI availability, checked 2026-08-01 (`404` = available):

| Name | Status |
|---|---|
| `ahp` | **taken** (Analytic Hierarchy Process) |
| `pyahp` | **taken** (Analytic Hierarchy Process) |
| `agent-host-protocol` | available |
| `ahp-host`, `ahp-server`, `ahp-core`, `ahp-types`, `ahp-ws` | available (not taken — see plan.md §2 for why the spelled-out name won) |
| `agent-host-server`, `agent-host-server-acp`, `agent-host-protocol-types` | available — **chosen** |
| `ahp-protocol`, `python-ahp`, `agenthost` | available |

The `ahp` search collision with Analytic Hierarchy Process is real and must be
addressed in the README's first paragraph.

Upstream's package split is `ahp-types` / `ahp` / `ahp-ws` (Rust) and
`ahptypes` / `ahp` / `ahpws` (Go). Mirroring it in Python is available and
would read as familiar to anyone who has used another client.

`docs/guide/implementations.md` lists third-party clients (ahpx) alongside
first-party ones, and its **Servers** section has exactly one entry — the VS
Code agent host. A Python host would be the second server ever listed, and the
path to being listed is an ordinary PR.

---

## 10. Recommended v0.1 scope

**In:**

- Protocol **0.6.0**, negotiated correctly, with `-32005` on no overlap.
- Channels: **root, session, chat.**
- Commands: `initialize`, `ping`, `subscribe`, `unsubscribe`, `listSessions`,
  `createSession`, `dispatchAction`, `reconnect`.
- Host-global `serverSeq`, replay buffer, snapshot-atomic subscribe.
- `origin` stamping; client-dispatch gating from the vendored 85-entry table;
  the two normative action-validation tables with `rejectionReason` echoes.
- Ported `root`, `session`, `chat` reducers, gated on the fixture corpus, plus
  hand-written tests for every hazard in §2f.
- Pluggable agent-provider interface + an **echo provider** with no model, no
  network, no credentials.
- WebSocket transport behind a transport abstraction.
- `_ahpLog` JSONL tracing.
- Mandatory policy object; loopback-only by default.

**Out, and stated as unimplemented in the README:**

terminals · changesets · comments/annotations · telemetry (OTLP) · MCP channel ·
resource-watch · the nine `resource*` methods · side chats · multiroot ·
authentication and step-up auth · `fetchTurns` pagination · completions ·
`resolveSessionConfig` · multi-tenancy.

**Non-goals, permanently:** building an agent; a model provider or tool
registry in the core; any vendor coupling in the core; forking the spec.

---

## 11. Open questions

Recorded with the experiment that would settle each. Several are candidates for
upstream issues — **none have been filed**; filing is a separate decision.

| # | Question | Experiment |
|---|---|---|
| 1 | Is running the canonical reducers *required* of a host, or may a host maintain state by any means as long as its action stream and snapshots are self-consistent? The claim is stated only descriptively, never as a MUST. | Ask upstream. Determines whether reducer parity is a v0.1 blocker or a v0.2 refinement. |
| 2 | With `SessionStatus` bit 31 set, is the normative post-reducer encoding TypeScript's signed value (negative) or the u32 the four runtime clients emit? They provably disagree. | Add a reducer fixture with `initial.status: 2147483720`; run all five harnesses. |
| 3 | Are empty arrays (`content: []`, `options: []`) preserved or dropped across the tool-call auth actions? TS preserves them because `[]` is truthy; a natural Python port drops them. Unfixtured. | Author the fixture, capture TS's `expected`, cross-check the other four. |
| 4 | How must a reducer behave when a `chat/delta` splits a Unicode surrogate pair? TS strings are UTF-16; Python `json.dumps` raises on lone surrogates. No fixture contains a surrogate. | Two-delta fixture with `"\ud83d"` and `"\ude00"`; run all five harnesses. |
| 5 | Will upstream publish `types/test-cases/**` as a release asset? Today only the five schemas + `registry-snapshot.json` ship. | One-line change to `publish-spec.yml`; propose upstream. |
| 6 | Should `AhpStateMirror` route `ahp-chat:` through `chatReducer`? It currently drops chat state entirely. | Reproduced in E3. Report upstream. |
| 7 | Is upstream's vacuous `--branches 100` reducer coverage gate known? | `npx c8 --include 'types/channels-*/reducer.ts' …` and compare. |
| 8 | **`actions.schema.json` has shipped a malformed `{"$ref": "#/$defs/"}` as `StateAction.oneOf[0]` in every tag since `spec/v0.5.0`.** Straightforward bug; a one-line generator fix. | Verified. Report upstream with the reproduction. |
| 9 | **`SessionStatus` is a bitset published as `enum: [1,2,8,24,32,64]`**, which rejects six of the eight status values in upstream's own fixtures. Should it be `integer`? | Verified. Report upstream. |
| 10 | Would upstream accept a `scripts/generate-python.ts` peer of `generate-go.ts`, making Python a first-class generated client? | Ask before building. Would eliminate our drift permanently. |

---

## 12. Sources

| Source | Revision / version | Role |
|---|---|---|
| `microsoft/agent-host-protocol` | `bd27d354` (2026-07-31), tags `spec/v0.7.0`, `typescript/v0.7.0` | **normative** |
| `@microsoft/agent-host-protocol` (npm) | `0.6.0` (2026-07-20) | tie-breaker client; CI counterparty |
| `wyrd-company/ahp-server` + `ahp-provider-kit` | `46f86649` (2026-06-14), pins `^0.3.0` | prior-art host design; **pre-chat-split, do not copy shapes** |
| `TylerLeonhardt/ahpx` | `a178119e` (2026-06-28), npm `0.5.1` → protocol 0.5.2 | second client |
| `roblourens/ahp-inspector` | `25b9ac29` (2026-07-29), v1.5.3 | log format to adopt |
| Agent Client Protocol | agentclientprotocol.com | downstream agent protocol |
