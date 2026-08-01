# Phase 2 — Plan

**Status:** proposed, awaiting review. Derived entirely from
[`research.md`](research.md) and [`experiments.md`](experiments.md); every
"because" below traces to a finding there. Nothing here has been implemented.

---

## 1. Scope of v0.1

The guiding constraint: **the smallest thing a real, installable client can
drive end to end**, chosen from what such a client demands rather than from what
is easy to build.

### Channels

| Channel | In | Why |
|---|:--:|---|
| `ahp-root://` | ✅ | Mandatory. 4 actions, 42 reducer lines. |
| `ahp-session:/<uuid>` | ✅ | 27 actions. Required for any session to exist. |
| `ahp-chat:/<cid>` | ✅ | 29 actions. Where the conversation lives post-0.4.0. |
| terminal · changeset · annotations · resource-watch · otlp | ❌ | 25 actions, 360 reducer lines, five separate state vocabularies. None is needed to render a conversation. |

Root + session + chat is **60 of 85 actions** and **1,323 of 1,683 reducer
lines** — the majority of the protocol's substance.

### Commands

Implemented: `initialize`, `ping`, `subscribe`, `unsubscribe` (notification),
`listSessions`, `createSession`, `dispatchAction` (notification), `reconnect`.

Everything else — the nine `resource*` methods, `createResourceWatch`,
`createTerminal`/`disposeTerminal`, `createChat`/`disposeChat`, `fetchTurns`,
`authenticate`, `resolveSessionConfig`, `sessionConfigCompletions`,
`completions`, `invokeChangesetOperation`, `disposeSession` — returns
`MethodNotFound` (`-32601`) and is listed as unimplemented in the README. No
silent stubs.

### Actions

All 60 root/session/chat actions are **reduced** (the reducer port is
all-or-nothing per channel — partial reducers cannot pass the corpus). The
*provider* surface in v0.1 drives only the minimal turn; the rest are reachable
by a host embedder.

Client-dispatch is gated by the vendored `IS_CLIENT_DISPATCHABLE` table, and
the two normative validation tables (`session-channel.md`,
`chat-channel.md`) are implemented with `rejectionReason` echoes.

### Explicit v0.1 non-goals

Building an agent · any model provider, tool registry or prompt logic in the
core · multi-tenancy or any authentication · vendor coupling in the core ·
forking or privately extending the spec.

---

## 2. Package layout

Upstream splits types / core / transport (`ahp-types`, `ahp`, `ahp-ws` in Rust;
`ahptypes`, `ahp`, `ahpws` in Go). **We mirror the split conceptually but ship
one distribution for v0.1.**

```
ahp-host-py/                      repo
  src/ahp_host/
    types/       wire types, actions, state, errors      (no I/O)
    reducers/    the seven pure reducers + clock         (no I/O)
    conformance/ fixture runners over the vendored corpora
    core/        channels, sequencing, subscriptions, replay, policy
    provider/    the AgentProvider protocol + echo provider
    transport/   the transport protocol + in-memory pair
    ws/          the WebSocket implementation
  vendor/upstream/   pinned fixtures + schemas (committed)
```

**Rationale for one distribution, not three.** The Rust/Go split exists so a
*client* can depend on types without pulling a runtime. Our first consumer is a
host embedder who needs all three. Splitting now would mean versioning three
packages against a spec that breaks every few weeks, for no consumer benefit.
The internal boundaries are enforced by an import-linter rule instead
(`types` and `reducers` may not import `core`, `transport` or anything doing
I/O), so the split remains cheap to perform later if a Python *client* appears.

**Names.** `ahp-host` on PyPI (available; `ahp` and `pyahp` are taken by
Analytic Hierarchy Process packages). Import name `ahp_host`. Adapters get their
own distributions — `ahp-host-acp` first — so the core stays vendor-neutral and
installable with no adapter. Reserve `ahp-types`, `ahp-ws`, `agent-host-protocol`.

---

## 3. The reducer strategy

This is the crux, and the answer is settled by evidence: **hand-port, and gate
on upstream's own corpus.**

Generation is impossible — no upstream generator emits reducers; the Go, Kotlin,
Swift and Rust reducers all carry "hand-written port" headers, and
`grep 'reducer' scripts/generate-*.ts` finds zero emission sites. The corpus is
the portability mechanism, and it is language-neutral.

### The harness

Two comparators, deliberately named differently, because the two corpora have
**opposite** null semantics:

| Corpus | Comparator | Rule |
|---|---|---|
| `reducers/` (247) | `assert_reduced_equal` | recursively drop `None`-valued keys from both sides, then compare (the Go/Rust/Swift route) |
| `round-trips/` (39) | `assert_wire_equal` | `null` and absent are **distinct**; compare `acceptableOutputs[0]` exactly |

The reducer runner is ~60 lines: glob, `json.load`, dispatch on
`fixture["reducer"]`, fold `actions` over `initial`, compare. Plus:

- **Pin the clock to `9999`.** The reducers are not pure — `chatReducer` stamps
  `modifiedAt` from the wall clock in six places. A module-level injectable
  epoch-millis callable, mirroring Go's `nowProvider`.
- **Hand-roll the ISO formatter.** `datetime.isoformat()` emits six-digit
  microseconds and `+00:00`; the corpus expects `1970-01-01T00:00:09.999Z`.
- **A type-aware deep comparator.** Plain `==` would pass
  `{'reviewed': True} == {'reviewed': 1}`. Reject bool/int cross-matches; treat
  `int`/`float` as equal on numeric value only. Never compare via `json.dumps`.
- **Adopt Go's free bonus**: assert `encode(decode(initial)) == initial` for
  every fixture. That validates the whole state type model across 247 real
  payloads and catches silently-dropped fields before any reducer logic runs.
- **Structural validation of the corpus itself** (five keys present, known
  `reducer` value, non-empty `actions`) so a malformed upstream fixture fails
  loudly rather than being skipped.

### Divergence detection beyond the corpus

The corpus is necessary, not sufficient — it pins no JavaScript language
semantics. Every hazard in `research.md` §2f gets a dedicated hand-written test:

empty-array truthiness (`[]` truthy in JS, falsy in Python) · `??` vs `or` ·
signed-int32 vs u32 bitwise on `SessionStatus` · missing `duration` ·
the missing `chat` unknown-action fixture · reducer non-mutation of its input
(identity checks cannot be expressed in JSON fixtures) · surrogate-pair splits
across `chat/delta`.

Additionally, the integration test feeds the host's own live action stream
through the **official TypeScript reducers** and diffs the result against a
fresh `subscribe` snapshot — equivalence over real traffic, not just recorded
inputs.

### Absorbing an upstream release

The procedure lives in [`UPSTREAM.md`](../UPSTREAM.md). The non-obvious step:
**diff `types/channels-*/reducer.ts` by hand**, because upstream's own
`--branches 100` reducer coverage gate has been vacuous since `ad3f9b96` — a new
reducer branch can land upstream with no fixture.

### Types: hand-written, from `types/`, not generated from schema

Justified in `research.md` §5 — the schemas are a derived artifact, were
`required`-wrong at 0.6.0, are missing `-32011`, and crash
`datamodel-code-generator` on `actions.schema.json`.

Two hard constraints on the type layer, both retrofit-hostile:

1. **Every discriminated union needs an `Unknown(raw: dict)` arm** that
   round-trips verbatim. Forward compatibility is fixture-tested; a strict
   union with `extra='forbid'` and no fallback fails the corpus and violates the
   protocol.
2. **Unknown enum values must not raise.** Upstream issue #366 (open, flagged a
   1.0.0 blocker) records that every generated client hard-fails on them today.
   We should not reproduce that bug.

Generated *data* — `IS_CLIENT_DISPATCHABLE` (85 entries) and the action→version
map — is vendored from `registry-snapshot.json` and
`action-origin.generated.ts`, with a CI assertion that our key set matches the
schema's action list.

---

## 4. Versioning

- **Wire version: `0.6.0`.** The only version an installable first-party client
  negotiates. `SUPPORTED_PROTOCOL_VERSIONS = ['0.6.0']` initially.
- Pre-1.0 compatibility is **per-MINOR**, so supporting another version means
  implementing it, not widening a range. Each supported version is a separate
  entry with its own action allow-list, derived from
  `registry-snapshot.json.actionIntroducedIn` — so "only send actions known to
  the negotiated version" is a data-driven check, not a hand-maintained table.
- **Negotiation must be enforced by us.** The client does not verify the
  answer (measured, E3). No overlap ⇒ `UnsupportedProtocolVersion` (`-32005`)
  and close.
- **Our SemVer is independent of the spec's**, exactly as upstream's clients
  are. `ahp-host` `0.x` tracks our own API. Every release states the protocol
  versions it speaks in the changelog and in a `release-metadata.json`, mirroring
  upstream's convention.
- **Support policy:** at most two spec MINORs at once, and the older is dropped
  one release after upstream drops it from `SUPPORTED_PROTOCOL_VERSIONS`.
  (Upstream has already dropped `0.5.0`, which is what `ahpx`'s lockfile pins.)

---

## 5. The agent-provider interface

Shaped by `wyrd-company/ahp-provider-kit` — with its central mistake corrected.

```python
class AgentProvider(Protocol):
    @property
    def agent(self) -> AgentInfo: ...
    async def create_session(self, ctx: AgentSessionContext) -> AgentSession: ...

class ResumableAgentProvider(AgentProvider, Protocol):
    async def resume_session(self, ctx: ResumableAgentSessionContext) -> AgentSession: ...

class AgentSession(Protocol):
    async def send_user_message(self, message: Message, sink: TurnSink) -> None: ...
    async def cancel(self, reason: str | None = None) -> None: ...
    async def get_resume_state(self) -> ProviderResumeState | None: ...
    async def aclose(self) -> None: ...
```

**The one significant departure: `TurnSink` accepts neutral provider events, not
AHP `StateAction`s.** The prior-art kit has providers emit raw AHP actions,
which couples every adapter to the AHP action vocabulary *and* to a spec
version. Upstream's own doctrine describes an "agent event mapper" **inside the
host**. So the sink takes `TextDelta`, `ReasoningDelta`, `ToolCallStarted`,
`ToolCallCompleted`, `TurnFailed`, … and the host maps those to actions, assigns
`serverSeq`, and broadcasts. Providers then survive a spec bump untouched.

Other decisions:

- **Cancellation is two-level** — asyncio cancellation for cooperative unwind,
  plus an explicit `cancel()` the provider can act on. Mirrors `AbortSignal` +
  `cancel()`.
- **Durable resume via an opaque `ProviderResumeState`** (`Mapping[str, Any]`)
  that the host persists and the provider interprets. Correct division of
  labour; adopted unchanged.
- **`ResumableAgentProvider` is a separate protocol**, so the host feature-detects
  resume with `isinstance` rather than a capability flag.
- **A `MarkdownTurn` helper** that guarantees `chat/responsePart` precedes any
  `chat/delta` — the ordering fixture 161 pins and the easiest thing for an
  adapter author to get wrong.
- `AgentSessionContext` carries `working_directories: list[URI]` (plural — the
  singular field was removed in 0.7.0) and the chat URI (absent from the 0.3.x
  design entirely).
- **Tool-call permission** flows host→client as `pending-confirmation` state,
  and the host arbitrates: **first `chat/toolCallConfirmed` wins**, later ones
  are rejected with `rejectionReason`.
- Client-contributed tool routing (an agent invoking a tool only the editor can
  execute) is **out of v0.1**, but the interface does not preclude it: the
  design is a `dict[(channel, tool_call_id)] -> Future[ToolCallResult]` resolved
  only by the authorised client, never trusting a client-supplied correlation id.

---

## 6. Transport

Adopt the prior art's best structural idea: **one symmetric transport protocol,
used by both ends.**

```python
class Transport(Protocol):
    async def send(self, message: Mapping[str, Any]) -> None: ...
    async def recv(self) -> Mapping[str, Any] | None: ...   # None = closed
    async def aclose(self) -> None: ...
```

The core never imports the WebSocket implementation. `ws/` provides it;
`transport/memory.py` provides an in-process pair, which makes the entire
protocol suite runnable with no sockets. Frame logging is a transport decorator
emitting ahp-inspector JSONL: the verbatim message plus
`_ahpLog: {ts, dir: "c2s"|"s2c", connectionId, transport}`. Name the file
`agent-host-*.jsonl` so `npx ahp-inspector` auto-discovers it.

WebSocket specifics: text frames, one JSON-RPC message per frame, no subprotocol
name required (the transport spec is deliberately non-normative and names none).

---

## 7. Concurrency and ordering

Total ordering is a protocol guarantee, so it is **structurally enforced**, not
hoped for. asyncio throughout.

1. **One global sequencer.** `serverSeq` is host-global (not per-channel — the
   single scalar `lastSeenServerSeq` in `reconnect` proves it). Assignment,
   reducer application, replay-log append and fan-out enqueue happen inside
   **one critical section**, in that order. That is the only place `serverSeq`
   is read or written.
2. **Per-connection outbound queue with a single writer task.** Publish is an
   O(1) non-blocking enqueue. The prior-art host's unawaited
   `void connection.send(...)` fan-out can deliver envelopes out of order, which
   breaks every client mirror — this is the direct fix.
3. **The read loop enqueues; it does not await handlers inline.** Requests run
   as separate tasks so a slow `createSession` cannot block a later
   `chat/toolCallComplete` on the same connection. Actions targeting one channel
   are still processed in arrival order.
4. **Snapshot atomicity.** `Snapshot.fromSeq` carries the protocol's only formal
   ordering rule — subsequent actions have `serverSeq > fromSeq` — and no client
   buffers pre-snapshot envelopes. So subscription registration and snapshot
   capture happen inside the same critical section, and the
   `subscribe`/`initialize` response is written to the connection's queue before
   any action for that channel.
5. **Backpressure.** Client event queues are lossy (4096/1024, documented
   upstream as an unfixed gap). Honour `subscribe`'s `delivery.maxLatencyMs` by
   coalescing high-frequency deltas rather than outrunning the client.

---

## 8. State persistence and replay

The sequence log is part of the data model, not an add-on.

```python
class SessionStore(Protocol):     # async, unlike the prior art's sync interface
    async def get(self, uri: URI) -> StoredSession | None: ...
    async def add(self, session: StoredSession) -> None: ...
    async def remove(self, uri: URI) -> None: ...
    async def list(self, *, limit: int | None, cursor: str | None) -> Page[SessionSummary]: ...
    async def append(self, envelope: ActionEnvelope) -> None: ...
```

- **v0.1 ships in-memory** plus a **filesystem** store: an append-only
  JSONL action log per session, with periodic state snapshots. Explicitly
  rejecting the prior art's whole-session JSON rewrite on every action — that is
  O(state) write amplification per streamed token.
- **Replay:** `reconnect` filters the global log by the caller's subscription
  set and returns `{type: 'replay', actions, missing}`. Beyond the retention
  window it returns `{type: 'snapshot', snapshots}`, which is spec-legal.
  `missing[]` reports subscriptions that cannot be resumed.
- **`serverSeq` must be durable**, or the host must force a snapshot reconnect
  after restart. Resetting to 0 silently corrupts every reconnecting client.
- Protocol notifications are **not** replayed, per spec; clients re-`listSessions`.

---

## 9. Security model

The boundary from `research.md` §8, made concrete.

- `Host(...)` **requires** a `Policy`. There is no default and no `serve()`
  convenience that binds a socket.
- `Policy` hooks: `authorize_connection(info)`, `visible_channels(client)`,
  `may_dispatch(client, channel, action)`, `may_create_session(client, params)`.
- A `LoopbackSingleUserPolicy` ships for local use and is named so nobody
  mistakes it for a multi-tenant one.
- Binding off-loopback without an explicit non-loopback policy is a **hard
  error**.
- `IS_CLIENT_DISPATCHABLE` gating is unconditional and independent of `Policy` —
  it is a protocol invariant, not a trust decision.
- v0.1 implements no `resource*` and no terminals, so the two largest holes stay
  closed by construction.

---

## 10. Testing and conformance

| Layer | Gate |
|---|---|
| Reducers | 247 upstream fixtures, unmodified, offline |
| Wire types | 39 round-trip fixtures + `encode(decode(initial))` over all 247 |
| JS-semantics hazards | hand-written tests, one per item in `research.md` §2f |
| Emitted frames | ~~`jsonschema` against the pinned schemas~~ — **not viable**, see below |
| Protocol behaviour | in-memory transport pair; ordering, replay, rejection, negotiation |
| **Interop** | the real `@microsoft/agent-host-protocol@0.6.0` client, driven by a Node script, against the Python host over a real socket |
| Reducer equivalence, live | host action stream → official TypeScript reducers → diff against a host `subscribe` snapshot |

**Why there is no schema-validation gate.** It was in an earlier draft of this
plan and had to be removed: the published schemas cannot validate AHP traffic.
`actions.schema.json` has shipped a malformed `{"$ref": "#/$defs/"}` as
`StateAction.oneOf[0]` in every tag since `spec/v0.5.0`, which makes
`jsonschema` raise `PointerToNowhere`; and `SessionStatus` is a *bitset* emitted
as a closed `enum` that rejects six of the eight status values in upstream's own
fixture corpora. Both are verified in `research.md` §5.

If we want the gate back, it costs a documented vendor-time patch (strip the
empty `$ref`, widen `SessionStatus` to `integer`) — which is worth doing only
alongside an upstream fix, so the patch has an end date. Treated as a follow-up,
not a v0.1 requirement. The round-trip corpus already covers the same ground
honestly.

The whole suite except the interop job runs **offline with no model, no
credentials and no network**. The interop job needs one
`npm i --no-save @microsoft/agent-host-protocol` — the package declares no
runtime dependencies and uses the global `WebSocket`, so it is the cheapest
possible Node footprint.

CI on every push: `ruff`, `mypy --strict`, `pytest`, import-linter, the
conformance gates, and a job asserting the vendored corpus matches the
`UPSTREAM.md` pin.

Errors use the spec's codes (`research.md` E11). No parallel taxonomy.

---

## 11. Build order

1. Types + vendored data tables + the round-trip corpus green.
2. Reducers + the 247-fixture corpus green + the §2f hazard tests.
3. Channel/subscription machinery, global sequencer, snapshot + replay.
4. `initialize` / `ping` / `subscribe` / `unsubscribe` / `listSessions` + root.
5. Session + chat channels; echo provider; the four-action minimal turn.
6. WebSocket transport; then the interop test against the real client.
7. `ahp-host-acp` in a separate distribution, so the core stays neutral.

Each step is a reviewable PR with its gate green before the next starts.

---

## 12. Open decisions for review

1. **Wire version 0.6.0** rather than the newest spec (0.7.0) or `main` (0.8.0).
   Recommended because it is the only version an installable client speaks —
   but it does mean shipping against a spec two MINORs stale from day one.
2. **One distribution** rather than upstream's three-package split.
3. **Neutral provider events** instead of the prior art's raw-`StateAction`
   sink. This is the largest departure from existing practice.
4. **Reduce all 60 actions in v0.1**, even though the echo provider exercises
   ~6 — driven by the corpus being all-or-nothing per reducer. The alternative
   is skipping fixtures, which forfeits the conformance claim.
5. **`ahp-host`** as the PyPI name.
6. Whether to **file the seven open questions** in `research.md` §11 upstream
   now, and whether to offer a `scripts/generate-python.ts` contribution.
