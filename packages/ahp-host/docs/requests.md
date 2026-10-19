# Requests from an embedder

Written from the outside in: what an application embedding this host runs into
when it deploys the library **behind an identity-aware reverse proxy, serving
several users**, rather than on loopback for one.

That posture is the one the README declines to support today, deliberately and
correctly — "a Python library that *looks* safe to expose would be worse than
one that says it is not." None of these requests asks the library to change that
stance. Every one of them is about the library being *usable* by an embedder
that supplies the trust decisions itself, which is exactly the split
[`core/policy.py`](../src/ahp_host/core/policy.py) already declares.

Ordered by whether it blocks. Items 1–3 are general to any multi-user
deployment; 4 and 5 are smaller.

---

## 1. The handshake's headers and token never reach `Policy` — blocking

**`ConnectionInfo` already has the right shape and nothing fills two of its
fields.**

```python
# core/policy.py:39-42
client_id: str
peer: str | None = None
token: str | None = None
headers: Mapping[str, str] | None = None
```

- `Connection.token` is initialised to `None` at `core/connection.py:44` and
  **never assigned anywhere in the tree**.
- `Connection.info` (`core/connection.py:52`) constructs `ConnectionInfo` with
  `client_id`, `peer` and `token` — **`headers` is never passed at all**.
- `Host.serve(transport, *, peer=None)` (`core/host.py:314`) accepts nothing
  else to pass on.
- The WebSocket server *does* see the whole request. `_process_request`
  (`ws/server.py:93`) receives it, validates `?tkn=` against the connection
  token, and keeps only `last_handshake_path` (`:103`). `_handler` (`:87`) then
  forwards the peer address and nothing more.

So a peer's credential is checked for equality at the door and discarded, and
any other evidence about who is connecting is dropped before `Policy` is asked.

**Why this blocks.** The standard way to authenticate a user in front of a
WebSocket service is a proxy that terminates TLS, authenticates the person, and
forwards the resulting principal as a request header on the upgrade. That is
the deployment shape for essentially every host that is not loopback. Today an
embedder cannot see that header from any policy hook — the information exists at
the handshake and is destroyed one call before the only place it is useful.

`Policy.authorize_connection(info)` is precisely the right hook and it is
currently given almost nothing to decide with. `reconnect` sharpens it: it
"carries no credential: it resumes on a client-asserted `clientId` alone, which
is exactly why admission is the Policy's decision and not this method's"
(`core/host.py`). Correct — but the Policy needs the material to make it.

**Proposed shape.** Nothing that changes the trust model:

- `Host.serve(transport, *, peer=None, headers=None, token=None)`, stored on the
  `Connection` and surfaced through `Connection.info`.
- `WebSocketServer` passes the upgrade request's headers, and the `tkn` it
  already parsed, into that call.
- Header names stay opaque to the library — it is a mapping, not a policy. Which
  header carries a principal, and whether to trust it, is the embedder's
  decision, and a library that guessed would be wrong for everyone.

A note on trust worth putting in the docstring: a forwarded header is only
evidence if the socket can be reached exclusively through the proxy. That is the
embedder's problem, and saying so is better than a hook that quietly implies
otherwise.

---

## 2. No worked example of a partitioning `Policy`

`LoopbackSingleUserPolicy` returns `True` from every hook. It is the right
default for what it is named after, and it is also the only implementation in
the tree, so an embedder partitioning sessions between users starts from a blank
file with six hooks and no map of which paths consult which.

Two sharp edges are documented in the `may_see_channel` docstring and are easy
to miss until something leaks:

> `subscribe` is the only major command the spec gives no `PermissionDenied`
> path for, and `listSessions` has no filter parameter — so if sessions must be
> partitioned, it happens here.

That is a real design constraint, and it means partitioning is not "add an
ownership check to `may_create_session`" — it is spread across `may_see_channel`
(catalogue visibility and channel access) and `may_dispatch` (who may act on a
session they can see).

**Request:** a reference `OwnedSessionPolicy` — sessions carry an owner, the
owner is derived from `ConnectionInfo` by an embedder-supplied callable, and the
hooks are wired consistently — shipped **with a negative-test suite**. The tests
matter more than the class. Every deployment writes the same four tests (peer B
cannot see A's session; cannot subscribe to its channels; cannot dispatch into
it; cannot resume A's connection by asserting A's `clientId`), and they are the
tests that fail loudly when a future change routes around a hook.

Not a core concept — an example plus a harness. The core staying agnostic is the
right call.

---

## 3. No structured record of what happened

`--wire-log` is the only account the host gives of itself, and it is explicitly
"a transcript, not a trace": every frame of every session, useful for debugging
a handshake and unusable as an audit record. Redaction and `0600` fixed its
worst property but not its shape.

An embedder in a regulated or reviewed environment needs the opposite: a small
number of **structured events**, retainable, with no conversation content —
connection admitted or refused, session created and by whom, a tool call
confirmed and by which principal, an action rejected and why. Every one of these
is already a decision point inside the host; several are not visible to an
adapter at all, because they happen at connection level before any provider is
involved.

Upstream's related item is OTLP telemetry, which is aimed the other way — at the
client, whose consumer discards traces and metrics — and is last in the roadmap
by consumer demand. This is a different requirement that happens to share a word.

**Proposed shape:** an optional observer protocol on `Host`, called with typed
events, defaulting to nothing. Fire-and-forget, exceptions logged and never
propagated (the same rule the sequencer's subscription hooks already follow), so
an embedder's audit sink cannot take the host down. Content stays out of it by
construction: identifiers and decisions, never message text.

---

## 4. Multi-agent on one host is unclaimed

`RootState.agents` is plural in the protocol. `Host` holds a single
`self.provider` and publishes `"agents": [self.provider.agent.to_wire()]`
(`core/host.py:306`). The roadmap lists this under "other unowned surface" and
it appears in no release.

An embedder whose runtime has several distinct agents — different roles,
different system prompts, different tool sets — currently runs one host process
per agent. That works, and it may well be the right answer, but it is a decision
the library is making implicitly rather than one an embedder gets to make.

**Request:** not necessarily an implementation — a paragraph in the docs saying
which it is. If one-host-one-agent is deliberate, say so and the workaround
becomes an architecture. If multi-provider is intended eventually, knowing that
now changes how an adapter is structured today.

---

## 5. No liveness or metrics surface

Nothing exposes whether the host is healthy. A deployed service is usually
supervised, restarted on failure, gated on a health check before traffic is
sent to it, and scraped for metrics; this one can be observed only by whether
its port accepts a connection.

Genuinely arguable as the embedder's job, and easy enough to build outside the
library. Filed because "the port is open" is a weak liveness signal for a
process whose interesting failure modes — a wedged sequencer, a provider that
stopped answering, suspended requests nobody will resolve — all keep the port
open.

**Smallest useful version:** a documented way to ask the `Host` for a few
counters (connections, sessions, pending requests, current `serverSeq`) without
reaching into private attributes. Whatever exposes them is the embedder's.

---

## Deliberately not requested

- **Durable pending requests.** ADR 0005 states the registry is in-memory and a
  request that outlives a crash is dead, with the turn reported failed so the
  transcript stays honest. That is a decision, not an omission, and the
  operational answer (restarts are announced) is adequate.
- **Model selection or routing.** "The host is a courier: it never
  authenticates, and it never selects a model" is the correct line. Routing
  belongs in the layer that owns the endpoints.
- **Agent-to-agent coordination.** An explicit anti-goal, and rightly — that
  belongs to whatever orchestrates the agents, not to the protocol between a
  session and its clients.
- **Anything that would make the library claim to be safe to expose.** The value
  of the current stance is that it is honest. Every request above is for
  material the embedder needs to make its *own* decision, not for the library to
  make one on its behalf.

---

# Round two — after the first five landed

All five above shipped, and the same embedder then tried to actually wire the
result: a multi-user deployment, sessions partitioned by principal, surviving a
service restart. Two things stopped that at the time of writing, and both were
small. The third and fourth were smaller still.

**All four landed**, as Tier 0 of
[`release-plan.md`](release-plan.md): 6 became
`Policy.channel_created`/`channel_dropped` — the alternative the request
offered — proven over the wire by two principals in
`tests/integration/test_ownership_over_the_wire.py`; 7 became
`StoredSession.metadata`, round-tripped verbatim; 8 is
[`guide/deploying.md`](guide/deploying.md) §"Liveness and readiness", executed
as a test; and 9 was decided and bounded — 2048 frames by default,
`Host(outbox_limit=…)`, overflow disconnects the peer rather than dropping or
reordering frames. The items below stay as written: they are the record of
what an embedder could not do, which is the shape a request is made of.

## 6. Ownership cannot be registered, so `OwnedSessionPolicy` cannot be wired — blocking

`OwnedSessionPolicy` keeps a channel→principal map and refuses anything unowned.
**Nothing populates that map.** `claim()` (`core/policies.py:93`) has no caller
in the library; its only call site anywhere is the test
(`tests/integration/test_multi_user.py:76`), which builds sessions in-process
and hands over `session.chat_uri` and `session.annotations_uri` straight off the
object it just constructed.

A deployment driven by clients never holds that object:

- `createSession` arrives over the wire;
- `may_create_session(info, params)` (`core/policy.py:73`) is consulted
  **before** the host mints the chat and annotations channels, so at decision
  time those URIs do not exist yet;
- nothing informs the policy afterwards.

The prefix walk in `owner_of` does not close the gap either. It matches
`"<owned>/…"`, and a chat URI is `ahp-chat://<chatId>/<base64 session uri>` —
the session URI appears *inside* it, not as its parent. So even an embedder that
claims the session URI at `may_create_session` time owns nothing else the
session goes on to create.

The behaviour is at least fail-closed: `may_see_channel` refuses unowned
channels, so the symptom is "a user cannot see their own chat", not a leak. It
is still a shipped example that cannot be used for the thing it is an example
of.

**Request: a session-lifecycle notification to the embedder** — *this session,
with these channels, was created on this connection*, and the matching disposal.
Delivered before the first subscribe can arrive. Shape is open; what matters is
that the channels a session owns are knowable by the layer making trust
decisions, without parsing a URI (invariant 15) or reaching into private state.

An alternative that would also work: let `Policy` see channel registration
directly, since that is the moment ownership becomes expressible.

## 7. `StoredSession` has no owner, so durability and partitioning do not compose

`may_restore_session` (`core/policies.py:147`) refuses by default, and its
docstring is exactly right about why: "a restored session has no owner until
somebody claims it, and `may_see_channel` refuses unowned channels — so
restoring one would produce a session nobody, including its author, can reach."

That is a correct default for a missing capability rather than a design
position. `StoredSession` carries channels, title, provider and resume state —
everything except who it belongs to. So the two headline features of a
multi-user deployment, partitioning and durability, cannot both be on.

An embedder can keep ownership in a second store and re-claim before serving,
which is what we will do in the meantime, and it means two records of the same
fact with no mechanism keeping them in step.

**Request: an embedder-owned `metadata` mapping on `StoredSession`**, round-
tripped verbatim and never interpreted by the library — the same treatment
`ProviderResumeState` already gets. Then ownership travels with the session it
describes, and `may_restore_session` has something to decide on.

## 8. The counters have no documented exposure

Request 5 landed as `Host.counters()` (`core/host.py:3559`), which is the right
API and explicitly "not a metrics endpoint — what scrapes this is the
embedder's". Agreed, and every deployed host still needs the same twenty lines:
a loopback HTTP listener that returns those counters and a readiness answer, so
a supervisor, a health gate or a reverse proxy has something to call.

**Request: not an endpoint — a worked example in `docs/guide/deploying.md`.**
Its examples are executed as tests, so a shown pattern stays correct, and every
embedder stops writing the same thing slightly differently. Readiness in
particular deserves a defined answer: "restore finished, transport bound" is not
guessable from outside.

## 9. The per-connection outbox is unbounded

`Connection._outbox` is an `asyncio.Queue()` with no `maxsize`
(`core/connection.py:58`), and `enqueue` "never blocks, never reorders". A peer
that stops reading — a suspended laptop, a wedged renderer, a client behind a
stalled proxy — accumulates frames in host memory for the lifetime of that
connection, with no backpressure and no drop policy.

On a loopback single-user host this is nothing. On a host serving several people
over a network it is a slow leak with an ordinary trigger, and the ordering
guarantee makes it exactly the wrong place for an embedder to improvise.

**Not a request for a specific fix** — the tradeoff belongs to whoever owns
invariant 10. Dropping frames breaks replay expectations; closing a slow
connection is a policy decision; a bound with a documented behaviour on
overflow is probably the answer. Filed so the choice is deliberate rather than
implicit, ideally with the limit stated in `docs/guide/deploying.md`.

## 10. Structured turns have no wire shape (upstream)

A client often wants a turn's answer as data, not prose. ahp-py supports that
today with conventions of its own: the client puts a JSON Schema in
`Message._meta["ahp-py/outputSchema"]`; the host offers the agent a host-internal
`StructuredOutput` tool whose input schema is that schema; the completed call
carries the validated value as `structuredContent`. A non-object schema is
wrapped under a `value` property, because `structuredContent` and MCP tool
arguments are objects. A turn with no valid result fails with
`ErrorInfo.errorType` `ahp-py/structuredOutput`.

**Request: a `Message.outputSchema` field, and a structured result on a
completed turn** (for example `chat/turnComplete.result`, or a typed response
part), plus a standard `errorType` for "no valid structured result".

Why upstream: without it every client has to learn a private `_meta` key and a
tool-name convention, and a different host cannot honour the request at all. The
current shape is a stopgap that stays wire-compatible, since a stock client
ignores `_meta` and shows an ordinary tool call.

## 11. Restart semantics for interrupted turns and open input requests (upstream)

**What the spec says today.** In-progress turns SHOULD be considered failed when
a server restarts. It says nothing about `session/inputNeeded` entries or the
approvals and questions behind them.

**What ahp-py does.** A turn the restart cut off is kept as a failed turn with
`errorType` `host.restart` and, when the agent can resume turns, a resumable
error; it is picked up through the standard `chat/turnResume` (automatically,
once, after a deliberate restart). Open input requests are persisted and, on
restore, withdrawn: the `inputNeeded` entry is removed and the client is told with
a `systemNotification` part carrying `_meta['ahp-py/restart']`
(`{"event": "inputWithdrawn", "requests": [...]}`). A resumed turn asks again
under new ids. See ADR 0006.

**Request.** State what a host must do with open input requests across a
restart, and give clients a standard way to learn of it: a standard
notification kind for "withdrawn because the host restarted" that names the
request ids, and a standard error kind for "failed by a restart" (so a turn is
not just an error with a private `errorType`).

Why upstream: every client otherwise has to learn a private `_meta` key and an
`errorType` string, and cannot tell a request still being waited on from one
that was withdrawn, except by trying to answer it.

## 12. A session started on another session's behalf has no origin (upstream)

**What the spec says today.** `SessionOrigin` has one kind, `automation`. A
chat spawned by a tool call can point back to it (`ChatOrigin` `{kind: "tool",
chat, toolCallId}`, with `ToolResultSubagentContent` on the parent's side), but
only within one session.

**What ahp-py does.** `ahp-host-flow` (plan §2) runs a workflow's `agent()`
calls, through `ahp-gateway`'s fleet tools, as ordinary sessions on other
nodes, so every surface lists them. The parent side is covered: the run's
worker chat links the node session. The child side is not: the node session
cannot say which run, chat and tool call started it, so a client lists it as
one more top-level session, unrelated to the run, and cannot nest or group it
under its workflow.

**Request.** A `SessionOrigin` kind for delegated sessions:
`{kind: "delegation", session, chat, toolCallId}`, naming the session, chat and
tool call on whose behalf the session was started (possibly on another host),
set at `createSession` by the client that starts it, so a client can render it
as the child of that call, as it does a `tool`-origin chat.

**What ahp-py does.** An interim kind and key, until upstream names one:
`createSession` `_meta["ahp-py/origin"]` carries `{kind: "ahp-py/delegation",
session, chat, toolCallId}` (`ahp_protocol.ext.delegation`; `ahp-client`'s
`create_session(origin=)` sends it, and `ahp-gateway`'s fleet `start_session`
tool takes `originChat` and `originToolCall` for it). The host seeds it into the
session's immutable `origin`, so it is in the session summary and persisted
with the session. Only this kind is accepted from a client; `automation` stays
host-only and any other kind is ignored. Clients keep unknown kinds verbatim,
so a stock client lists the session as before.

Why upstream: the link has to be on the session itself, where every client
reads it; a private `_meta` key would only be understood by ahp-py's own
clients, and the shape mirrors `ChatOrigin`'s `tool` kind, which clients
already render.

## 13. Redaction after publishing (upstream)

**What the spec says today.** Nothing. History is append-only: there is no
action that changes what an already-published part says, so content that
should not have been published stays in every client's mirror and in any
replay of the channel.

**What ahp-py does.** An extension, gated by the capability
`_meta['ahp-py/redaction']` sent at `initialize`: the host-initiated actions
`ahp-py/chat/partRedacted` `{turnId, partId, reason, redactedAt}` and
`ahp-py/chat/messageRedacted`, reduced by the pure
`ahp_protocol.ext.redaction.apply_redaction`. A stock client is never sent the
action; the host truncates the channel's replay log at the redaction so the
original cannot be replayed, and a stock client gets the redacted state on its
next snapshot. See ADR 0007.

**Request.** A standard `chat/partRedacted` and a message variant, with the
reducer semantics of `apply_redaction`:

- The target is a turn id plus a part id, using the reducer's identity rule for
  parts (the same one `chat/responsePart` updates use); a message variant
  targets the turn's user message.
- The replacement keeps the part's kind and id; its text becomes
  `[redacted]` and `_meta` records `{reason, redactedAt}`. A tool call keeps its
  id, name and status; its text fields and content are replaced and structured
  content is dropped.
- An unknown target leaves the state unchanged.
- Only the **shared view** is redacted. What an agent holds in its own context
  is outside the protocol and is not covered.

Why upstream: without a standard action, a redaction is invisible to every
connected stock client until it resubscribes, replay must be truncated by
convention, and a client cannot tell a redacted part from one that said
`[redacted]`.

## 14. Who sent a message and who answered (upstream)

**What the spec says today.** A message and the answer to a pending input or tool
confirmation carry a `clientId`, which a peer chooses. Nothing names the person.
Connection admission is outside the protocol, so a host cannot tell a provider who
asked or who approved.

**What ahp-py does.** An optional `Policy.principal_of(info)` names the person behind
a connection. The host stamps it on `AgentSessionContext.principal` (the creating
connection), on `UserMessage.principal` (the sender of each turn) and as
`answered_by` on `InputOutcome` and `ToolConfirmationOutcome`. It is stored under
`metadata["ahp-py/principal"]`, never put on the wire, and is `None` without the hook.

**Request.** An optional identity on the wire, so any host and client can agree on it:

- A `principal` string on a user message and on the resolution of an input or tool
  confirmation request, set by the host from its own admission and never taken from
  the peer's message.
- Clients must preserve it verbatim and may display it.
- Its meaning stays the host's; the protocol only carries it.

Why upstream: without it every deployment invents its own header and metadata key,
and an agent that wants "this person approved" has no portable field to read.

## Still deliberately not requested

The list above holds, and one addition:

- **Content in `AuditEvent`.** `toolcall.resolved` records who resolved which
  call, and the type carries identifiers only, by construction. Recording *what
  ran* — the approved tool input — belongs to the embedder's own trail, where it
  is already subject to that deployment's retention rules. Two records, each
  honest about its scope, is the right shape; putting tool input in the audit
  event would quietly make the audit log a transcript, which is the mistake
  `--wire-log` already documents.

