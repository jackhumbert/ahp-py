# Agent guide

For AI agents and humans maintaining this repository. Assume the reader starts
with zero context.

**Read first:** [`docs/research.md`](docs/research.md) (what the protocol
actually does, with evidence), then [`docs/plan.md`](docs/plan.md) (what v0.1
is), then [`docs/roadmap.md`](docs/roadmap.md) (everything after it), then
[`UPSTREAM.md`](UPSTREAM.md) (what revision we target), then
[`docs/release-plan.md`](docs/release-plan.md) (everything between here and
`0.1.0`, in three tiers: what stops an embedder shipping, what "published"
means, and parity with VS Code's own host).
[`docs/requests.md`](docs/requests.md) is the outside-in view: what an embedder
deploying this behind a proxy for several users needed and could not get. The
first five landed — `Host.serve(headers=, token=)`, `core/policies.py`,
`core/audit.py`, `Host.counters()`, and one-host-one-provider decided. Round
two landed as Tier 0 of the release plan — the channel hooks on `Policy`,
`StoredSession.metadata`, the bounded outbox, and the worked liveness example.
[`RELEASING.md`](RELEASING.md) is the procedure that cuts a release — a tag
plus the GitHub release with distributions attached; this family of packages
is public on GitHub and deliberately not on PyPI.

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

## Deferred upstream reports

Measured defects that belong to the spec or to VS Code, held back until these repos are public:
**[`docs/deferred-upstream.md`](docs/deferred-upstream.md)**. Add to it when you find one — the entry is
the report, written while the evidence is in front of you. Two rules: only measured things go in, and if
one turns out to be ours, delete it rather than leaving a wrong accusation lying around.

## What this project is

A Python **host** library for the Agent Host Protocol. AHP is an external
specification owned by Microsoft. We implement it; we do not design it.

Current state: **feature-complete for `0.1.0`; not yet published.** What was
scoped as v0.2 landed before the first release, so it ships in `0.1.0` too.
All **272** reducer fixtures and
all 39 round-trip fixtures pass, the v0.1 command set is implemented, and the
real published Microsoft TypeScript client drives a full turn against the host
over WebSocket in CI. v0.1's build order is
`docs/plan.md` §11; everything after it is scoped in
[`docs/roadmap.md`](docs/roadmap.md), which is currently being worked through
release by release. Decisions are in `docs/decisions/`.

**Before adding a feature, read `docs/roadmap.md` §6.** It lists hard ordering
constraints — things that must exist *before* a feature ships, not alongside it
— and §10 lists what is permanently out of scope for this distribution.

Run the demo host with `python -m agent_host_server`.

## Commands

```bash
uv sync --all-extras          # or: pip install -e '.[dev]'
pytest                        # full suite, offline
pytest tests/conformance      # every published frame, against the spec schemas
ruff check . && ruff format --check .
mypy --strict src
lint-imports                  # enforces the layering rule below
```

The interop tests drive two independently-built clients. The TypeScript one
needs Node; the Python one is an import:

```bash
npm i --no-save @microsoft/agent-host-protocol@0.9.0 ws
pip install -e ../agent-host-client-py
pytest tests/interop
```

**The upstream pin is not this repository's concern any more.** The vendored
corpora, the generated tables, the JS-semantics oracle and the re-vendoring
script all live in
[`agent-host-protocol`](https://github.com/jackhumbert/agent-host-protocol-py),
which this package depends on. Bump the spec there.

It installs from its repository (no index carries it, by design) — or, for
development, from the sibling checkout:

```bash
pip install -e ../agent-host-protocol-py
```

## Layout

| Path | Contents | May import |
|---|---|---|
| `agent_host_protocol` *(dependency)* | wire types, the nine reducers, transports, the vendored corpora | stdlib only |
| `src/agent_host_server/core/` | sequencing, subscriptions, replay, policy, dispatch | the dependency |
| `src/agent_host_server/provider/` | `AgentProvider` protocol + echo provider | the dependency |
| `src/agent_host_server/ws/` | WebSocket implementation | `core`, the dependency |
| `scripts/` | `smoke_wheel.py` (checks an *installed* wheel) | — |

## If a feature is not documented, it does not exist

**Complete the documentation update before committing.** Not after, not in a
follow-up. A feature nobody can find is indistinguishable from one that was
never built, and this repository has shipped several: the README listed ten
commands while the host answered twenty-nine, claimed the terminal, changeset
and resource-watch channels were "not registered and their commands not
implemented" long after all three shipped, and carried "No command answers
`MethodNotFound` any more" directly above "Every one returns a proper JSON-RPC
`MethodNotFound`". Twelve CLI flags existed and were documented nowhere.

Concretely, a change is not finished until:

- **The README's claims still hold.** If you added a command, a flag, or a
  surface, it is listed. If you removed one, it is gone.
- **`docs/guide/` covers anything an embedder must do differently.** Its
  examples are executed by `tests/docs/`, so an example that stops working is
  a failing test, not stale prose.
- **The docstring says why, not what.** The code says what it does; the comment
  says why it is not the obvious thing. Every non-obvious line in this codebase
  should carry the evidence that made it non-obvious — a spec sentence, an
  offset in the client bundle, a wire frame.
- **A claim that can be checked, is.** Prose contradicting itself is not
  catchable. Prose contradicting the dispatcher, the CLI, or the version table
  is — `tests/docs/test_readme_is_true.py` does exactly that, and every check
  in it is derived from the code rather than kept in a list, because a list
  never contains the thing someone just added.

If you find yourself writing "I will document this next", stop and document it.
The follow-up does not happen, and the next person reads the code instead and
believes it.

## Invariants that must not break

Each of these is load-bearing; breaking one produces silent, hard-to-diagnose
failures in *clients*, not in our tests. Evidence for every item is in
`docs/research.md`. The reducer-porting rules among them (2–6, 18–19) travel
with the reducers, which now live in `agent-host-protocol` — that repo's
`AGENTS.md` carries the authoritative copies. They stay listed here because
host code calls the reducers and reviews touch both sides of that line.

1. **The protocol layer performs no I/O and imports nothing from `core/`.**
   `types/` and `reducers/` are no longer directories here — they are the
   `agent-host-protocol` distribution, and the boundary the retired
   `lint-imports` contract used to enforce is enforced by packaging: the
   dependency cannot import its consumer. (`pyproject.toml`'s import-linter
   note records the retirement.)
2. **Reducers are pure except for the injected clock.** Never call
   `time.time()`; take the clock from the module-level provider. The conformance
   harness pins it to `9999`.
3. **Unknown action ⇒ return the input state unchanged.** Never raise. Every
   reducer's fallthrough returns `state`.
4. **Unknown enum values and union variants must not raise, and must
   round-trip verbatim.** Wire values are plain dicts precisely so this is free
   ([ADR 0001](docs/decisions/0001-wire-representation.md)). An earlier draft
   of this invariant described an `Unknown(raw)` arm on every discriminated
   union; no such class ever shipped — ADR 0001's plain-dict decision
   superseded the design it belonged to.
5. **Never use bare truthiness on an optional field.** `[]` is truthy in
   JavaScript and falsy in Python; `if x:` silently changes reducer behaviour.
   Always `if x is not None:`.
6. **`??` is not `or`.** Use the `coalesce()` helper so every site is greppable.
7. **`serverSeq` is assigned in exactly one place**, host-global, inside one
   critical section — and the reducer runs **before** the number is taken. A
   reducer that raises must not consume a `serverSeq`: the number would be
   missing from the log forever and replay above a `Snapshot.fromSeq` could
   never fill the hole. A reducer fault becomes a `rejectionReason`, never an
   exception that escapes.
8. **Subscription registration and snapshot capture are atomic**, and the
   `subscribe`/`initialize` response is queued before any action for that
   channel. `Snapshot.fromSeq` is the protocol's only formal ordering rule.
9. **Stamp `ActionEnvelope.origin` from the connection's `clientId`.**
   `dispatchAction` does not carry one. Getting this wrong breaks optimistic
   reconciliation for every client except the originator.
10. **Per-connection outbound ordering** — one queue, one writer task. Never
    fire-and-forget a send.
11. **Client-dispatch gating is unconditional** and independent of `Policy`.
12. **Rejected client actions are echoed with `rejectionReason`**, never
    silently dropped. Actions on an unknown channel are silently ignored with no
    echo — that asymmetry is specified.
13. **No `Host` without a `Policy`.** No socket-binding convenience function.
14. **A declined feature answers a specific error, never a silent stub and
    never a blanket `MethodNotFound`.** All 32 protocol commands are dispatched
    (`tests/docs/test_readme_is_true.py` parses the dispatcher out of
    `core/host.py` and checks the README's list both ways), so `-32601` is
    reserved for a method
    the protocol does not define. A refusal names its reason:
    `PermissionDenied` for what the host will not do, `NotFound` for what it
    does not have, `ProviderNotFound` for an agent that does not exist. No
    invented error codes. (This invariant used to mandate `MethodNotFound` for
    unimplemented commands and a README list of them; both the refusal style
    and the list are retired.)
15. **Never route on a channel URI's scheme.** Session and chat URIs are
    client-chosen and opaque: VS Code uses `<provider>:/<uuid>` for sessions and
    `ahp-chat://<chatId>/<base64 session uri>` for chats. The reducer is bound
    when the channel is registered. Routing on the scheme applies *no* reducer,
    which freezes host state silently while it keeps broadcasting actions.
16. **`reconnect` is a valid FIRST request.** It re-establishes a dropped
    connection, so there is no prior `initialize` on that transport. VS Code
    opens with it and does not fall back if refused — it retries forever.
17. **A notification handler must not let an exception escape.** There is no
    response to carry the error, and an escaping one ends the read loop,
    dropping a connection over one bad frame from an untrusted peer.
18. **`x === undefined` is not `x is None`.** In JSON an explicit `null` is not
    an absent key, and the reference reducers distinguish them. Port
    `=== undefined` as `"x" not in obj`, never as `is None` — that class of
    mistranslation lets a peer wipe a chat transcript. Where the reference uses
    `??` or truthiness, `is None` *is* correct.
19. **Use `reducers/js.py` for every JavaScript-semantics operation.** It exists
    because the same three mistakes keep recurring, and because the fixture
    corpus cannot see any of them:
    - `js.get` / `js.assign` for reading and writing an optional field.
      An unconditional JS spread (`{...state, x: action.x}`) writes an explicit
      `null` **through** and drops only an absent key. A helper that deletes on
      `None` conflates the two, and the corpus comparator normalises `null` away
      so it passes anyway. An audit found this in four reducers at once.
    - `js.strict_equal` / `js.index_of` for any `===`. Python `==` matches
      `True` against `1`, matches absent against `null`, and compares objects
      structurally where `===` compares them by reference — so an id lookup
      selects a different entry than the reference does, on data a peer controls.
    - `js.key_of` for a `Map`/`Set` key, `js.to_string` for JS `+` coercion.

    New behaviour of this kind needs a case in the protocol package's
    `scripts/js_semantics_cases.py`
    and a regenerated `tests/conformance/fixtures/js-semantics.json`. The oracle
    is the reference reducer itself — never a hand-written expectation, because
    a hand-written one just restates the reading being tested.

## Absorbing a new upstream spec release

Full procedure in [`UPSTREAM.md`](UPSTREAM.md). The step people skip: **diff
`types/channels-*/reducer.ts` between the two tags by hand.** Upstream's own
reducer branch-coverage gate is vacuous, so a new branch can land with no
fixture, and our suite would stay green while diverging.

## Adding a provider adapter

Adapters live in their own distribution (`agent-host-server-<name>`), never in the core —
the core must stay installable and fully testable with no adapter present.

1. Implement `AgentProvider`; add `ResumableAgentProvider` if the runtime can
   resume across host restarts.
2. Emit **neutral provider events** into the `TurnSink` — `TextDelta`,
   `ToolCallStarted`, … — **not** AHP `StateAction`s. The host owns the mapping
   to actions and all sequencing. This keeps adapters alive across spec bumps.
3. Use the `MarkdownTurn` helper so `chat/responsePart` always precedes
   `chat/delta`.
4. Round-trip `ProviderResumeState` as an opaque mapping; the host persists it.
5. Test against the in-memory transport pair — no sockets, no network.

## Conventions

- Conventional commits. `CHANGELOG.md` maintained from the first release.
- Small, reviewable PRs — a human should not need to read the whole repo.
- `docs/research.md` and `docs/plan.md` are **living documents**. If code
  contradicts them, one of the two is wrong; fix it in the same PR, never leave
  them silently inconsistent.
- Every protocol claim is backed by a test. "Conformant" without a conformance
  test is a lie, and this project's whole value is that others can trust it.
- Protocol questions go **upstream**, not into a private divergence. Record them
  in `docs/research.md` §11.
