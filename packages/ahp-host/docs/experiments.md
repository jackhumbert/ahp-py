# Experiment log

First-hand experiments run during phase-1 research. Everything here was executed
against real artifacts on **2026-08-01**; nothing in this file is inferred from
prose. Each entry names the command, the observed output, and what it settles.

Upstream checkout under test: `microsoft/agent-host-protocol`
@ `bd27d354b39c1b2090fbcc6db392d406b743280c` (2026-07-31).

---

## E1 — Which protocol version does a real, installable client negotiate?

```bash
npm view @microsoft/agent-host-protocol versions --json
```

> `["0.2.0","0.3.0","0.4.0","0.5.0","0.5.1","0.5.2","0.6.0"]`, `dist-tags.latest = 0.6.0`

The installed package's `SUPPORTED_PROTOCOL_VERSIONS`
(`dist/types/version/registry.js:27`):

```js
export const SUPPORTED_PROTOCOL_VERSIONS = Object.freeze(['0.6.0', '0.5.2', '0.5.1']);
```

Meanwhile the git repo is well ahead:

| Source | Version |
|---|---|
| `types/version/registry.ts` on `main` | `0.8.0` (unreleased), supports `0.8.0 → 0.5.1` |
| newest `spec/v*` tag | `spec/v0.7.0` (2026-07-31) |
| newest `typescript/v*` **GitHub release** | **does not exist** — `GET /releases/tags/typescript/v0.7.0` → `Not Found` |
| npm `@microsoft/agent-host-protocol` | **0.6.0** (2026-07-20) |
| Go module proxy `.../clients/go/@v/list` | max `v0.6.0` |
| `@tylerl0706/ahpx` 0.5.1 dependency | `@microsoft/agent-host-protocol: ^0.5.0` → resolves to **0.5.2** |

**Settles:** the spec is at 0.7.0 and `main` at 0.8.0, but *no client you can
install speaks either*. The interoperable ceiling in the wild is **0.6.0**, and
the one shipping third-party CLI client is on **0.5.2**. Per
`docs/specification/versioning.md`, pre-1.0 compatibility is per-MINOR, so
0.5.2 and 0.6.0 are **not** mutually compatible — a host that wants both must
implement both.

---

## E2 — Can a Python host actually drive the real Microsoft client?

Built a ~200-line Python WebSocket stub host (`websockets` 17.0.1) and drove it
with the published `@microsoft/agent-host-protocol@0.6.0` client
(`AhpClient` + `WebSocketTransport` + `ws`).

Result: **full end-to-end success.** Negotiated `0.6.0`; subscribed to root,
session and chat; dispatched a client action; received the echoed turn.

Exact client→host wire sequence, captured verbatim:

```jsonc
{"id":1,"method":"initialize",   "params":{"channel":"ahp-root://","clientId":"probe-client-1",
                                           "protocolVersions":["0.6.0","0.5.2","0.5.1"],
                                           "initialSubscriptions":["ahp-root://"]}}
{"id":2,"method":"listSessions", "params":{"channel":"ahp-root://"}}
{"id":3,"method":"createSession","params":{"channel":"ahp-root://","sessionId":"…","chatId":"…",
                                           "provider":"echo","title":"Probe session"}}
{"id":4,"method":"subscribe",    "params":{"channel":"ahp-session:/<uuid>"}}
{"id":5,"method":"subscribe",    "params":{"channel":"ahp-chat:/<cid>"}}
{      "method":"dispatchAction","params":{"channel":"ahp-chat:/<cid>","clientSeq":1,"action":{…}}}
{"id":6,"method":"ping",         "params":{"channel":"ahp-root://"}}
```

**Settles:** the `channel` routing-key invariant holds on the wire for *every*
message, including `initialize`/`listSessions`/`ping` using the literal
`'ahp-root://'`. A minimal interoperable host is genuinely small.

### E2a — `dispatchAction` carries no `clientId`

```ts
// dist/types/common/commands.d.ts:430
export interface DispatchActionParams { channel: URI; clientSeq: number; action: StateAction }
```

but the echo the host must broadcast is

```ts
// dist/types/common/actions.d.ts
export interface ActionEnvelope {
  channel: URI; action: StateAction; serverSeq: number;
  origin: ActionOrigin | undefined;   // ActionOrigin = { clientId, clientSeq }
}
```

**Settles:** the host must remember each connection's `clientId` from
`initialize` and stamp it into `origin` itself. A host that simply reflects
`params` back silently breaks optimistic reconciliation for every client except
the originator. This is a required host behaviour that no client will diagnose
for you.

---

## E3 — What does the real client *enforce*? (adversarial hosts)

> **Scope caveat, added after the source survey.** These probes drive the bare
> `AhpClient`, which is application-driven and validates almost nothing. The
> `MultiHostClient` / `HostRuntime` layer that real applications use *does* have
> hard requirements — an array-valued `InitializeResult.snapshots`, an iterable
> `items` on a successful `listSessions`, and an exactly-`'ahp-root://'` root
> snapshot resource — and violating them yields an infinite reconnect-backoff
> loop rather than an error. See `research.md` §2a. The conclusion below is
> unchanged and in fact strengthened: host correctness is not observable by
> testing against the client.

Four deliberately misbehaving stub hosts, one per port.

| Probe | Host behaviour | Client reaction |
|---|---|---|
| Version mismatch | answered `protocolVersion: "9.9.9"`, never offered | **accepted silently** |
| Missing `snapshots` | omitted the required `InitializeResult.snapshots` | **accepted silently** |
| Bogus `reconnect` result | `{replayed:true,…}` instead of the specified `{type:"replay"…}` | **accepted silently** |
| `serverSeq` gap | delivered seqs `1, 2, 97, 98` | **all four delivered, no error** |
| Unknown notification method | sent `totally/unknown` | **ignored, no error** |

The E2 probe also violated five concrete type contracts without complaint:

| Sent | Actual type | Reference |
|---|---|---|
| `createSession` on `channel: "ahp-root://"` with a `sessionId` field | `CreateSessionParams.channel` **is** the client-chosen `ahp-session:/<uuid>` URI; there is no `sessionId` field | `types/channels-session/commands.ts:64` |
| `RootState.activeSessions: string[]` | `activeSessions?: number` (a count) | `types/channels-root/state.ts:37` |
| `ListSessionsResult.sessions` | `items: SessionSummary[]` + `nextCursor` | `types/channels-root/commands.ts:61` |
| `root/sessionAdded params.session` | `summary: SessionSummary` | `types/channels-root/notifications.ts:41` |
| `ChatState {sessionId, turns, activeTurn, queuedMessages, inputRequests}` | required set is `{resource, title, status, modifiedAt, turns}` | `types/channels-chat/state.ts:37-56` |

That `createSession` deviation is the sharpest illustration: the probe addressed
the wrong channel entirely — the routing key the whole protocol is built on —
and the reference client still drove a session to completion against it.

**Settles:** `AhpClient` performs essentially **no** validation of host
responses. Conformance cannot be discovered by testing against the reference
client — it must be self-enforced by the host's own test suite. This is the
strongest argument for this project shipping its own conformance suite rather
than relying on "it works with the client."

It also means version negotiation correctness is **entirely** the host's
obligation (`versioning.md`: the host MUST return `UnsupportedProtocolVersion`
`-32005` when no offered version is acceptable).

---

## E4 — Reducer forward-compatibility, measured

Called the published reducers with unknown action types:

```
Unhandled action type: {"type":"session/totallyMadeUp","foo":1}
UNKNOWN-ACTION sessionReducer -> identity: true
Unhandled action type: {"type":"chat/nope"}
UNKNOWN-ACTION chatReducer   -> identity: true
```

**Settles:** the required behaviour on an unknown action is *return the input
state unchanged* (same object identity) and log a warning — `softAssertNever`
in `types/common/reducer-helpers.ts`. Never throw. The fixture corpus tests
this explicitly (`session/nonExistentAction`, `root/nonExistentAction`,
`annotations/unknownActionType`, `changeset/nonExistentAction`,
`resourceWatch/unknownAction`).

Caveat, from upstream [issue #366][i366] (open, 2026-07-29, labelled a 1.0.0
blocker): unknown **enum values** on directly-typed fields (e.g.
`AuthRequiredReason`, `TurnState`, `ChangesetStatus`, `MessageOrigin.kind`) do
*not* degrade — they hard-fail deserialization in every generated client. Only
unknown *action types*, unknown extra fields and unknown *object-union variants*
degrade gracefully. A Python implementation should tolerate unknown enum values
from the start.

[i366]: https://github.com/microsoft/agent-host-protocol/issues/366

---

## E5 — The reducers are **not pure**

```bash
grep -rn "Date.now()\|new Date(\|Math.random" types/channels-*/reducer.ts
```

> six hits, all in `types/channels-chat/reducer.ts`
> (lines 218, 253, 359, 723, 779, 812), each `modifiedAt: new Date(Date.now()).toISOString()`

The protocol requires reducers to "run identically on host and client", yet
`chatReducer` reads the wall clock. Upstream's answer is an **injected clock**
in every port plus a **frozen clock in the conformance harness**:

- `types/reducers.test.ts:114` — `const MOCK_NOW = 9999; Date.now = () => MOCK_NOW`
- `clients/go/ahp/reducers.go:33` — `nowProvider func() int64`, overridable
- `clients/go/ahp/reducers_fixture_test.go:97` — `const mockNowMillis int64 = 9999`
- Kotlin `FixtureDrivenReducerTest.kt:339` and Swift `FixtureDrivenReducerTests.swift:26` — same `9999`

Fixtures therefore expect the sentinel `"1970-01-01T00:00:09.999Z"`.

**Settles:** a Python port must expose an injectable millisecond clock on the
reducer module and freeze it to `9999` in the conformance test. It also means
host and client genuinely diverge on `modifiedAt` for the same action in
production — a real protocol wart worth raising upstream.

---

## E6 — The shared conformance corpus is language-neutral and directly usable

`types/test-cases/reducers/` — **247 fixtures**, ~1.0 MB. Schema is uniform
across all 247 files (verified by key census):

```json
{ "description": "…", "reducer": "session", "initial": {…}, "actions": [{…}], "expected": {…} }
```

Coverage: chat 123, session 70, terminal 19, changeset 16, annotations 10,
root 7, resourceWatch 2 — **91 distinct action types**.

`types/test-cases/round-trips/` — 40 fixtures for *serialization* conformance:

```json
{ "name":"…","group":"A","description":"…","type":"StateAction",
  "input": {"type":"future/newAction","foo":42},
  "acceptableOutputs": [{"type":"future/newAction","foo":42}] }
```

The Go, Kotlin and Swift clients all consume the reducer corpus unmodified via
`FixtureDrivenReducerTest` / `reducers_fixture_test.go`. Both harnesses
normalise `null` ⇄ absent before comparing (Go `stripNulls`, TS
`nullToUndefined`).

**Not** published as release assets — GitHub release `spec/v0.7.0` ships only
the five `*.schema.json` files, `ahp-schemas-0.7.0.zip` and
`registry-snapshot.json`. The fixtures must be vendored from a pinned git tag.

---

## E7 — Feasibility spike: three reducers ported to Python

Hand-ported `rootReducer`, `terminalReducer` and `resourceWatchReducer`
(169 lines of TypeScript) to ~100 lines of Python, then ran them against the
**unmodified** upstream fixture corpus with a `strip_nulls` normaliser:

```
ported reducers: ['resourceWatch', 'root', 'terminal']
PASS 28   FAIL 0   SKIP(other reducers) 219
```

Whole reducer surface, for scale:

| reducer | TS lines |
|---|---|
| chat | 884 |
| session | 397 |
| changeset | 120 |
| annotations | 113 |
| terminal | 91 |
| root | 42 |
| resource-watch | 36 |
| **total** | **1683** |

**Settles:** hand-porting the reducers and proving equivalence against
upstream's own corpus is not just viable, it is the *same* mechanism every
non-TypeScript client already uses. 1,683 lines is a tractable port.

---

## E8 — Codegen from the published JSON Schemas

Schema structure (all five files, JSON Schema 2020-12, `$defs`-only bundles
with no root schema):

| file | defs | `oneOf` | `const` | `additionalProperties:false` | `$ref` |
|---|---|---|---|---|---|
| `actions` | 271 | 29 | 164 | **0** | 539 |
| `commands` | 340 | 30 | 170 | **0** | 586 |
| `errors` | 345 | 30 | 170 | **0** | 591 |
| `notifications` | 188 | 26 | 76 | **0** | 376 |
| `state` | 178 | 25 | 76 | **0** | 350 |

No `additionalProperties: false` anywhere — good, unknown fields survive.

`datamodel-code-generator` 0.71.0 → pydantic v2:

| schema | result |
|---|---|
| `state` | OK — 3,034 lines |
| `commands` | OK — 4,816 lines |
| `notifications` | OK — 3,193 lines |
| `errors` | OK — 4,859 lines |
| **`actions`** | **`RecursionError: maximum recursion depth exceeded`** |

Reproducible across `pydantic_v2.BaseModel` and `typing.TypedDict` outputs and
with `sys.setrecursionlimit(20000)`. Not caused by the single self-recursive
definition (`ConfigPropertySchema`) — stubbing it out does not fix it.

**Root cause, located and verified.** `StateAction.oneOf[0]` is the literal
`{"$ref": "#/$defs/"}` — an empty JSON pointer, a generator artifact of a
leading `|` in the TypeScript union. It has shipped in every released tag:

```
spec/v0.5.0    oneOf= 74 malformed=1 first={'$ref': '#/$defs/'}
spec/v0.5.2    oneOf= 80 malformed=1 first={'$ref': '#/$defs/'}
spec/v0.6.0    oneOf= 82 malformed=1 first={'$ref': '#/$defs/'}
spec/v0.7.0    oneOf= 86 malformed=1 first={'$ref': '#/$defs/'}
```

The same defect makes Python's `jsonschema` raise `PointerToNowhere` on any
attempt to validate a `StateAction`.

### E8c — `SessionStatus` is a bitset published as a closed enum

```json
{"enum": [1, 2, 8, 24, 32, 64], "type": "number",
 "description": "Bitset of summary-level session status flags.
                 Use bitwise checks instead of equality…"}
```

The schema contradicts its own description. Status values in upstream's **own**
golden corpora:

| corpus | values |
|---|---|
| `test-cases/reducers/` | `1, 2, 8, 24, 33, 40, 56, 65` |
| `test-cases/round-trips/` | `72`, `2147483720` |

Six of those eight, and both round-trip values, are absent from the enum.

**Settles:** the published schemas cannot validate AHP traffic — they reject the
project's own conformance fixtures. Together with E8a and E8b this closes the
codegen question: `types/` is the only usable source of truth.

### E8a — The published schemas for 0.6.0 are *wrong* in a way that matters

The 0.7.0 changelog claims a fix for `T | undefined` properties wrongly marked
`required`. Verified both sides:

```
# HEAD (0.7.0+)
ActionEnvelope  required=['channel','action','serverSeq']            origin  required? False
Turn            required=['id','message','responseParts','state']    usage   required? False

# spec/v0.6.0 — the version the installable client speaks
ActionEnvelope  required=['channel','action','serverSeq','origin']   origin  required? True
Turn            required=['id','message','responseParts','usage','state']  usage required? True
```

**Settles:** generating Python types from the *0.6.0* published schemas would
produce models that reject valid traffic — including the traffic the real
client accepted in E2, where host-originated envelopes carry no `origin`.

### E8b — The schemas are a derived artifact, not the source of truth

Every upstream generator — `scripts/generate-go.ts`, `generate-rust.ts`,
`generate-kotlin.ts`, `generate-swift.ts`, and `generate-json-schema.ts`
itself — parses the TypeScript in `types/` with **ts-morph**. The JSON Schemas
are a *sibling output* of the same pipeline that produces the Go and Rust
clients, not their input.

**Settles:** generating Python from JSON Schema means generating from a lossy,
demonstrably buggy downstream product. The correct source of truth is `types/`.

---

## E9 — Client-dispatch validation is a host obligation

`types/common/reducer-helpers.ts`:

> Servers SHOULD call this to validate incoming `dispatchAction` requests and
> reject any action the client is not allowed to originate.

`IS_CLIENT_DISPATCHABLE` in `types/action-origin.generated.ts` is a generated
map over every `StateAction['type']`. **38 of 85 actions** are dispatchable:
chat 15, session 11, annotations 5, terminal 5, changeset 1, root 1,
resource-watch 0.

There are **40** `@clientDispatchable` JSDoc annotations across
`types/channels-*/actions.ts`, but only 38 map entries are `true` — upstream's
`scripts/generate-action-origin.ts` counts an annotation only when the following
declaration carries a `type: ActionType.X` member, so two annotations on
non-enum-bearing declarations are skipped. **The generated map is
authoritative** (it is what `isClientDispatchable` reads) and is what we vendor.
An earlier draft of this document reported the JSDoc count of 40.

Two shipped-code observations:

1. The `isClientDispatchable` helper's *signature* omits `ChatAction`
   (`RootAction | SessionAction | TerminalAction | ChangesetAction | AnnotationsAction`)
   even though `ClientChatAction` is generated and 15 chat actions are
   client-dispatchable. The runtime map is complete; the type is stale.
2. `AhpStateMirror` (`clients/typescript/src/client/state-mirror.ts`, HEAD)
   tracks root, sessions, terminals and changesets — **but not chats**.
   `applySnapshot` and `apply` both fall through and silently drop every
   `ahp-chat:` snapshot and action, six weeks after the 0.4.0 split moved turns
   into `ChatState`. Confirmed at runtime in E3. `chatReducer` is exported but
   the mirror never calls it.

**Settles:** a host must enforce client-dispatchability itself, and it can do so
from vendored generated data rather than hand-maintained lists.

---

## E10 — `registry-snapshot.json` is a machine-readable version oracle

Release asset on every `spec/v*` tag:

```json
{ "specVersion": "0.7.0",
  "supportedProtocolVersions": ["0.7.0","0.6.0","0.5.2","0.5.1"],
  "actionIntroducedIn":      { "root/agentsChanged": "0.1.0", … },   // 85 entries
  "notificationIntroducedIn":{ "root/sessionAdded": "0.1.0", … },    // 8 entries
  "generatedAt": "2026-07-31T22:11:49.586Z",
  "commit": "ea6fae670c4012721fdc02d587b3a46ecdc871c0" }
```

**Settles:** the versioning rule "the host only sends action types known to the
negotiated version" can be enforced from data instead of hand-maintained
tables, and the file gives an exact upstream commit to pin in `UPSTREAM.md`.

---

## E11 — Error codes (for reference; do not invent parallel ones)

`types/common/errors.ts`. Standard JSON-RPC: `-32700` ParseError, `-32600`
InvalidRequest, `-32601` MethodNotFound, `-32602` InvalidParams, `-32603`
InternalError. AHP application codes:

| code | name | notes |
|---|---|---|
| `-32001` | `SessionNotFound` | |
| `-32002` | `ProviderNotFound` | |
| `-32003` | `SessionAlreadyExists` | |
| `-32004` | `TurnInProgress` | |
| `-32005` | `UnsupportedProtocolVersion` | `data` MAY be `UnsupportedProtocolVersionErrorData` |
| `-32006` | `ContentNotFound` | |
| `-32007` | `AuthRequired` | `data` **MUST** be `AuthRequiredErrorData` |
| `-32008` | `NotFound` | |
| `-32009` | `PermissionDenied` | `data` MAY advertise an unlocking `resourceRequest` |
| `-32010` | `AlreadyExists` | |
| `-32011` | `Conflict` | optimistic-concurrency precondition failure |

There is no "not implemented" AHP code — an unimplemented command returns
JSON-RPC `MethodNotFound` (`-32601`).

---

## E12 — A real VS Code 1.131 connected to this host

The decisive experiment, run 2026-08-01 against `python -m agent_host_server`
over WebSocket on loopback with a connection token. VS Code Stable **1.131.0**,
macOS. Full frame capture via `--wire-log` (275 frames).

**Result: it works.** Handshake, session creation, chat subscription, and a full
turn — the user typed `hello` and got `You said: hello` back from the echo
provider.

It also broke three things no other test caught, because all three are places
VS Code does something the spec's *examples* do not.

### E12a — VS Code opens with `reconnect`, not `initialize`

First frame on a fresh socket:

```jsonc
{"id":66,"method":"reconnect","params":{
  "clientId":"66a61b49-…","lastSeenServerSeq":38,
  "subscriptions":["ahp-root://","echo:/88c9e07f-…", …]}}
```

Our host answered `-32602 "initialize must be the first request"`. **VS Code did
not fall back to `initialize`** — it retried the same request on an exponential
backoff, forever, so the connection never established. (The npm client's
`MultiHostClient` *does* fall back on an `RpcError`; VS Code's own client does
not.)

`reconnect` re-establishes a connection that dropped, so by construction there
is no prior `initialize` on *that* transport. A host must accept it as a first
request. It carries no `protocolVersions` and no credential — it resumes on a
client-asserted `clientId` alone.

### E12b — Session URIs are `<provider>:/<uuid>`, chats can be base64

From the same subscription list:

| URI | What it is |
|---|---|
| `echo:/af2d658f-61c0-4279-a549-ffb2d89517f2` | a **session** — provider scheme, not `ahp-session:` |
| `echo:/af2d658f-…/annotations` | an annotations channel derived from it |
| `ahp-chat://default/ZWNobzovYzA4NzNiZGEt…` | a **chat** — authority `default`, path is base64 |

That base64 decodes to `echo:/c0873bda-45aa-4eb0-9e36-6d5ce861fee7` — the
session URI. So a chat URI can be `ahp-chat://<chatId>/<base64 session uri>`.

**Consequence:** a host that routes reducers on the URI scheme applies **no
reducer at all** to a VS Code session. Its state freezes at the snapshot while
it keeps broadcasting actions, and every client diverges immediately with
nothing to notice. The reducer must be bound when the channel is created, never
inferred from the URI. Fixed; regression in
`tests/integration/test_vscode_trace.py`.

### E12c — VS Code offers exactly one protocol version

```jsonc
{"id":1,"method":"initialize","params":{
  "channel":"ahp-root://","protocolVersions":["0.7.0"],
  "clientId":"0a78fdaa-…","clientInfo":{"name":"vscode…"}, …}}
```

**This corrects §1 of `research.md`.** That section reported VS Code offering the
full `['0.7.0','0.6.0','0.5.2','0.5.1']` list, which is what
`SUPPORTED_PROTOCOL_VERSIONS` declares in its vendored source — but the runtime
sends `[PROTOCOL_VERSION]` alone, exactly as the npm `MultiHostClient` does.

So supporting 0.7.0 was not a preference, it was **required**: a host speaking
only 0.6.0 would have been refused outright. The decision in ADR 0002 was right
for a reason weaker than the one now available.

### E12d — What VS Code probes, and tolerates

40 refused calls in one session, all `-32601`, none fatal:

| Method | Times |
|---|---|
| `createResourceWatch` | 17 |
| `resourceRead`, `resourceResolve` | 10 each |
| `resolveSessionConfig`, `resourceList`, `createTerminal`, `disposeTerminal` | 1 each |

Only `createTerminal` surfaced to the user, as a
*"The terminal process failed to launch: Method not found: createTerminal"*
toast. Everything else was absorbed silently. This is the designed behaviour —
AHP has no server capability object, so `MethodNotFound` **is** how a host
declines — and it confirms a v0.1 host can decline broadly and still render.

### E12e — `createSession` contributes client tools

```jsonc
{"channel":"echo:/af2d658f-…","provider":"echo",
 "workingDirectories":["file:///"],
 "config":{"autoApprove":"default","mode":"interactive"},
 "activeClient":{"clientId":"0a78fdaa-…","tools":[{"name":"usages", …}, …]}}
```

`workingDirectories` is plural — the 0.7.0 shape. And `activeClient.tools` is
the active-client tool routing from `research.md` §8a: VS Code offers *its own*
tools for the agent to invoke. v0.1 does not implement that, but must not choke
on being handed them.

### E12f — The settings that connect it

```json
{
  "chat.remoteAgentHostsEnabled": true,
  "chat.remoteAgentHosts": [
    {"address": "127.0.0.1:4321", "name": "Echo", "connectionToken": "…"}
  ]
}
```

`chat.remoteAgentHosts` holds `IRawRemoteAgentHostEntry` objects — `address` and
`name` are both **required strings**, and an entry failing that guard is
silently dropped. The address is stored scheme-less (the transport prepends
`ws://`; only `wss://` is preserved), and `connectionToken` is separate: VS Code
appends it as `?tkn=`, which is `connectionTokenQueryName` in
`vs/base/common/network.ts`.

There is no UI for this in 1.131.0 — `parseRemoteAgentHostInput` and
`addRemoteAgentHost` exist and accept a pasted `ws://host:port/?tkn=…` URL, but
only tests call them.

---

## E13 — Differential testing beyond the corpus

Two independent harnesses ran our reducers against the real TypeScript ones
under `node --experimental-transform-types`, with `Date.now()` frozen to 9999:
~1.2M single-step cases across both reducers, plus 705 hand-built session cases
crossing every action with absent / null / wrong-type / valid payloads over
three state shapes.

They found **six defects the 247-fixture corpus cannot reach**, because every
fixture in it is well-formed input from a cooperating peer:

| Defect | Reachable by |
|---|---|
| `chat/truncated` with `turnId: null` destroyed the whole transcript | any client — the action is client-dispatchable |
| tool-call `status` membership raised `TypeError` on an unhashable value | a client — `chat/toolCallComplete` spreads its `result` onto the call |
| ids used as set/dict keys raised on unhashable values | a client, via `chat/pendingMessageSet` |
| `session.py` raised `KeyError` on nine client-dispatchable actions | any client omitting an optional field |
| `_meta: null` / `editedToolInput: null` ignored instead of clearing | any client |
| `response is None` vs `=== undefined` left input requests open | any client — **and this host produced the offending state itself** |

The recurring root cause is one line of translation: JavaScript distinguishes
`undefined` from `null`, Python does not. `x === undefined` must be ported as
`"x" not in obj`, never as `x is None`. Where the reference uses `??` or
truthiness, `is None` *is* right — so the mistranslation is invisible unless you
check each site against the source.

**The blind spot, quantified.** Across all 247 fixtures there is no explicit
`null` for `turnId`, `_meta`, `editedToolInput` or `response`; no empty
`content` or `options` array; no unhashable id; and no `status` with bit 31 set.

**What this means for the conformance claim.** Passing the corpus proves a port
*agrees with the reference on well-formed input*. It does not prove the port
survives a peer that is hostile, buggy, or merely written in a language whose
serializer emits `null` where TypeScript omits a key. Both properties matter for
a host, and only the first is testable against upstream's artifacts.
