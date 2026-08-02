# Plan — a fully-featured Agent Host Protocol client for Python

**Status:** proposed, awaiting review. Nothing here is implemented; the repository
is empty.

Derived from a research pass over upstream's five reference clients, the AHP
specification at the `spec/v0.7.0` pin, the sibling host
[`agent-host-server-py`](../../agent-host-server-py), and the three real-world
AHP consumers (`ahpx`, `ahp-inspector`, VS Code 1.131). Three competing
architectures were drafted and scored by three adversarial reviewers; this
document is the synthesis, not any one of them. Where a reviewer found a factual
error in a proposal, the corrected fact is what appears below.

---

## 1. Why this exists

Upstream publishes AHP **clients** for Rust, TypeScript, Kotlin, Swift and Go
(`docs/guide/clients.md`). There is no Python client, and there is no Python
entry in the implementations list. Meanwhile the sibling repository is the only
AHP **host** library in any language.

The two halves are complementary and were designed to meet. The sibling's
`docs/plan.md` §2 says so outright — the internal `types` / `reducers` boundary
is enforced by `import-linter` specifically so "the split remains cheap to
perform later if a Python *client* appears." This is that client.

### 1.1 What "fully featured" means here

Not "the TypeScript client, in Python." The reference clients leave real ground
uncovered, and matching them exactly would inherit their gaps:

| Surface | Reference state | Ours |
|---|---|---|
| Reducers in the state mirror | TS mirror covers 4 of 7 and **silently ignores every `ahp-chat:` snapshot** | all 7 |
| Channel→reducer binding | TS routes on URI **scheme**; VS Code session URIs are `<provider>:/<uuid>`, so nothing binds | bound at registration, never inferred |
| Write-ahead reconciliation | specified in `docs/guide/reconciliation.md`; **implemented by no reference client** | implemented |
| Server→client requests | TS ships a typed handler registry with zero implementations; `ahpx` implements 2 of 10, read-only | 10 of 10 |
| `initialize` payload | TS helper cannot send `clientInfo` or `capabilities` at all | both |
| Version negotiation | client accepts a version it never offered (`experiments.md` E3) | verified, opt-out |
| Server notifications | TS handles 5 of 9; the other 4 reach neither subscriptions **nor** `events()` | 9 of 9 |
| Sequence gaps | detected by nobody | detected, reported, never fatal |

Each divergence gets an ADR naming it. Divergence from a reference client is a
decision, not an accident.

### 1.2 Verified protocol surface

Counted directly from the pinned `types/common/messages.ts`, not from prose:

- **27** client→server requests (`CommandMap`)
- **2** client→server notifications (`unsubscribe`, `dispatchAction`)
- **10** server→client requests (`ServerCommandMap` — the symmetrical `resource*` family plus `createResourceWatch`)
- **9** server→client notifications (`ServerNotificationMap`)
- **247** reducer fixtures, **39** round-trip fixtures, **85** action types, **38** client-dispatchable

27 + 2 = 29, which reconciles with the sibling README's "all 29 commands." Every
proposal in the design pass wrote 28 or ~30; only a *generated* parity matrix
would have caught that, which is why §9 makes the matrix generated.

---

## 2. The extraction: one protocol layer, two peers

**Decision: extract `types/`, `reducers/`, `conformance/` (corpora included),
`transport/`, and the pure parts of `core/{versions,errors,channels,seq}.py`
out of `agent-host-server-py` into a third repository
`agent-host-protocol-py`, publishing the distribution `agent-host-protocol`.
Both the host and this client depend on it.**

### 2.1 Why, and why now

The axis that matters: **can the 1,161-line `chat.py` reducer exist exactly
once?** The alternative — a byte-for-byte fork with a drift-detecting CI job —
was the recommended path while the two repos looked like they had to release
independently. They do not: both are under one owner, and the server's first
release can be sequenced behind the extraction. That removes the only serious
objection.

The window is genuinely open and genuinely closing. Verified in the sibling
repo today: `version = "0.0.0"`, `git tag` empty, `dependencies = []`, and an
`import-linter` contract that already machine-proves `types` and `reducers`
import nothing from `core` and that `types` performs no I/O. The boundary being
cut is already enforced. After the first release this becomes a migration for
every consumer instead of one PR.

Options weighed and rejected:

| Option | Verdict |
|---|---|
| Fork + `check_shared_drift.py` | **Rejected.** Drift becomes *detectable*, never impossible, and the failure mode is silent: the 247-fixture comparator normalises `null` away on both sides, so a `js.assign` null-passthrough fix landing in one repo leaves both suites green while they diverge. Six defects of exactly this class already lived inside that blind spot in the sibling. |
| Git submodule / subtree into both repos | **Rejected.** Solves source identity, not *distribution* identity. Two wheels still ship two module objects, two `reducers/clock.py` globals, and `frozen_clock()` in one does not freeze the other — which breaks any application embedding both a host and a client. |
| Client depends on `agent-host-server` | **Rejected.** Inverts the dependency and drags a 3,614-line `core/host.py`, a PTY backend and a filesystem jail along to get `chat_reducer`. |
| Monorepo, two distributions | **Rejected, closest call.** Genuinely simplifies release coordination, but the repos already have separate `AGENTS.md`, `CHANGELOG.md`, ADR series and issue trackers, and it forces host consumers to track client releases. §11.4's cross-repo CI job recovers most of the benefit. |

`agent-host-protocol`, `agent-host-protocol-types`, `agent-host-client` and
`agent-host-server` are all confirmed free on PyPI. (`ahp` and `pyahp` are taken
by Analytic Hierarchy Process packages — hence spelling everything out, per the
sibling's existing rationale.)

### 2.2 How, without destabilising the server

Six ordered steps. 1–3 in the new repo, 4–6 as one reviewable PR in the server.

1. **Split with history.** `git subtree split --prefix=src/agent_host_server`,
   then delete what is not extracted in a *first commit* so
   `git log --follow src/agent_host_protocol/reducers/chat.py` still reaches the
   commits that ported it. That history is the primary evidence for why each
   JS-semantics line reads the way it does; losing it forfeits the reason the
   port is trustworthy.
2. **Rename the import root** `agent_host_server` → `agent_host_protocol`,
   mechanically.
3. **Fix the corpus-in-wheel defect.** `conformance/corpus.py:26` resolves
   `CORPUS_ROOT` as `Path(__file__).resolve().parents[3] / "vendor" / "upstream"`
   while `pyproject.toml` packages only `src/agent_host_server` — an installed
   wheel ships the loader and none of the data. Ship the corpus as package data
   and resolve it through `importlib.resources`. **Note the API consequence the
   design pass got wrong:** `importlib.resources.files()` returns a `Traversable`,
   not a `Path` — no `.glob()`, `.resolve()` or `.parents`. The correct shape is
   `as_file()` inside an `ExitStack`, i.e. a context manager, not a function
   returning `Path`. Budget for that.
4. **Server PR** `feat!: depend on agent-host-protocol`. Delete the extracted
   subpackages, add the dependency, rewrite imports. `core/errors.py`,
   `core/channels.py` and `core/seq.py` are *partially* extracted and get hand
   edits, not `sed`.
5. **Acceptance criterion, stated in the PR body:** the extraction is correct
   iff `pytest` passes with **zero changes to any test assertion** — only import
   lines move. 247 + 39 fixtures, the wire-schema gate,
   `tests/integration/test_vscode_trace.py` and the Node interop job all stay
   green. If an assertion needs touching, the extraction changed behaviour and
   must be reverted. This is the sharpest gate available and it is free.
6. **Layering contract shrinks.** `scripts/vendor_upstream.sh`,
   `generate_tables.py`, `js_semantics_*` and `vendor/upstream/` **move to the
   protocol repo entirely**; `UPSTREAM.md` moves with them and the server's
   becomes a pointer. Pin bumps stop being a server concern — that is the real
   win.

Do **not** add the proposed `forbidden` contract banning
`agent_host_protocol.types._generated` from the server: `import-linter`'s
`ForbiddenContract.allow_indirect_imports` defaults to `False`, and
`types/__init__.py` re-exports seven names from `._generated`, so every
legitimate `from agent_host_protocol.types import ACTION_TYPES` trips it.

### 2.3 Version coupling

The spec breaks in MINOR bumps, so loose pins are a trap.

- `agent-host-protocol`'s SemVer is independent of the spec's (matching the
  sibling's ADR 0002), but its MINOR moves whenever the vendored spec tag's
  MINOR moves.
- Both consumers pin `agent-host-protocol ~= 0.1.0`.
- `UPSTREAM_PROTOCOL_VERSION` is asserted in each consumer's
  `tests/docs/test_readme_is_true.py`, so a README claiming "speaks 0.7.0" fails
  when the dependency moves under it.
- Each consumer gets a **non-blocking** `protocol-main` CI job installing from
  git `@main`, so drift is visible the day it lands.

### 2.4 What the protocol package must fix on the way out

Three things are wrong in the sibling today and should be repaired during
extraction, not inherited:

- **`reducer_name_for(uri)`** (`core/channels.py:75`) is dead code, covers only
  root/session/chat, and routes on the URI scheme — invariant 15's exact failure
  mode. Delete it. Replace it with `reducer_for_state(state)`, a **shape**
  classifier with a pinned precedence order (`"agents"` → root, `"lifecycle"` →
  session, `"turns"` → chat, `"claim"` → terminal, `"files"` → changeset,
  `"annotations"` → annotations, `"root"` → resourceWatch). The ordering is
  load-bearing because `SessionState` also carries `annotations` and
  `changesets` — document that at the definition. A reviewer ran this classifier
  over all 247 fixtures' `initial` states: **zero misclassifications**
  (root 7, session 70, chat 123, terminal 19, changeset 16, resourceWatch 2,
  annotations 10). That is 247 free correctness cases from data neither peer
  authored. Scope the test honestly: fixtures whose `initial` is empty assert
  `None`, so the positive-case count is smaller than 247.
- **`ws/transport.py:38`** does `return await self.receive()` on a
  `JSONDecodeError`. A peer streaming malformed frames exhausts the stack.
  Rewrite as a loop with a malformed-frame counter.
- **`ws/transport.py:33-35`** collapses every `recv()` exception into `None`, so
  a consumer cannot distinguish clean EOF from abnormal close — information the
  hosts supervisor needs to choose shutdown-vs-reconnect. Do **not** widen the
  shared `Transport` contract (the host depends on its current shape). Add an
  optional, duck-typed `DiagnosticTransport` alongside it. Note: a
  `@runtime_checkable` Protocol with non-method members returns `False` from
  `isinstance` when the attributes are absent and **raises `TypeError` on
  `issubclass`** — so the client must branch and synthesise a default, not
  "report by absence."

`without_none()` stays deliberately **shallow**, with the reason documented at
the definition: a nested explicit `null` is load-bearing (a JS unconditional
spread writes `null` through, and `js.assign` removes a key only for
`UNDEFINED`), so a recursive strip silently rewrites the wire.

While in there: the sibling's `AGENTS.md` invariant 4 describes an
`Unknown(raw)` arm on every discriminated union. No such class exists — ADR 0001
superseded it. Fix the text rather than copying a description of code that isn't
there.

---

## 3. Distributions

| Repo | Distribution | Import package | Runtime deps |
|---|---|---|---|
| `agent-host-protocol-py` | `agent-host-protocol` | `agent_host_protocol` | none |
| `agent-host-server-py` | `agent-host-server` | `agent_host_server` | `agent-host-protocol~=0.1.0` |
| `agent-host-client-py` | `agent-host-client` | `agent_host_client` | `agent-host-protocol~=0.1.0` |

This mirrors the shape every other AHP ecosystem converged on — Rust's
`ahp-types`/`ahp`/`ahp-ws`, Go's `ahptypes`/`ahp`/`ahpws`.

**The WebSocket transport.** Upstream splits it into a fourth package; we do
not. The frame codec is common but the server's `serve()`/upgrade path and the
client's `connect()` path share almost nothing, and a fourth distribution to
hold ~80 lines of codec is not worth a release axis. The shared package holds
the `Transport` ABC, `MemoryTransport` and `memory_pair()`; each peer ships its
concrete WebSocket behind its own `[ws]` extra. Revisit if a third transport
(stdio, Unix socket) appears in both.

`requires-python = ">=3.11"`. Core has zero runtime dependencies, for the same
reason the host does: a notebook user parsing a wire log should not pull a
WebSocket stack. `py.typed` and the `Typing :: Typed` classifier ship in **all
three** distributions — without it in `agent-host-protocol`, every re-exported
type is `Any` downstream.

Dev dependencies go in a **`[dependency-groups]` table (PEP 735)**, never
`[project.optional-dependencies]`: the latter is baked into the wheel's
`Requires-Dist`, and PyPI rejects any distribution whose metadata contains a
direct-reference (`@ <url>`) requirement — which a local path to the sibling
repo would be.

---

## 4. Package layout and layering

```
src/agent_host_client/
  client/     errors.py events.py queue.py client.py commands.py mirror.py outbox.py
  serve/      router.py resources.py watch.py tools.py plugins.py inputs.py
  hosts/      types.py policy.py cancel.py client_id_store.py factory.py
              runtime.py handle.py multi.py mirror.py
  api/        client.py session.py chat.py turns.py events.py approvals.py views.py
  sync.py     loop-thread facade
  testing/    fake_host.py scenarios.py            # PUBLIC API
  wirelog/    jsonl.py
  ws/         transport.py
  cli/        commands/*.py render/*.py            # [cli] extra
```

`import-linter` layers, highest first (parenthesised = optional while building
out):

```
(agent_host_client.cli)
(agent_host_client.sync)
(agent_host_client.api)
(agent_host_client.hosts)
(agent_host_client.serve)
agent_host_client.client
(agent_host_client.ws)
(agent_host_client.wirelog)
```

Two forbidden contracts beyond the layers:

- `client` may not import `serve` or `hosts`. `client` *defines* the
  `ServerRequestHandler` protocol; `serve` implements it; `hosts` or the
  application wires them. That inversion is what keeps `serve` above `client`
  while `client` still dispatches into it.
- `testing` may not import `cli` or `ws`, so a downstream test suite stays
  offline.

Conventions mirror the sibling exactly: `mypy --strict` over
`["src","tests","scripts"]`, ruff line-length 100 / py311 /
`E,F,W,I,N,UP,B,A,C4,PT,RET,SIM,TID,RUF` with `N815` ignored (the protocol's
field names are camelCase and mirroring them is correct), conventional commits,
ADRs in `docs/decisions/`, and **"if a feature is not documented, it does not
exist."**

---

## 5. Concurrency model

asyncio, modelled on Swift's actor + `AsyncStream` design — the closest
reference to a single-threaded event loop. Not Rust's mpsc, not Go's goroutine
pair.

- **One reader task, one writer task.** The writer drains an `asyncio.Queue`;
  two concurrent `await transport.send()` calls can interleave frames, which
  Swift discovered the hard way.
- **`notify()`, `dispatch()` and `unsubscribe()` are synchronous** and enqueue
  with `put_nowait`. This is load-bearing and the reason must be in the
  docstring: if `dispatch` were `async def`, two coroutines could interleave
  between `clientSeq` allocation and enqueue, putting `clientSeq` 5 on the wire
  before 4. TypeScript gets this free from single-threaded JS; asyncio does not.
- **Use `asyncio.TaskGroup`** for the reader/writer pair rather than
  `create_task` + a `finally` that cancels. It is the 3.11+ answer: sibling
  cancellation on failure, `ExceptionGroup` aggregation, and no
  "bare `create_task` can be garbage-collected mid-flight" hazard — a comment
  that appears twice in the sibling's `core/host.py`. No proposal evaluated
  this; it should be the default and departures justified.
- **Inbound server requests dispatch on their own task, never inline.** A
  handler may re-enter the client and would deadlock the read loop. All three
  reference clients call this out.
- **Cancellation.** asyncio has no `AbortSignal`, and a naive port leaks
  listeners every reconnect cycle — the exact thing `hosts.test.ts:1023`
  regression-tests. Ship a `ShutdownSignal` (an `asyncio.Event` wrapper) plus a
  `link(*signals)` **context manager**, so the leak is structurally impossible
  rather than convention-dependent. **Do not name it `CancelScope`** —
  `anyio.CancelScope` is a well-known, semantically different object and `anyio`
  is already in the sibling's dev deps. `race_with_cancel()` must await and
  swallow the loser; a bare `asyncio.wait(FIRST_COMPLETED)` leaves "Task
  exception was never retrieved" in every log.
- **Cancellation of the public API must be specified**, which no proposal did.
  When a caller's `await client.request(...)` task is cancelled: the pending
  entry is popped in a `finally`, the request id is burned, and a late response
  for that id is dropped with a diagnostic. The sibling's
  `core/outbound.py::OutboundRequests.call` already contains the worked answer.
- **The reducer clock stays a module global**, reused verbatim. Reduction is
  synchronous CPU work that never awaits mid-reduction, so `frozen_clock` cannot
  interleave. But state the invariant *accurately*: the mirror is mutated from
  at least two tasks (the reader, and any user task calling
  `apply_optimistic()` at dispatch time). It is safe because **no mutation spans
  an `await`** — which is weaker than "one task" and is the rule to document.
  Add a runtime guard in `apply()` asserting the running loop's thread, because
  a downstream `asyncio.to_thread(mirror.apply, env)` breaks it silently.

### 5.1 Reuse the sibling's already-correct plumbing

The largest missed reuse in the design pass: **nobody looked at
`core/outbound.py`, `core/connection.py`, `core/wirelog.py` or `core/auth.py`.**
All three proposals planned to rewrite a pending-request table and a
one-writer outbox from scratch. They already exist, are symmetric by
construction, and are documented with the exact invariants the proposals
restated:

- `OutboundRequests.call(...)` takes a **synchronous** `send` callable
  specifically so a write cannot be awaited from inside a handler — verbatim the
  argument every proposal gave for a sync `notify()`. `resolve()` never raises
  and already handles an `id` of `{}` (legal JSON, unhashable in Python).
- `Connection.enqueue` / `_write_loop` / `drain` is the single-writer outbox.
- `core/wirelog.py` already redacts credential-shaped keys at any depth,
  rebuilding containers so the outbound frame is not mutated, with no opt-out
  ("a flag to log credentials verbatim is a flag someone eventually sets on a
  machine they do not control"). **All three proposals planned a wire log and
  none mentioned redaction — and the client is the party that sends
  `authenticate{token}`.**
- `core/auth.py::BearerToken` has `__slots__` (no `__dict__` for `vars()`), a
  fixed-marker `__repr__` so f-strings and tracebacks leak nothing, and a single
  `reveal()` exit. Every proposal threaded `token: str` into a `?tkn=` query
  string and a dataclass with an auto-generated `repr`, while `websockets` puts
  the URI in connection-error messages. **Use the type, and override `__repr__`
  on any config that holds one.**

These are candidates for the shared package's `_util` or for a small
`agent-host-protocol.rpc` module. They are exactly the code most likely to be
subtly wrong when rewritten.

---

## 6. The three layers

### 6.1 `client/` — `AhpClient`, single-shot

Behavioural parity with the TypeScript client including its quirks, except where
an ADR names a divergence. No reconnect logic at this layer: the transport is
dead, so recovery means a new transport and therefore a new client.

```python
@dataclass(frozen=True)
class ClientConfig:
    request_timeout: float | None = 30.0
    subscription_buffer: int = 0  # 0 = unbounded, per ADR 0003
    event_buffer: int = 4096
    protocol_versions: tuple[str, ...] = DEFAULT_SUPPORTED_VERSIONS
    verify_negotiated_version: bool = True
    wire_log: WireLog | None = None
```

Ported invariants, each with its evidence in the docstring: idempotent
`connect()`; `shutdown()` tears down *before* closing the transport so in-flight
requests raise `ClientClosed` not `TransportError`; structural response demux
(`"id" in m and "result" in m`, …); malformed frame logs and continues while a
transport error is fatal; `subscribe()` attaches the local queue **before**
sending and closes it if the RPC raises; `unsubscribe()` is a no-op after
shutdown while everything else raises; explicit `client_seq` advances the
counter to `max(current, seq + 1)`; an inbound request with no handler answers
`-32601` so the host never leaks a pending request; request ids and `clientSeq`
**never reset across transport swaps** (VS Code's first frame on a fresh socket
carried id 66).

**Omit-if-absent everywhere.** `json.dumps` emits `null` where `JSON.stringify`
drops the key, and `capabilities: {"mcpApps": {}}` vs `null` is load-bearing.

Divergences, each an ADR: nine server notifications fanned out instead of five
(the TS `default:` branch does `void channel; break;` and never calls `fanOut`,
so those four reach neither subscriptions *nor* `events()` — the source comment
claiming otherwise is wrong); `initialize()` sends `client_info` and
`capabilities`; `verify_negotiated_version` defaults on.

`client/commands.py` holds all 27 typed wrappers generated from one table.
Every root-scoped method force-overwrites `channel` to `ahp-root://`;
**`completions` preserves the caller's channel** — the single exception, and the
one every reference client comments on. Get it backwards and @-mention pickers
silently break.

**Version discipline.** Default `protocol_versions` to the *pin's* supported
set, not upstream's. The sibling deliberately separates
`UPSTREAM_SUPPORTED_PROTOCOL_VERSIONS` ("this is NOT what we speak") from
`DEFAULT_SUPPORTED_VERSIONS = ('0.7.0', '0.6.0')`. Every design proposal
collapsed them back into one constant that included an unreleased `0.8.0`.
Offering a version whose action and state tables you have not vendored is
precisely the failure `verify_negotiated_version` exists to catch. **Ship a test
asserting the offered list is a subset of what the pin covers.** (For the
record: there is no `spec/v0.8.0` tag; 0.8.0 is unreleased HEAD, and the
`v0.7.0`→HEAD diff of `types/` is three files — a `ResourceReponsePart`
spelling fix, an `index.ts` re-export refactor, and the registry bump. Action
tables are identical: 85 types, 38 dispatchable.)

`-32005` responses may carry SemVer **range** constraints in
`data.supportedVersions` (`">=0.1.0 <0.3.0"`, `"^0.2.0"`), not just exact
versions. Parse or degrade gracefully; do not equality-match and produce a
confusing failure.

### 6.2 `client/mirror.py` — the state mirror

The TS `AhpStateMirror` is not a model. Port **VS Code's `agentSubscription.ts`**,
the only working implementation.

```python
class ApplyOutcome(Enum):
    APPLIED = "applied"
    REJECTED = "rejected"
    BUFFERED = "buffered"
    STALE = "stale"
    NO_CHANNEL = "noChannel"
```

Rules:

1. **Reducers bind at `register()`**, from the channel kind the caller knows —
   never from a URI scheme. `reducer_for_state()` shape-sniffing is the fallback
   for a snapshot we did not ask for.
2. **Envelopes for an unregistered channel are buffered**, not dropped, and
   replayed on `apply_snapshot` filtered to `serverSeq > fromSeq`.
   `Snapshot.fromSeq` is the protocol's only formal ordering rule.
3. **Snapshots are matched on `Snapshot.resource`, never positionally.**
   `initialSubscriptions` returns snapshots only for state-bearing channels, so
   a stateless channel in that list yields no entry and the arrays are not
   aligned. A positional `zip` is the default mistake and no proposal warned
   against it.
4. **Per-channel `fromSeq` baselines are retained alongside the host-global
   `lastSeenServerSeq`.** A late `subscribe` snapshot otherwise re-applies
   already-counted actions. This is the one place a client can silently
   double-apply.
5. **Reconciliation.** Own echo with `rejectionReason` → drop the pending entry
   **without applying**, emit `ActionRejected` on `diagnostics()`. Own echo
   without → drop pending, apply to confirmed. Foreign or server-originated
   (`origin` absent *or* explicitly `null` — treat identically) → apply to
   confirmed; pending rebases because `optimistic` is recomputed. Match on
   **exact `clientSeq`**, following VS Code (`agentSubscription.ts:321`), not
   Swift's cumulative ack. Reproduce VS Code's second branch too: an own echo
   with **no** matching pending entry and no `rejectionReason` is still applied
   to confirmed — an arm every proposal missed.
6. **Gaps are detected and reported, never raised.** `GapPolicy` ∈
   `IGNORE | WARN | RESEED`, default `WARN`. A client that raises would be
   unusable against real hosts; a client that cannot see a gap is the current
   state of the art.

**A consequence nobody costed:** the chat reducer resets `modifiedAt` from the
clock at turn end (`channels-chat/reducer.ts:133-190`) and recomputes
`ChatState.status` from open input requests while preserving bits 5+. So
optimistic replay stamps a *different* `modifiedAt` than the server's echo,
guaranteeing a visible optimistic-vs-confirmed diff at every turn end. Views
must render `confirmed.modified_at`, and the guide must say why.

**Performance, measured rather than assumed.** One design proposal claimed the
mirror deep-copies chat state per delta. It does not: `reducers/chat.py` uses
shallow structural spreads and never copies `turns` (2,000 deltas ≈ 2.9 ms
total). The real cost is that `part['content'] + delta` is quadratic because the
old string stays referenced by the old part dict, so CPython's in-place `+=`
optimisation never fires. Ship a benchmark and a budget for the streaming path —
no proposal had any performance gate, and this is where a Python client is most
likely to be visibly worse than the TS reference.

### 6.3 `hosts/` — the supervisor

Ported from TypeScript and Rust. **Go is explicitly not a model:** it never
issues `reconnect`, never mirrors root state, inverts `maxAttempts` polarity,
and defaults to 1 s with no jitter.

`connect_once()` in order: link the shutdown and manual-reconnect signals →
transport factory (inside the link, so tokens refresh per attempt) → new client
→ **attach `events()` before the handshake**, or notifications racing the
handshake response are lost to the no-replay queue → `reconnect` iff
`server_seq > 0 and subscriptions`, else `initialize` → fall back to
`initialize` **only on `RpcError`**; transport errors and cancellation propagate
→ `listSessions` best-effort → apply the reconnect result → bump `generation`,
raise `server_seq` monotonically → replay envelopes through the mirror **and**
the fan-out → **only then** transition to `connected`.

Replay arm prunes `missing[]`. Snapshot arm keeps a URI iff
`surviving or not prior` — so URIs added while the request was in flight
survive. Subscriptions are replayed by threading them into the handshake's
`initialSubscriptions`, never by re-issuing `subscribe`.

`PendingPolicy` (ADR) resolves a real disagreement rather than silently picking
a side: the spec says clear `pendingActions` in **both** arms
(`reconciliation.md:81`); VS Code re-sends on replay; Swift re-sends always.
Default `VSCODE`; `SPEC` and `RESEND_ALL` available. Document that the default
is judgement, not evidence, and that a host expecting the spec's behaviour will
see a duplicate `chat/turnStarted`.

`ClientIdStore`: one `<percent-encoded-host-id>.clientid` file per host, raw
UTF-8, atomic temp+rename, `0o600`/`0o700` — the Rust/Swift format, the one two
SDKs agree on. Load does **not** strip whitespace. Resolution: explicit →
`load` → `uuid4()`, and the resolved value is **always** written back.

`MultiHostStateMirror` keys by **`tuple[str, str]`**, not the length-prefixed
`f"{len(host)}\x00{host}{uri}"` string. That encoding exists only because a JS
`Map` cannot take a tuple key. `aggregated_sessions()` parses `modifiedAt` into
a real `datetime` before sorting — TS and Rust compare ISO strings
lexicographically, which is only correct if every host normalises to `Z` with
identical fractional precision.

**Reconnect must re-check authentication.** `auth/required` is ephemeral and
never replayed, so a reconnect silently loses every outstanding challenge. All
three proposals re-issued `listSessions` on reconnect; none re-ran the auth
check, and the spec asks for it (`authentication.md`, Auth Expiry).

---

## 7. The front door

The lower layers are correct; this is what makes them adopted. Designed first,
everything else serves it.

```python
import asyncio
from agent_host_client import connect, Delta, ToolCallReady, TurnCompleted


async def main() -> None:
    async with connect("ws://localhost:4321", token="…") as client:
        async with await client.create_session(provider="echo", cwd=".") as session:
            async for event in session.prompt("Summarise README.md"):
                match event:
                    case Delta(text=t):
                        print(t, end="", flush=True)
                    case ToolCallReady() as call:
                        await call.approve()
                    case TurnCompleted():
                        print()


asyncio.run(main())
```

Or, not caring about streaming:

```python
result = await session.prompt("Summarise README.md", approvals="reads")
print(result.text)
```

`TurnStream` is **both awaitable and async-iterable** — one object, two idioms,
one code path (`__await__` delegates to a `_drain()` that iterates `self`).

**`reconnect=True` is the default**, a deliberate divergence from every
reference SDK. They make the client single-shot and push supervision into a
separate layer. That split is architecturally right and is kept — `Client`
*composes* `HostRuntime`, which composes `AhpClient` — but the default
composition is the supervised one. A user should not have to learn a supervisor
to survive a laptop sleep. `connect(url, reconnect=False)` gives the single-shot
client.

**Events are not wire data.** Wire values stay plain dicts forever (ADR 0001
applies symmetrically to a mirroring peer). Ergonomics come from two derived
things that never mutate or copy the wire:

- **Zero-copy views** — `__slots__` wrappers exposing snake_case properties over
  the underlying dict, with `.raw` always available. Hand-written for the ~15
  shapes people actually read. Types as a *lens*, not a decoder.
- **Frozen `match`-able event dataclasses**, each carrying its `envelope`.
  `UnknownEvent(envelope)` is the forward-compat arm: an action type we do not
  model still reaches the user, and the reducer still applied it.

Actionable events carry their actions: `ToolCallReady.approve()/.deny()`,
`InputRequested.answer()/.decline()/.cancel()`,
`ToolCallResultReview.confirm()/.reject()` — the last of which `ahpx` omits, and
whose absence **hangs any turn using `requiresResultConfirmation` forever**.

`ApprovalPolicy` with `approve_all` / `deny_all` / `ask` / `auto` and string
shorthands. `auto(read_only=True)` really does inspect
`ToolAnnotations.readOnlyHint` with an explicit fallback — `ahpx`'s
`approve-reads` silently degrades to prompting for everything and its own docs
say otherwise. Default is `manual`: the event surfaces, the stream waits, and
after `approval_timeout` it raises `UnansweredToolCall(tool_call_id, tool_name)`.
Silently approving or denying are both worse than a loud, specific error.

`SessionStatusFlags` is an `IntFlag` with `_missing_` returning a pseudo-member,
so a status with bit 31 set (fixture 005 carries `2147483720`) round-trips and
`status & IN_PROGRESS` still works. Bitwise always — `INPUT_NEEDED == 24` shares
a bit with `IN_PROGRESS == 8`.

**`sync.py`** — a thin loop-thread facade with the same signatures. No reference
client has one, and "no reference client has one" is not a reason: TypeScript,
Rust, Go and Swift have no sync facade because their ecosystems have no Jupyter
cell. Notebooks are a real adoption surface. The correct grounds to refuse would
be maintenance cost and a double API surface; the facade is thin enough that
neither bites.

### 7.1 Session bring-up, exactly

Every non-obvious step carries its evidence:

1. `resolveSessionConfig` — best-effort; `-32601`/`-32603` means unimplemented,
   continue.
2. Mint `uri = f"{provider}:/{uuid4()}"` unless supplied. **Not**
   `ahp-session:` — VS Code uses `<provider>:/<uuid>` and nothing may route on
   the scheme.
3. `createSession` with **plural** `workingDirectories` (0.7.0), no `model`
   (per-message since 0.5.0).
4. `subscribe(uri, kind=SESSION)` — binds the reducer explicitly, applies the
   snapshot.
5. If `activeClient` was sent, also dispatch `session/activeClientSet` so our
   own mirror observes it.
6. If `state.lifecycle == "creating"` the session is **provisional** — return
   immediately. Otherwise await `session/ready`/`session/creationFailed`,
   *also* checking the already-applied snapshot. Waiting unconditionally
   deadlocks for 30 s against a provisional host.
7. Resolve `state.defaultChat`; subscribe lazily on first `.chat` access.

---

## 8. The reverse direction — `serve/`

The protocol is symmetric and no reference client finishes this half. It is what
separates "a nice turn API" from a client a host can actually work with.

- **`ResourceRouter`** — longest-prefix mount on `params["uri"]`, `-32601` for
  anything outside `ServerCommandMap`, `-32008` for an unmounted prefix. Empty
  results serialize as `{}`, never `null`.
- **`FileResourceServer(roots, writable=False)`** — `realpath` **then** re-check
  containment under a root, so a symlink swapped between check and open cannot
  escape. `ifMatch` etags → `-32011 Conflict`; `createOnly` → `-32010`;
  `mode` truncate/append/insert with mode-dependent **byte** positions (append
  counts back from EOF; `0` is POSIX append and must be atomic). Writable is a
  second, separate opt-in, mirroring the host's posture.
- **`VirtualResourceServer`** — in-memory, for client-published plugins. This is
  not optional polish: publishing a `virtual://` plugin causes the host to call
  `resourceList`/`resourceRead` straight back at you, and a forward-only client
  silently publishes empty plugins.
- **`ResourceWatchServer`** — the client as a watch *server*. Allocates
  `ahp-resource-watch:/<uuid>`, serves `subscribe` with the frozen state, pushes
  `resourceWatch/changed`. There is no dispose command: release when the last
  subscriber unsubscribes or the connection drops. Polling backend by default;
  `watchdog` is an optional extra, never a core dependency.
- **`ClientToolHost`** — client-owned tools. `attach()` dispatches
  `session/activeClientSet` (a full-entry upsert; there is no tools-only
  action); `detach()` dispatches `session/activeClientRemoved`, **which is
  client-dispatchable** (`action-origin.generated.ts:373` → `true`,
  contradicting the "a client never unsets itself" prose that two of the three
  proposals inherited). Watches for `chat/toolCallStart` with
  `contributor == {"kind": "client", "clientId": ours}`; unknown tool name
  auto-denies. `chat/toolCallContentChanged` **replaces** content, so a
  streaming helper must resend accumulated blocks. A referenced `toolInput`
  (`ContentRef`) is resolved via `resourceRead` and **never cached across
  confirmation**.
- **`InputResponder`** — elicitation and confirmation, answered by dispatching
  the ordinary `chat/*` action to `entry["chat"]` **without subscribing to that
  chat**, which is the whole point of the `SessionState.inputNeeded` aggregate.
  The one exception is `kind == "toolAuthentication"`, resolved by calling
  `authenticate` with `toolCall.auth.resource`. `chat/inputAnswerChanged`
  **merges** per-question answers, so read the current map from the mirror and
  overlay rather than clobbering another client's partial answer.

### 8.1 Auth as a subsystem, not a command

`TokenProvider` installed at `connect()`. Three behaviours, none of which any
reference client has:

- **Upfront**: `authenticate` for every `AgentInfo.protectedResources` at
  connect time, before any session exists — 0.5-era Copilot agents reject every
  turn otherwise.
- **On demand**: `-32007` from *any* command (the spec says it MAY come from
  any) → resolve → `authenticate` → retry **once** → re-raise.
- **Ambient**: `auth/required` notifications handled asynchronously and
  non-fatally, and **re-checked after every reconnect**.

`is_session_gone(err)` unifies `-32001` and `-32008` in one place: the
third-party `ahp-server` defines `NotFound = -32008` and omits `SessionNotFound`
entirely, so both mean "recreate the session" in the wild.

---

## 9. Surfaces the design pass missed

Every item below was found by an adversarial reviewer and is in scope. They are
listed together because they share a cause: they are specified in prose with no
reference implementation to copy.

- **`fetchTurns` returns `{}`.** The turns arrive as a `chat/turnsLoaded` action
  the host MUST dispatch *before* responding
  (`channels-session/commands.ts:137-181`). A client that awaits the result and
  reads it gets nothing; one whose mirror is not wired before the call loses the
  turns entirely. The mirror must **prepend** loaded turns and maintain
  `turnsNextCursor`.
- **Stateless `subscribe`.** `subscribe` returns `{snapshot}` for state channels
  and `{}` for stateless ones (`ahp-otlp:`, `mcp://`). VS Code has a separate
  path and *raises* when a stateful subscribe has no snapshot. Ship
  `subscribe_stateless()`; without the split you cannot subscribe to telemetry
  without tripping your own validation.
- **Telemetry template expansion.** `InitializeResult.telemetry` advertises
  `ahp-otlp://logs{?level}` — an RFC 6570 template the client must expand before
  subscribing, with each expansion its own server-side subscription, and never
  replayed on reconnect. Two proposals fanned out `otlp/export*` without ever
  subscribing, so those handlers could never fire.
- **`root/progress` needs `createSession.progressToken`.** Progress is opt-in;
  without minting the token the notification never arrives and the surface is
  dead. Ship `create_session(progress=True)`.
- **`listSessions` pagination.** Cursors are opaque and server-defined, and
  clients **MUST NOT** parse, modify, or persist them across connections. VS
  Code sends no pagination params at all, so a client that only mirrors VS Code
  silently truncates at the host's default page size.
- **`ChatState.interactivity`** (`'full' | 'read-only' | 'hidden'`,
  `channels-chat/state.ts:236-244`) gates whether a client may send a message
  and whether the chat should be hidden. Undocumented in every guide and spec
  page. All three proposals would happily dispatch `chat/turnStarted` into a
  read-only chat.
- **`AgentCapabilities` gating.** Check `multipleChats{fork?, sideChat?}` before
  passing a `ChatSource`, and `multipleWorkingDirectories{immutablePrimary?}`
  before sending >1 working directory. These are presence-flag objects: `{}`
  means supported, and **`{}` is falsy in Python**, so every test must be
  `is not None`. `multipleChats: {}` advertises multi-chat *without* fork or
  sideChat.
- **`immutablePrimary` is enforced at dispatch, not in the reducer.** Sharing
  the reducers verbatim means inheriting one that will happily remove index 0
  optimistically and then be corrected by a `rejectionReason`. Add the
  MUST-NOT check before dispatching.
- **`_meta` verbatim preservation.** "Clients MUST preserve every property of a
  completion attachment's `_meta` when echoing it back." Plain dicts make this
  correct by default; the place it breaks is a typed view or frozen event that
  reconstructs an attachment field-by-field. Name the invariant and test it.
- **UTF-16 offsets.** `CompletionsParams.offset`,
  `CompletionItem.rangeStart/rangeEnd` and `TextPosition.character` are UTF-16
  code units; Python strings are code points. Ship `types/utf16.py`
  (`length`, `offset_to_index`, `index_to_offset`) **in the plan body, not in a
  risks section**, with an astral-plane test. No corpus fixture covers this, so
  it ships broken otherwise.
- **`disposeChat` exists** in `CommandMap` with `DisposeChatParams`, while
  `chat-channel.md:196` claims the protocol does not expose it. The types win.
- **The `mcp://` side-channel** — zero coverage in any proposal. It speaks
  verbatim MCP minus `initialize`/`initialized`, serves only methods in the
  `AhpMcpUiHostCapabilities` union (`-32601` for anything else), and its
  `McpServerCustomization.channel` MUST be re-read on every
  `session/customizationUpdated` and treated as unavailable while absent.
  Declaring `capabilities: {"mcpApps": {}}` obliges the client to own all `ui/*`
  postMessage traffic. **Explicitly out of 0.1.0, and therefore the `mcpApps`
  capability is never advertised** — a host must not be told we can do something
  we cannot.

---

## 10. Parity matrix

`docs/parity.md` is **generated** by `scripts/generate_parity.py` and checked by
`tests/docs/test_parity_matrix_is_true.py`, which derives every row from the
code — the command table, the notification dispatch map,
`IS_CLIENT_DISPATCHABLE`, the registered reducers, the router's method set —
never from a hand-kept list. The sibling's stated reason applies: a hand-kept
list never contains the flag someone just added. It is also the only mechanism
that would have caught the 27-vs-28 error that every proposal made.

Sections: forward commands (27) · client notifications (2) · server
notifications (9) · reverse commands (10) · channels (8) · client-dispatchable
actions (38). Each with columns for *TS client has it / VS Code sends it / we
have it*.

**Add a second column the generator cannot fake: "proven against a real host."**
Sourced from the interop suite. A derived matrix is self-updating and also
self-congratulatory — a wrapper that exists but has never been exercised shows
green otherwise.

---

## 11. Testing and conformance

| Layer | Gate |
|---|---|
| Reducers | 247 upstream fixtures, unmodified, clock frozen at 9999 |
| Wire types | 39 round-trip fixtures + `encode(decode(x)) == x` over all 247 |
| JS semantics | the 43-case oracle regenerated from the pinned TypeScript, frozen verbatim |
| Shape classifier | `reducer_for_state` over all 247 fixtures' `initial` states |
| Client invariants | `FakeHost` over `memory_pair()` — teardown ordering, error taxonomy, subscribe-before-send, `-32601` for unhandled server requests, malformed-frame survival |
| Reconciliation | Hypothesis properties: for any interleaving of own/foreign/rejected envelopes, `optimistic == replay(confirmed, pending)` and pending never holds an acked `clientSeq` |
| Frame parity | our connect sequence vs the sibling's `tests/integration/fixtures/vscode-1.131-client-requests.json` |
| Adversarial | the six documented corpus blind spots (explicit `null` `turnId`/`_meta`, empty `content`/`options`, unhashable ids, status bit 31, int64, `bool`-is-`int`) |
| Docs | every ```python block in `docs/guide/` executed; README claims derived from code |

**On interop, stated honestly.** Driving the sibling Python host over
`memory_pair()` is cheap, offline and high-coverage — and it is **not
independent evidence**, because both peers share the reducers and were written
from the same reading of the same spec. A wrong-but-symmetric reducer passes
both suites. Say that in the README, in those words, rather than calling it
interop. The genuinely independent counterparty is
`@wyrd-company/ahp-server`, pinned to AHP ^0.3 — which makes it simultaneously
the interop test and the 0.3-shape tolerance test (chat actions on the session
channel, nested `SessionState.summary`, `reconnect` always answering
`{type: "snapshot"}`, `NotFound = -32008`).

**`ahp doctor <url>`** is the piece that converts adoption into conformance
evidence, and it is the only mechanism in the whole design pass that yields
independent data. ~20 assertions against a live host: `InitializeResult.snapshots`
is an array; the root snapshot's `resource` is byte-exactly `ahp-root://`;
`listSessions` items are iterable; the reconnect result matches its declared
shape. It tells a host author in twenty lines what they got wrong.

**`agent_host_client.testing` is public API**, not a test-internal fixture.
`FakeHost` with `.on()`/`.push()/.emit_turn()`, `echo_host()`,
`tool_call_host()`, and a `connected()` pytest fixture. `FakeHost` simulates the
awkward realities: folded first deltas, `serverSeq` gaps, snapshot-only
reconnect, `-32601` for unimplemented methods, `-32008` where `-32001` was
expected. Rust and Go hid their in-memory transports and their users complained;
Swift and TypeScript shipped theirs.

**Wire logs.** ahp-inspector-compatible JSONL — the raw JSON-RPC object plus a
root-level `_ahpLog` sidecar `{ts, dir, connectionId, transport, byteLength}`,
one compact UTF-8 line each, default filename matching the inspector's
`/^(agenthost|agent-host|ahp).*\.jsonl$/i` discovery. Credential redaction is
inherited from the sibling's `core/wirelog.py`, with no opt-out.
**`ahp replay <file.jsonl>` reconstructs state through the *same* `StateMirror`**,
so the debugger and the client provably cannot drift — and the reader stops
being dead weight.

---

## 12. Build order

Each step is a reviewable PR with its gate green before the next starts.

- **M0 — Extraction.** `agent-host-protocol-py` created by `git subtree split`;
  import root renamed; corpus-in-wheel fixed; `reducer_name_for` replaced;
  malformed-frame recursion fixed; `py.typed` added. Server PR depends on it.
  **Gate: the server's existing suite passes with zero test-assertion edits.**
- **M1 — Client scaffold.** pyproject, ruff/mypy/import-linter, CI on
  3.11/3.12/3.13, ADRs, AGENTS.md, CONTRIBUTING.md. Gate: `lint-imports` green
  on an empty tree.
- **M2 — Transport + client core.** `ws/transport.py`, `client/queue.py`,
  `errors.py`, `events.py`, `client.py`, `FakeHost`. Gate: the §6.1 invariant
  suite; `docs/guide/connecting.md` executes.
- **M3 — Commands + parity matrix.** All 27 wrappers, root-scoping table,
  the `completions` exception, the pin-vs-offered-versions test. Gate: matrix
  generated and asserted against the vendored `CommandMap`.
- **M4 — State mirror.** Binding, buffering, `fromSeq` baselines,
  reconciliation, `GapPolicy`, views. Gate: Hypothesis properties; chat state
  matches the host's after a scripted 200-action turn; streaming benchmark
  within budget.
- **M5 — The front door.** `api/`, `sync.py`, `testing/` as public API. Gate:
  the ten-line README example runs as a test.
- **M6 — Hosts layer.** `cancel`/`ShutdownSignal` **first** — everything depends
  on it — then policy, client-id stores, runtime, handle, multi. Gate: kill the
  host mid-turn, the turn resumes; the linked-signal leak regression test.
- **M7 — Reverse direction.** `serve/*`, auth subsystem. Gate: `FakeHost`
  issues all ten reverse methods; a client-owned tool executes end to end; a
  `virtual://` plugin is published and fetched back; symlink-escape and etag
  tests pass.
- **M8 — Interop, doctor, replay.** Sibling host over WS; `ahp-server` marked
  and green; frame parity vs the VS Code fixture. Gate: `ahp doctor` finds real
  deviations in a real host.
- **M9 — Release.** Guide pages executing, parity matrix complete, CHANGELOG,
  `0.1.0`.

**Non-negotiable ordering:** M0 before everything (the corpus is the only
correctness evidence that exists). `ShutdownSignal` before any of M6. The mirror
(M4) before the front door (M5), because folded-first-delta recovery reads
authoritative text out of the mirror and cannot be written without it. The
parity matrix (M3) before M7, so reverse-direction rows have somewhere to land
instead of being retrofitted.

M0–M5 is the shippable core. M6–M7 are what "fully featured" means. M8–M9 are
what adoption means.

---

## 13. ADRs

Written before the code they justify. The wire-representation decision moved
upstream into the shared package during M0 (it is that package's ADR 0001), and
the extraction ADR lives there too, so this repository's series is seven rather
than the nine originally scoped:

| ADR | Decision |
|---|---|
| [0001](decisions/0001-depend-on-the-shared-protocol-layer.md) | Depend on `agent-host-protocol`; never fork it |
| [0002](decisions/0002-lossless-per-channel-delivery.md) | Per-channel queues are lossless; only fan-in taps drop |
| [0003](decisions/0003-supervised-by-default.md) | `connect()` is supervised by default |
| [0004](decisions/0004-write-ahead-reconciliation.md) | Implement write-ahead reconciliation, following VS Code |
| [0005](decisions/0005-verify-versions-observe-gaps.md) | Verify the negotiated version; observe gaps, never raise |
| [0006](decisions/0006-reverse-direction-and-pending-policy.md) | The reverse direction is first-class; reconnect `PendingPolicy` defaults to `VSCODE` |
| [0007](decisions/0007-out-of-scope-for-0-1-0.md) | `mcpApps` and `ahp-otlp:` are out of 0.1.0, and the capability is not advertised |

---

## 14. Open risks

1. **Reconciliation has no testable counterparty.** The spec describes it, no
   reference client implements it, and VS Code's `agentSubscription.ts` — the
   only working reference — cannot be run against. Hypothesis properties test
   self-consistency, not conformance.
2. **`PendingPolicy`'s default is judgement, not evidence.** The spec, VS Code
   and Swift disagree. A host expecting the spec's "clear in both arms" sees a
   duplicate `chat/turnStarted` from our replay-arm re-send.
3. **Lossless per-URI queues trade a bounded failure for an unbounded one.** A
   consumer that stops draining a chat subscription mid-turn grows the queue
   without limit. Swift accepted this; we inherit it. The only backpressure path
   is `delivery.maxLatencyMs`, which is advisory and which VS Code never sets.
4. **No independent 0.7 host exists.** `ahp-server` is genuinely third-party but
   pinned to ^0.3. For the 0.7 surface we have zero independent conformance
   evidence, and the README must not imply otherwise.
5. **Protocol version skew is the dominant field hazard.** `strict_version`
   protects the handshake, not the action shapes. A version-keyed normalisation
   seam belongs in the mirror's bind step — even as a no-op today — or it gets
   retrofitted through every reducer call site.
6. **The reverse-direction surface is designed entirely from types.**
   `resourceWrite`'s mode/position byte semantics, `resourceRequest`'s
   permission negotiation and the watch release rule are prose with no
   implementation to check against. Expect these to be wrong in ways only a real
   host reveals.
7. **Three coupled distributions under a spec that breaks in MINOR bumps.** The
   `~=0.1.0` pin and the non-blocking `protocol-main` job are the mitigation;
   the fallback if coordination proves painful is a monorepo, and switching
   later is more expensive than choosing it now.
8. **Hand-written views grow with the spec.** Every upstream state-shape change
   is a manual edit, and mypy cannot tell us a view property no longer matches
   the wire. Generating views from the vendored `.ts` state files is a real
   project and is not planned.
