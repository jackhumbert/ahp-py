# Agent guide

For AI agents and humans maintaining this repository. Assume the reader starts
with zero context.

**Read first:** [`docs/research.md`](docs/research.md) (what the protocol
actually does, with evidence), then [`docs/plan.md`](docs/plan.md) (what we are
building), then [`UPSTREAM.md`](UPSTREAM.md) (what revision we target).

## What this project is

A Python **host** library for the Agent Host Protocol. AHP is an external
specification owned by Microsoft. We implement it; we do not design it.

Current state: **steps 1–6 of the build order done.** All 200 in-scope reducer
fixtures and all 39 round-trip fixtures pass, the v0.1 command set is
implemented, and the real published Microsoft TypeScript client drives a full
turn against the host over WebSocket in CI. Remaining: durable session store,
`fetchTurns` pagination, an ACP provider adapter. Build order is
`docs/plan.md` §11; decisions are in `docs/decisions/`.

Run the demo host with `python -m agent_host_server`.

## Commands

```bash
uv sync --all-extras          # or: pip install -e '.[dev]'
pytest                        # full suite, offline
pytest tests/conformance      # the 247 + 39 upstream fixtures
ruff check . && ruff format --check .
mypy --strict src
lint-imports                  # enforces the layering rule below
```

The interop test needs Node:

```bash
npm i --no-save @microsoft/agent-host-protocol@0.6.0 && pytest tests/interop
```

Re-vendoring upstream (only when bumping the pin — see `UPSTREAM.md`):

```bash
scripts/vendor_upstream.sh
```

## Layout

| Path | Contents | May import |
|---|---|---|
| `src/agent_host_server/types/` | wire types, actions, state, errors | stdlib only |
| `src/agent_host_server/reducers/` | the pure reducers + injectable clock | `types` |
| `src/agent_host_server/conformance/` | fixture runners | `types`, `reducers` |
| `src/agent_host_server/core/` | channels, sequencing, subscriptions, replay, policy | `types`, `reducers` |
| `src/agent_host_server/provider/` | `AgentProvider` protocol + echo provider | `types` |
| `src/agent_host_server/transport/` | transport protocol + in-memory pair | `types` |
| `src/agent_host_server/ws/` | WebSocket implementation | `transport`, `types` |
| `vendor/upstream/` | pinned fixtures, schemas and TS source of truth, **committed** | — |
| `scripts/` | `vendor_upstream.sh` (re-pin), `generate_tables.py` (data tables) | — |

## Invariants that must not break

Each of these is load-bearing; breaking one produces silent, hard-to-diagnose
failures in *clients*, not in our tests. Evidence for every item is in
`docs/research.md`.

1. **`types/` and `reducers/` perform no I/O and import nothing from `core/`.**
   Enforced by `lint-imports`.
2. **Reducers are pure except for the injected clock.** Never call
   `time.time()`; take the clock from the module-level provider. The conformance
   harness pins it to `9999`.
3. **Unknown action ⇒ return the input state unchanged.** Never raise. Every
   reducer's fallthrough returns `state`.
4. **Every discriminated union keeps an `Unknown(raw)` arm that round-trips
   verbatim.** Unknown enum values must not raise either.
5. **Never use bare truthiness on an optional field.** `[]` is truthy in
   JavaScript and falsy in Python; `if x:` silently changes reducer behaviour.
   Always `if x is not None:`.
6. **`??` is not `or`.** Use the `coalesce()` helper so every site is greppable.
7. **`serverSeq` is assigned in exactly one place**, host-global, inside the
   critical section that also applies the reducer, appends to the replay log and
   enqueues fan-out — in that order.
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
14. **Unimplemented commands return `MethodNotFound` (`-32601`)** and appear in
    the README's unimplemented list. No silent stubs, no invented error codes.

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
