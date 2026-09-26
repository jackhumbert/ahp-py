# ADR 0005 — One primitive for a provider request that waits on a client

**Status:** accepted · **Date:** 2026-08-01

## Context

[ADR 0003](0003-provider-emits-neutral-events.md) made the `TurnSink` a
fire-and-forget reporting interface: the provider says what the agent did, the
host maps it to actions. That covers everything a turn does *on its own*.

It does not cover the case where the agent has to **stop and wait for a human**.
Four separate features need that, and each needs the identical missing thing —
a provider-initiated request that suspends until an action arrives on *some
other connection*:

| Feature | Waits for | Arrives as |
|---|---|---|
| Elicitation | The user answering a question | `chat/inputCompleted` |
| Tool-call confirmation | Someone approving the call | `chat/toolCallConfirmed` |
| Client-contributed tools | The client running the tool | `chat/toolCallComplete` |
| Auth step-up | A token being pushed | the `authenticate` **command** |
| Terminal claim hand-back | A peer releasing the terminal | `terminal/claimed` |

A scoping pass over the protocol surface proposed three *different* mechanisms
for these, in three different areas: a "pending-challenge registry" for auth, "a
request/response neutral event under ADR 0003" for elicitation, and "neutral
provider events so an agent can own a terminal" for terminals. Three registries,
three lifetimes, three cancellation semantics — arrived at independently, none
of them wrong on its own, and collectively unmaintainable.

The resolution never comes back on the connection that caused the turn. It comes
from whichever client the user happened to answer on, which is the entire reason
AHP exists. So this cannot be modelled as a return value from anything.

## Decision

**One registry, one lifetime, one cancellation rule — and the provider awaits a
neutral outcome, never an action.**

```python
outcome = await sink.request_input(InputRequest(message="Which environment?", questions=[...]))
if outcome.response == "accept":
    ...
```

Concretely:

1. **`core/pending.py` owns every suspended request.** One registry, keyed by a
   host-minted id. Nothing else may hold a future waiting on a client.
2. **The id is minted by the host, never taken from the provider or a client.**
   A provider-chosen id would let two providers collide; a client-chosen one
   would let a peer resolve a request it was never offered.
3. **The scope is the turn.** When a turn completes, fails, is cancelled, or its
   session is disposed, every request opened under it is cancelled — the
   provider's `await` raises `CancelledError`, which is already how turn
   cancellation reaches it.
4. **Resolution happens after the reducer, on the normal dispatch path.** The
   host applies the client's action, then resolves the future. State and the
   provider therefore never disagree about whether the request was answered.
5. **No default timeout.** A human is on the other end of all five of these. A
   provider that wants one wraps its own `asyncio.timeout`; a host-imposed
   deadline would cancel a turn because someone went to lunch.
6. **The outcome is neutral.** `InputOutcome(response, answers)` — not
   `ChatInputCompletedAction`. ADR 0003's reasoning applies unchanged: an
   adapter that names wire actions dies at the next spec relocation, and the
   `chat/input*` family has already moved channels once.

## Rationale

**1. The three proposals differ in exactly the places that are hard.** Not in
shape — all three are "park a future, resolve it later" — but in lifetime,
cancellation and whether a late resolution is an error. Those are the details
that produce leaks and hangs, and settling them once is worth more than the code
saved.

**2. Cancellation is the part that must not be reinvented.** A suspended
provider holds a turn open. Get the scope wrong and a cancelled turn leaves a
future nobody will ever resolve, the session's status stays `InputNeeded`
forever, and the only cure is disposing the session. Binding the scope to the
turn structurally — rather than asking each feature to remember — is the whole
value.

**3. The protocol already models these as one thing.** Upstream aggregates all
of them into `SessionState.inputNeeded`, whose variants are exactly
`SessionChatInputRequest`, `SessionToolConfirmationRequest`,
`SessionToolClientExecutionRequest` and `SessionToolAuthenticationRequest`. It
is one concept upstream; making it four here would be our invention, not the
spec's.

**4. It keeps the host's authority intact.** The provider never dispatches
`chat/inputRequested`, so it cannot mis-order the response stream, invent a
request id, or resolve its own request. Same argument as ADR 0003 §3.

## Consequences

- `TurnSink` grows awaiting methods alongside its reporting ones. It is no
  longer purely fire-and-forget, and that is the point.
- **An adapter can now deadlock its own turn** by awaiting input from a session
  no client is watching. That is a real new failure mode. It is bounded by the
  turn scope — cancelling the turn frees it — and `session/inputNeeded` makes it
  visible rather than silent.
- The registry is in-memory and therefore does not survive a restart. A pending
  request after a crash is a dead request; the turn is already reported failed
  by the durability work, so the transcript stays honest.
- **Cost:** the neutral outcome vocabulary has to be evolved deliberately, the
  same cost ADR 0003 accepted. Adding auth step-up or client tools later means
  adding an outcome type, not a second mechanism.
- Validation of the resolving actions stays host-side, because the reducers
  deliberately do not do it: upstream lists the `chat/input*` rejection rules as
  things "servers SHOULD reject", in prose, with no reducer support.
