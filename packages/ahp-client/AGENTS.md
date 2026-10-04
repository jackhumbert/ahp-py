# Agent guide

For AI agents and humans building this repository. Assume the reader starts with
zero context.

**Read first:** [`docs/plan.md`](docs/plan.md). It is the whole design, it is
grounded in evidence rather than preference, and every section below is a
pointer into it. If this file and the plan disagree, one of them is wrong — fix
it in the same PR, never leave them silently inconsistent.

## Keep it generic

This repository is public. It is a library for anyone to embed or run against
their own deployment, so nothing tracked may name or depend on one particular
setup: no hostnames, machine names, domains, home-directory paths, IP
addresses, tokens, employers or internal projects. Use placeholders
(`example.com`, `my-mac-mini`, `/Users/me`) in code, tests, docs *and* commit
messages — a commit message is as public as the code.

Deployment glue (service files, reverse-proxy config, one fleet's layout)
belongs in the deployment, not here. A feature one setup needs is generalised
into an option or left out.

Anything an agent needs to know about the local setup lives in
`AGENTS.local.md`, gitignored by `*.local.*`. Read it if it exists; never copy
from it into a tracked file.

## What this project is

A Python **client** for the Agent Host Protocol. AHP is an external
specification owned by Microsoft. We implement it; we do not design it.

Upstream ships clients for Rust, TypeScript, Kotlin, Swift and Go, and a host
library in no language at all. There is no Python client. There is a Python
host — [`ahp-host`][server] — and the two are meant to meet.

```
                ahp-protocol          ← the shared layer
                  ▲             ▲
                  │             │
        ahp-host   ahp-client ← this repo
```

**Current state: M1–M8 of `docs/plan.md` §12 are done, with the two
exceptions below; M9 (release) is not.** The suite (count it from
`pytest --collect-only`, never from prose — a number here went stale twice),
`mypy --strict`, `ruff`, `ruff format` and three import-linter contracts
green. A full turn runs against the sibling Python host. The shared layer
([`ahp-protocol`][protocol]) is green on 247 reducer fixtures,
39 round-trips and the JS-semantics oracle.

The two exceptions, so nobody re-discovers them: **`MultiHostStateMirror`
(§6.3) was never built** — `hosts/` supervises one host per runtime, and an
embedder with several holds one `HostRuntime` each — and **`ResourceWatchServer`
(§8) is deliberately absent** — §1.3 argues a watch server buys nothing the
command does not already say, so `createResourceWatch` is routed and declined
with `-32601` rather than implemented.

Also deliberately **not** built: the `mcp://` side-channel and `ahp-otlp:`
telemetry (ADR 0007 — and therefore `mcpApps` is never advertised), a CLI
entry point, a TUI, and a sync facade. The last three are ergonomics the plan
scopes and M9 has not reached.

## Depend on the shared layer; do not fork it

`ahp-protocol` holds the wire types, all ten reducers, version
negotiation, the error taxonomy, the transport ABC and the conformance corpora.
**Do not copy any of it into this repository**, and do not re-port a reducer.
The reducers are ~2,500 lines of hand-ported JavaScript semantics with a
documented history of six defects an adversarial oracle had to find; the
fixture corpus cannot see that class of bug, so two copies would stay green
while diverging. See [ADR 0002][adr2] in the protocol repo.

Pin `ahp-protocol ~= 0.1.0` and assert
`ahp_protocol.UPSTREAM_PROTOCOL_VERSION` in `tests/docs/`, so a
dependency bump that moves the spec under us fails loudly.

## What "fully featured" means here

Not "the TypeScript client, in Python." `docs/plan.md` §1.1 has the table; the
short version is that the reference clients leave real ground uncovered and
matching them exactly would inherit their gaps. We exceed them on: all seven
reducers in the mirror, reducer binding that is not scheme-based, real
write-ahead reconciliation, all 10 server→client requests routed with 9 served
(`createResourceWatch` is declined on purpose — §1.3), `clientInfo` and
`capabilities` on the handshake, verified version negotiation, all 9 server
notifications, and sequence-gap detection.

That table is about the *protocol* layer and every row of it holds. **§1.3 is the
other axis and is less flattering:** it found four of the seven channels with no
typed API at all, and that essentially every client defect lived on a surface
that *does* have one. Multi-chat (§7.3), changesets (§7.4) and terminals (§7.5)
have since been built; resource watches have not. Read §1.3 before adding a
wrapper: an unwrapped surface is a gap in ergonomics, a wrapper is a place to be
wrong, and §1.3 argues resource watches should stay absent. **A gate needs a
reason as much as an omission does** — [ADR 0008][adr8] is the four-part test a
local refusal has to pass before a typed surface may raise on a call the host
would have accepted.

**Every divergence from a reference client is an ADR.** Divergence is a
decision, not an accident. `docs/plan.md` §13 lists the ADRs to write before the
code they justify.

## Verified surface (count from the code, never from prose)

- **31** client→server requests, **2** client notifications. The sibling host
  dispatches all 33; `moveChat` (1.0.0) moves a chat between sessions only
  when its provider implements `TransfersChats`
- **10** server→client requests, **9** server→client notifications
- **106** action types, **47** client-dispatchable
- **308** reducer fixtures, **67** round-trips

Every design proposal that fed this plan wrote 28 or ~30 commands. Only a
*generated* parity matrix catches that, which is why `docs/parity.md` is
generated by `scripts/generate_parity.py` and asserted by
`tests/docs/test_parity_matrix_is_true.py` — deriving each row from the command
table, the notification dispatch map, `IS_CLIENT_DISPATCHABLE`, the registered
reducers and the router's method set. A hand-kept list never contains the thing
someone just added.

## Layering

`docs/plan.md` §4 has the full layout. The contracts that matter:

```
(cli)  (sync)  (api)  (hosts)  (serve)  client  (ws)  (wirelog)
```

Two forbidden contracts beyond the layers:

- **`client` may not import `serve` or `hosts`.** `client` *defines* the
  `ServerRequestHandler` protocol, `serve` implements it, `hosts` or the
  application wires them together. That inversion is what keeps `serve` above
  `client` while `client` still dispatches into it.
- **`testing` may not import `cli` or `ws`**, so a downstream test suite stays
  offline.

## Invariants that must not break

The protocol-level invariants live in the shared package's `AGENTS.md` and are
enforced there. These are the client's own, and each produces a silent failure
rather than a test failure.

1. **Never pick a reducer from a URI scheme.** Bind at registration, from the
   channel kind the caller already knows. Where that is impossible use
   `reducer_for_state()`. VS Code mints `<provider>:/<uuid>` for sessions and
   three `agenthost-terminal:` forms for terminals; scheme routing binds *no*
   reducer, and state then freezes while actions keep arriving.
2. **Match snapshots on `Snapshot.resource`, never positionally.**
   `initialSubscriptions` returns snapshots only for state-bearing channels, so
   a stateless channel in the list yields no entry and the arrays are not
   aligned. A positional `zip` is the default mistake.
3. **Retain per-channel `fromSeq` baselines** alongside the host-global
   `lastSeenServerSeq`. A late `subscribe` snapshot otherwise re-applies
   already-counted actions. This is the one place a client can silently
   double-apply.
4. **Buffer envelopes for a registered channel whose snapshot has not yet
   arrived**, replaying them on snapshot filtered to `serverSeq > fromSeq`. The
   TS mirror drops them; that is a bug we do not reproduce. The scope matters:
   a channel with no `bind()` entry at all is `UNKNOWN_CHANNEL` and dropped —
   which is why the mirror must be bound *before* `subscribe` goes out, as
   `hosts/runtime.py` does. A direct `AhpClient` user who subscribes first
   loses whatever arrived during the round trip.
5. **Reconciliation matches on exact `clientSeq`**, following VS Code
   (`agentSubscription.ts:321`), not Swift's cumulative ack — and reproduces VS
   Code's second arm: an own echo with no matching pending entry and no
   `rejectionReason` is still applied to confirmed. Three corollaries, each of
   which was wrong once:
   - **`rejectionReason` is on the envelope, not on the originator's copy.** The
     host fans a refused action out to *every* subscriber with its own state
     untouched, so no peer may apply it — scoping the check to our own `origin`
     makes every observer diverge permanently, with nothing that corrects it.
     Advance the channel's `last_seq` anyway: the host consumed that `serverSeq`.
   - **Sequence gaps are measured against the global mark, never a channel's.**
     `serverSeq` is one host-global counter, so per-channel contiguity is not a
     property the protocol provides, and two subscribed channels turn ordinary
     interleaving into a gap per envelope.
   - **A pending action a reconnect keeps must be re-sent** (`redispatch`, same
     `clientSeq`). Keeping it without re-sending renders an optimistic turn the
     host has never heard of, forever.
6. **`notify()`, `dispatch()` and `unsubscribe()` are synchronous.** If
   `dispatch` were `async def`, two coroutines could interleave between
   `clientSeq` allocation and enqueue, putting `clientSeq` 5 on the wire before
   4. TypeScript gets this free from single-threaded JS; asyncio does not.
7. **Omit absent keys; never serialise `null` for them.** `json.dumps` emits
   `null` where `JSON.stringify` drops the key, and `capabilities: {"mcpApps":
   {}}` vs `null` is load-bearing.
8. **A malformed inbound frame logs and continues; a transport error is fatal.**
   Getting the distinction wrong kills unrelated in-flight requests.
9. **Inbound server requests dispatch on their own task, never inline.** A
   handler may re-enter the client and would deadlock the read loop.
10. **`shutdown()` tears down before closing the transport**, so in-flight
    requests raise `ClientClosed`, not `TransportError`.
11. **Never advertise a capability we do not implement.** A gap degrades; a
    false claim fails. `mcpApps` is out of 0.1.0, so it is not sent.
12. **Default the offered version list to the pin's `DEFAULT_SUPPORTED_VERSIONS`,
    not upstream's constant.** Offering a version whose tables are not vendored
    means negotiating a protocol we cannot reduce. Ship the subset test.
13. **The reducer clock is a module global and is safe only because no mutation
    spans an `await`.** That is weaker than "one task" — `apply_optimistic()`
    runs from user tasks at dispatch time — and it is the rule to document.
    `apply()` asserts the running loop's thread; never reduce in a thread pool.

## Surfaces that are specified in prose with no reference implementation

`docs/plan.md` §9 is the list, and it is where the real risk sits, because
nothing exists to copy and no fixture covers any of it: `fetchTurns` returning
`{}` with turns arriving as `chat/turnsLoaded`; stateless `subscribe` returning
no snapshot; the `ahp-otlp://logs{?level}` template that must be expanded before
subscribing; `root/progress` needing a `createSession.progressToken` to exist at
all; opaque `listSessions` cursors that MUST NOT be persisted;
`ChatState.interactivity` gating whether a message may be sent;
`AgentCapabilities` presence flags where `{}` is falsy in Python;
`immutablePrimary` enforced at dispatch rather than in the reducer; `_meta`
preserved verbatim; UTF-16 code-unit offsets. **Read §9 before adding anything
to `api/` or `serve/`.**

## If a feature is not documented, it does not exist

**Complete the documentation update before committing.** Not after, not in a
follow-up. A feature nobody can find is indistinguishable from one that was
never built — and the sibling host shipped that failure repeatedly: a README
listing ten commands while the host answered twenty-nine, and twelve CLI flags
documented nowhere.

A change is not finished until:

- **The README's claims still hold.** Unlike the shared package's README, its
  fenced `python` blocks are **not** executed by `tests/docs/` — they need a
  live host and name types without imports, so they are illustrative and have
  to be re-read by hand when the API moves. The cautionary tale still applies:
  the first draft of the shared package's README dispatched an action that does
  not exist, and the reducer's forward-compatibility fallthrough made it
  silently do nothing. What `tests/docs/` *does* assert are the facts the
  README's claims rest on — the parity counts, the served reverse methods, the
  upstream pin and the offered-version subset — so prose that contradicts them
  contradicts a failing test.
- **`docs/parity.md` regenerates clean**, and any new command, notification or
  reverse method has a row.
- **`docs/plan.md` still describes what is being built.** It is a living
  document. If the code contradicts it, fix one of them in the same PR.
- **The docstring says why, not what.** The code says what it does; the comment
  says why it is not the obvious thing. Every non-obvious line should carry the
  evidence that made it non-obvious — a spec sentence, an offset in the client
  bundle, a wire frame.
- **A claim that can be checked, is.** Prose contradicting itself is not
  catchable. Prose contradicting the command table, the channel list or the
  supported versions is.

## Testing

`docs/plan.md` §11 has the gate table. Two rules about honesty:

- **Driving the sibling Python host is not independent evidence**, and the
  README must say so in those words. Both peers share the reducers and were
  written from the same reading of the same spec; a wrong-but-symmetric reducer
  passes both suites.
- **The parity matrix is self-congratulatory unless it has a second column**
  sourced from the interop suite. A wrapper that exists but has never been
  exercised against a real host shows green otherwise.

`ahp_client.doctor.diagnose()` — a library call, not a CLI; the `ahp
doctor <url>` command is M9 ergonomics that does not exist yet — is the one
mechanism that converts adoption into conformance evidence, and
`ahp_client.testing` ships as **public API** — the fastest way to lose
an adopter is for them to be unable to test their app.

## Requests from an embedder

[`docs/requests.md`](docs/requests.md) is written from the outside in, by an
application building a **long-lived, server-side surface** on this library: one
connection per end user, sessions it did not create, turns it did not start.
Three items block that consumer — watching a turn started elsewhere, answering a
request without being the originator, and a rejected credential retried forever.
Read it before adding to `api/`; it is the clearest statement of where the front
door stops.

Its "deliberately not requested" section is as load-bearing as the requests.

## Conventions

- Conventional commits. `CHANGELOG.md` from the first release.
- Small, reviewable PRs — a human should not need to read the whole repo.
- Every protocol claim is backed by a test.
- Protocol questions go **upstream**, not into a private divergence. A PR that
  changes a wire type, action shape, state field or error code to anything other
  than what the pin says is declined however sensible it is.

[server]: https://github.com/jackhumbert/ahp-py/tree/main/packages/ahp-host
[protocol]: https://github.com/jackhumbert/ahp-py/tree/main/packages/ahp-protocol
[adr2]: https://github.com/jackhumbert/ahp-py/blob/main/packages/ahp-protocol/docs/decisions/0002-extraction.md
[adr8]: docs/decisions/0008-refusing-what-the-host-would-accept.md
