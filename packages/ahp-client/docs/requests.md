# Requests from an embedder

Written from the outside in: what an application embedding this client runs into
when it uses the library to build a **long-lived, server-side surface** — a
process that holds one connection per end user against a host it did not start,
attaches to sessions other clients created, and renders turns it did not begin.

That is the shape the README's ten-line example is deliberately not about, and
correctly so: `connect` → `create_session` → `prompt` is the right front door
for a script, a CLI or a single-user tool, and making it the only front door
would be the wrong trade. None of these requests asks for that example to get
heavier. Every one of them is about the *second* consumer — the one for which
the turn it is watching is somebody else's — being able to stay above
`client.protocol`.

The escape hatches are real and they are why nothing here is fatal: `mirror`,
`protocol` and `events()` are all public and documented as such, and every item
below can be built on them today. The point of each request is that building it
means re-deriving something the library already knows, in a place where getting
it wrong is silent.

Ordered by whether it blocks. Items 1–3 block a co-presence surface; 4–6 and 9
are smaller; 7–8 are documentation.

**Status: 1–4 and 9 are implemented.** 9 was filed afterwards, against the
implementation of 2. Each section keeps its original text — the
request is the record of *why*, and rewriting it into a description of what was
built would lose the reasoning that justified it. What was actually done is
noted under each, including where it differs from the proposed shape. 5–8 are
open.

---

## 1. A turn can only be watched by the client that started it — blocking

`TurnStream` is the only typed event surface, and it is bound to a turn of its
own making from construction:

- `TurnStream._start` (`api/client.py:417`) **dispatches `chat/turnStarted`**.
  There is no path into the iterator that does not first send a message.
- `TurnStream.__anext__` filters on `turn_id != self._turn_id`
  (`api/client.py:476`) and drops everything else.
- `_start` also acquires the event reader (`api/client.py:425`), so the stream
  begins reading *at dispatch* — a turn already in flight when we attach has no
  entry point at all.

`Client.open_session` (`api/client.py:255`) exists and does the right thing —
subscribes, refuses to dispose what it does not own. But the object it returns
can only be *read* (`Session.state`, `Chat.turns()` — mirror snapshots) or
*written to* (`prompt`, which starts a new turn). There is nothing between them:
no way to say "render whatever this chat is doing, starting now".

**Why this blocks.** Two clients on one session, both rendering the same live
turn, is the thing the protocol exists for and the thing a bespoke per-surface
stream cannot do. An embedder building the second client has to drop to
`client.events()`, filter envelopes by channel, and re-implement the dispatch
`TurnStream.__anext__` already performs — including the parts that are not
obvious from the outside: that a terminal event must be *yielded* rather than
raised past, that `TurnCompleted.text` is re-read from confirmed state rather
than accumulated from deltas (`_finalise`, and the `turns`-before-`activeTurn`
rule in `text()`), and that a dropped connection means the turn is *failed*
rather than ended. Each of those is a comment in this repository explaining a
mistake somebody already made. A second consumer gets no benefit from any of
them.

**Proposed shape.** No new machinery — the same iterator with its two
originator-only assumptions lifted:

- `Chat.watch()` (and `Session.watch()`, folding in the default chat) returning
  the same `AsyncIterator[TurnEvent]`, with no dispatch on entry and no
  `turn_id` filter, so `TurnStarted` for somebody else's turn is an ordinary
  event rather than the thing that never arrives.
- A way to enter mid-turn: if `activeTurn` is present at `watch()` time, emit a
  synthetic `TurnStarted` (or a distinct `TurnInProgress`) carrying the turn id
  before the first live envelope, so a UI has something to open a bubble on.
- Export `event_for` (`api/events.py:318`). It is the whole envelope→event
  mapping, it is documented as never raising, and it is not in `__all__` — so
  an embedder writing its own loop today either imports a private name or
  writes a second `_BY_TYPE`. The second copy is the one that will not know
  about the next action type added here.

`TurnStream` stays exactly as it is. This is a sibling, not a rework.

**Implemented.** `ChatWatch`, reached through `Chat.watch()` and
`Session.watch()`, with `event_for` now in `__all__`.

Two notes where the built thing differs from the request. The mid-turn entry
event is a distinct `TurnInProgress` rather than a synthetic `TurnStarted`: a
consumer that cannot tell "this began now" from "this began before you were
looking" will replay an animation or log a start that already happened, and
there is nothing in a forged `TurnStarted` to warn them. It carries the text so
far, read from the mirror, so a bubble opens populated rather than empty. And
`watch(from_start=False)` opts out, because a consumer that only wants the live
tail should not have to filter out an event it never wanted.

---

## 2. Answering a request as a non-originating client is only reachable below the front door

Everything needed to answer an approval from a client that did not start the
turn is already implemented, and none of it is on `Session`:

- `pending_inputs(mirror, session_uri)` (`serve/inputs.py:29`) reads
  `SessionState.inputNeeded` — the aggregate whose *stated purpose* in that
  module's docstring is that "a client can answer without subscribing to the
  chat".
- `InputResponder` (`serve/inputs.py:38`) covers all four blocking kinds, and
  its `answer` merges against the mirror rather than clobbering another
  client's partial answer — which is exactly the multi-client concern, solved,
  one layer below where a multi-client consumer is looking.

Reaching them means constructing `InputResponder(client.protocol, client.mirror)`
by hand, from a module under `serve/` whose name suggests it is about serving
resources to the host.

**Why it matters even though it works.** The front door teaches approvals
through `ToolCallReady.approve()` on a `TurnStream` — i.e. only ever for a turn
you started. An embedder who reads the README and the `api/` package concludes
that answering somebody else's approval is not supported, because at that
altitude it is not. The capability being present in `serve/inputs.py` does not
help someone who has no reason to look there.

**Proposed shape.**

- `Session.pending_inputs()` → `list[JsonObject]`, and `Session.responder` →
  `InputResponder`, both thin delegations.
- An event when the set changes. Today a UI polls `Session.state` or filters
  raw envelopes, because `inputNeeded` transitions arrive as session-channel
  actions with no typed surface. `Session.inputs()` as an async iterator, or an
  `InputNeededChanged` event on the `watch()` stream from item 1, would mean a
  session list can render "waiting on a human" without either polling or
  hand-rolled envelope filtering.
- One line in the README's approvals section noting that a pending call is
  answerable by any subscriber, not only by the turn's originator. The host
  arbitrates and `confirm_tool` already documents first-answer-wins
  (`serve/inputs.py:72`); it is the front door that does not say so.

**Implemented.** `Session.pending_inputs()`, `Session.responder`, and
`Session.inputs()` as an async iterator yielding the pending set whenever it
changes.

`inputs()` is derived from **mirror state**, not from envelopes. `inputNeeded`
moves for several reasons — a request opening, another client answering one, a
turn ending — and rebuilding it from `session/inputNeededSet` and
`session/inputNeededRemoved` means re-deriving what the session reducer already
computed. That is the same argument the rest of this document makes, so it
applies here too.

It first shipped waking on an interval, which item 9 then took issue with — not
with the mirror being the source, which is right and unchanged, but with the
interval being the mechanism. It now wakes on the envelope and re-reads the
mirror. See item 9.

---

## 3. A rejected credential is retried forever — blocking for token auth

The supervisor treats every connection failure as transient:

```python
except Exception as exc:  # a failed attempt is data, not a crash
    self._transition(HostState("reconnecting", attempt, exc))
```

`hosts/runtime.py:289`. With the default `exponential_policy()` —
`max_attempts=None`, "forever", correctly documented as the default — a
connection refused because the token is wrong or expired is retried on the same
schedule as one refused because the host was restarting.

The information needed to tell them apart is destroyed one layer down.
`WebSocketClientTransport.connect` (`ws/transport.py:80-88`) wraps every failure
as `TransportError("io", …)`, so `websockets.InvalidStatus` — which carries
`.response.status_code`, i.e. the 401 or 403 a proxy returned on the upgrade —
becomes a string. `ReconnectPolicy` (`hosts/policy.py:45`) then has nothing to
branch on even if an embedder wanted to write the branch: it decides on
*attempt count* alone.

**Why this blocks.** An identity-aware proxy in front of the host is the
standard deployment shape for anything not on loopback, and per-user tokens
expire. On expiry, the embedder's surface enters an invisible retry loop: the
user sees a disconnected client, the logs show `reconnecting`, and nothing
anywhere says "re-authenticate". Worse, the loop is *load* — one doomed
handshake per user per backoff interval against the very proxy that is rejecting
them.

This is also the failure mode the repository's own comment on `hosts/policy.py`
warns about from the other direction: a policy copied from Go "retries forever
exactly where the author meant *do not retry*". The polarity is right here; the
missing piece is that some failures should not be retried at any polarity.

**Proposed shape.**

- Preserve the status on the transport error: a `status` (or `http_status`)
  field on `TransportError` when the handshake was answered and rejected, and a
  `kind` that distinguishes `"rejected"` from `"io"`. The redaction in
  `_redact` (`ws/transport.py:164`) is the right instinct and a status code
  leaks nothing.
- A classification hook on the policy — `ReconnectPolicy.should_retry(failure)`
  or a `fatal_errors` predicate — defaulting to today's behaviour so nothing
  changes for existing callers.
- A default that declines to retry the three unambiguous ones: an HTTP 401/403
  on the upgrade, a `1008` policy-violation close, and a version-negotiation
  refusal (`-32005`), which is a permanent disagreement about the wire and
  cannot be improved by waiting.
- `HostState("failed", …)` is already the right terminal state and is already
  broadcast on `state_changes()`; it just needs to be reachable without
  exhausting a finite attempt budget. An embedder can then map "failed with a
  rejected credential" onto a re-login prompt, which is the whole ask.

**Implemented, as proposed.** `TransportError` gained `kind="rejected"` plus
`status` and `close_code`; `WebSocketClientTransport.connect` maps
`websockets.InvalidStatus` rather than flattening it, and a 1008 close is
classified as a refusal. `ReconnectPolicy.should_retry` defaults to
`default_should_retry`, which declines HTTP 401/403, a 1008 close and `-32005`,
and retries everything else. `retry_everything` restores the previous behaviour
under a name rather than a lambda.

One deliberate departure from "defaulting to today's behaviour so nothing
changes": the default predicate **does** change behaviour for those three cases.
A hook that ships inert is a hook nobody enables, and the three refusals it
declines are the ones the request calls unambiguous.

---

## 4. `connect()` cannot be given a TLS configuration

`connect(url=…)` builds its transport in a closure (`api/client.py:133`) that
forwards `token` and `headers` and nothing else, and
`WebSocketClientTransport.connect` (`ws/transport.py:59`) has no `ssl`
parameter, so `websockets.connect` gets the default context.

Consequences for a `wss://` host whose certificate chains to a private CA — a
completely ordinary posture for an internal deployment, and the same one that
makes item 3's proxy meaningful: the handshake fails verification, and the only
way out of it is to abandon `connect()` for a hand-written `transport_factory`.
That is a documented and working escape hatch, but it means reimplementing the
one thing the closure does — and the token handling it drops is the
security-sensitive part (`_with_token` percent-encodes, and `_redact` keeps the
credential out of the exception that a failing private-CA handshake is about to
raise).

**Proposed shape.** `ssl: ssl.SSLContext | None = None` on
`WebSocketClientTransport.connect`, passed straight to `websockets.connect`, and
threaded through `connect()` next to `token` and `headers`. No policy, no
`verify=False` convenience flag, no CA-bundle path parsing — an `SSLContext` is
the standard currency and anything friendlier would be the library taking a
position on certificate trust, which it should not.

Client certificates fall out of the same parameter for free, which is the other
half of the deployments that need it.

**Implemented, as proposed.** `ssl: SSLContext | None` on both
`WebSocketClientTransport.connect` and `connect()`, passed straight through. No
`verify=False`, no CA-bundle path parsing.

---

## 5. A client that attaches gets whatever history the snapshot carried

`Chat.turns()` (`api/client.py:350`) reads `turns` from the mirror, which holds
what the subscribe snapshot delivered. `fetch_turns` exists on the command
surface (`client/commands.py:160`) and is not surfaced anywhere above it, so
paging older history means calling `client.protocol.fetch_turns(...)` and then
deciding what to do with a result the mirror does not know about.

For a client that creates its session this is a non-issue — it has seen every
turn. For one attaching to a long-lived session it is the first thing the UI
needs, and the question an embedder cannot answer from the docs is not "how do I
call it" but "**what is the relationship between the fetched page and the
mirror?**" Does a fetched page merge into `turns`? If it does, is a subsequent
snapshot going to drop it? If it does not, the embedder maintains a second,
parallel transcript and has to decide which one wins where they overlap — and
"two transcripts, one conversation" is a bug factory.

**Proposed shape.** Either is fine; the ambiguity is the problem.

- `Chat.history(limit=…, before=…)` returning a page, **plus one paragraph
  stating that the mirror is not backfilled** and that pagination is the
  embedder's to hold — or,
- the same helper merging into mirror state, with the snapshot-overwrite
  interaction spelled out.

The spec's cursor rule ("MUST NOT parse, modify or persist across connections")
is already honoured and documented on `Client.sessions` (`api/client.py:203`);
whatever shape this takes should carry the same note, because a paging UI is
precisely where someone will be tempted to stash a cursor.

---

## 6. `label` is both the display name and the client-identity key

`HostRuntime._resolve_client_id` (`hosts/runtime.py:494`) keys the store on
`self._config.label`, and `label` defaults to the URL (`hosts/runtime.py:129`, set at `api/client.py:152`) and
is otherwise the human-readable name in error messages and the supervisor task
name (`hosts/runtime.py:216`).

For one app against one host, that is exactly right. For a process holding a
connection per end user against the *same* host, the two meanings come apart:
every connection wants a distinct durable `clientId`, and they all share one
label. The default `InMemoryClientIdStore` is per-runtime so nothing collides
today — but an embedder who wants ids to survive a restart configures a
`FileClientIdStore`, and a shared one hands every user the same id. The store's
own docstring says "last writer wins across processes"; within one process
across users, it is worse than that, because they would all *load* the winner.

`clientId` is an identifier and not an authenticator, so this is not a
privilege-escalation claim — a host that partitions by connection identity is
unaffected. It is a correctness one: several live connections asserting one id,
and `reconnect` resuming the wrong one's state.

**Proposed shape.** Separate the two names — `identity_key: str | None = None`
on `HostConfig`, defaulting to `label`, and used as the store key — and a
sentence in the `client_id_store` docstring saying that an embedder with more
than one logical client per host must vary it. Setting `client_id=` explicitly
per connection already works and is arguably the right answer for this consumer;
it is just not discoverable from a store whose key is invisible.

---

## 7. No stated posture on many connections in one process

Nothing in the docs says whether N concurrent `connect()` calls on one event
loop are supported, and the reading of the code is that they are: no module
globals, a runtime per connection, a mirror per runtime, broadcast readers per
runtime. The mirror's single-thread invariant is enforced rather than merely
documented, which is the property that matters most here and is already the
subject of a commit.

The request is to **say so** — one short section in `AGENTS.md` or the README
covering: that connections share no process-global state; roughly what one
connection costs (tasks, buffers, the `max_size=16 MiB` frame ceiling in
`ws/transport.py:67`, which is per connection and is the number that decides how
many fit in a memory budget); and that `client_id` must vary per connection
(item 6). A reader deciding whether this library can back a multi-user surface
is currently reading the source to find out, and the answer is favourable — it
just is not written down.

---

## 8. Two defaults worth documenting rather than changing

Neither is wrong; both surprise a server-side consumer.

- **`idle_timeout=300.0` on a turn** (`api/client.py:377`) is an *idle* timeout,
  measured between events, which is the right measure. It is still five minutes
  of silence, and a single long-running tool call with no interleaved deltas is
  a real way to reach it on a host doing slow work. `TimeoutError` out of
  `__anext__` is a good loud failure; a line noting that the ceiling is per-gap
  and that `None` disables it would stop an embedder discovering the interaction
  in production.
- **`ready_timeout=30.0` on `create_session`** (`api/client.py:218`), with the
  `lifecycle == "creating"` short-circuit in `_await_ready` that the docstring
  explains well. Worth stating the consequence for a host that answers neither
  `ready` nor `creating`: 30 s of 10 ms polling before an error. Hosts with slow
  bring-up exist, and the fix is a parameter the caller already has.

---

## 9. `Session.inputs()` polls, and the choice that forced it is a false binary

Filed after reading the implementation of item 2, which is otherwise exactly
right.

`Session.inputs()` (`api/client.py:344`) loops on `pending_inputs()` every
`poll` seconds, default 0.05. The reasoning given for it — that `inputNeeded`
moves for several reasons and rebuilding the set from
`session/inputNeededSet` / `session/inputNeededRemoved` re-derives what the
session reducer already computed — **is correct, and this request does not
dispute it.** The mirror should stay the source of truth.

But "read the mirror" and "wake on envelopes" are not the two ends of one axis.
The third option is to do both: block on the event reader, and on any envelope
for this session's channel, re-read `pending_inputs()` and yield if it differs.
The reducer still computes the set; the envelope is only the *clock*. Nothing is
reconstructed from actions, and no interval is guessed.

**Why it is worth a follow-up rather than a shrug.** A 50 ms poll is invisible
for one session and is not what this consumer runs: a surface holding a
connection per user, each watching several sessions, pays a wakeup per session
per 50 ms forever — 20 Hz of mirror reads and list comparisons whose overwhelmingly
common answer is "nothing changed". It is also the one place in this library
where latency is a *guess*: everything else here is edge-triggered, and an
approval prompt is precisely the thing a user is waiting on.

**Proposed shape.** Keep the signature, keep the mirror as the source, replace
the sleep with a wait on the event reader filtered to the session channel.
`poll` stays as a fallback interval — a belt-and-braces tick for anything that
mutates `inputNeeded` without an envelope this client sees — but as a ceiling on
staleness rather than the mechanism, so it can default to something like 5 s
instead of 50 ms.

`ChatWatch` already does the reader half of this correctly and is the model.

**Implemented.** `Session.inputs()` now waits on the event reader, filtered to
this session's channel, and re-reads `pending_inputs()` on a wake. The mirror is
still the source; the envelope is only the clock. `poll` keeps its name and its
place in the signature and becomes the staleness ceiling, defaulting to 5 s.

Two notes on what the change turned up.

The reader is attached **before** the first `pending_inputs()` read. The old
loop had no reader at all, so this is new ground rather than a preserved
property: a request opening between the first read and the first wait would have
been missed until the next tick, which under a 5 s ceiling would have been a
regression rather than the invisible 50 ms it was before. `ChatWatch._open`
already had the same rule for the same reason.

The channel filter is **not** observable through `inputs()`, and its test says
so. An unfiltered wake re-reads the mirror, finds the set unchanged and yields
nothing — so a black-box test of it passes whether the filter is there or not.
It is an efficiency property (a busy chat must not cost a mirror read and a list
comparison per delta), and it is asserted on `_wait_for_input_change` directly.
The two properties that *are* observable — waking on the envelope, and ending
when the stream ends — are tested through the public generator.

---

## Deliberately not requested

Listed so the omissions read as decisions rather than oversights.

- **A CLI, a TUI, or a sync facade.** `AGENTS.md` scopes all three as M9
  ergonomics. An embedder building its own surface uses the library, not a CLI,
  and a sync facade over an async protocol is the wrong shape for a server-side
  consumer regardless.
- **The `mcp://` side-channel and `ahp-otlp:` telemetry.** ADR 0007 excludes
  them and `mcpApps` is correspondingly never advertised. Advertising a
  capability that is not backed is worse than not having it.
- **Anything that changes a wire type, an action shape or an error code.** The
  README states the rule and it is the right one: this project targets an
  external specification, and a client that improves the protocol is a client
  that does not interoperate.
- **A default that disables `reasoning_delta`, or any other content filter.**
  Whether reasoning text may leave a deployment is the embedder's policy
  question. The client's job is to deliver what the host sent and let the
  consumer decide — `Reasoning` being an ordinary event is correct.
- **A built-in resource provider, or any filesystem default.** `serve/` being
  opt-in and `resources=None` by default is the right posture: a client that
  serves the host a filesystem it was not explicitly given is a second boundary
  nobody asked for.
- **A timeout on an approval.** Upstream imposes no deadline on a suspended
  request because a human is on the other end, and that is right. A deployment
  that needs "unanswered denies rather than hangs" owns that rule, and it
  belongs where the denial is enforced rather than in a client that might not be
  the one watching.
