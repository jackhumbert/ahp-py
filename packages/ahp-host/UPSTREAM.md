# Upstream pin

This project implements an external specification, but it no longer vendors it.
Everything protocol-shaped — types, reducers, error codes, conformance fixtures
— lives in [`agent-host-protocol`](https://github.com/jackhumbert/agent-host-protocol-py),
which this package depends on, and **that repository's `UPSTREAM.md` is the
authority on the pin**. Bumping the spec is no longer a change to this
repository.

What stays here is the consequence: which protocol versions this host offers on
the wire, and which client it is tested against. The table below is asserted by
`tests/docs/test_readme_is_true.py`, so it fails when the dependency moves under
it rather than going quietly stale.

**The spec moves weekly and lands breaking changes in MINOR bumps**, which is
why the dependency is pinned with `~=` rather than `>=`: a spec break must not
be able to arrive as a patch upgrade.

## Current pin

| Field | Value |
|---|---|
| Upstream | [`microsoft/agent-host-protocol`](https://github.com/microsoft/agent-host-protocol) (MIT) |
| Spec tag | `spec/v0.8.0` |
| Spec commit | `7153143f1c6993fa886d7d59870811cdad479d83` |
| Working revision read during research | `bd27d354b39c1b2090fbcc6db392d406b743280c` (2026-07-31) |
| **Protocol version negotiated on the wire** | **`0.8.0`** (we offer `0.8.0, 0.7.0, 0.6.0`) |
| Conformance corpus | with the dependency, vendored there from `spec/v0.8.0` → `types/test-cases/` |
| Reference client for interop tests | `@microsoft/agent-host-protocol@0.8.0` (npm, published 2026-08-18) |

### The interop client matches the wire version

Earlier revisions of this section recorded the interop client trailing the
preferred wire version by a MINOR, until npm caught up with `spec/v0.7.0`. The
move to `spec/v0.8.0` kept them together from the start: this host
prefers **`0.8.0`**, npm published `@microsoft/agent-host-protocol@0.8.0` from that
same tag on 2026-08-18, and `tests/interop/` drives it and negotiates `0.8.0`.
`0.7.0` and `0.6.0` stay in the offered list — upstream still declares both
supported — and the negotiation tests still cover the downgrade path.

A peer that negotiates an older version still gets 0.8.0's reducers, and one
0.8.0 change is breaking on the wire: `session/customizationToggled` carries
`enablement` where it carried `enabled`. This host **rejects** an old-shape
toggle rather than letting it reduce to a silent no-op, so a pre-0.8.0 client
reverts its optimistic state instead of showing a toggle that never happened.

## Vendoring moved out

Nothing is vendored here — this repository has no `vendor/` directory. The
conformance corpora, the five schemas and the TypeScript codegen inputs are
vendored once, in `agent-host-protocol`, and arrive as package data with the
dependency (`agent_host_protocol.conformance.corpus.CORPUS_ROOT`). The
wire-schema gate in `tests/conformance/schemas.py` reads the dependency's copy
for exactly that reason: a second copy here could pin a different tag, and the
gate would then assert against a spec the reducers do not implement.

The vendored-file inventory, the pin-bump procedure and the ledger of known
upstream defects live in
[`agent-host-protocol`'s `UPSTREAM.md`](https://github.com/jackhumbert/agent-host-protocol-py/blob/main/UPSTREAM.md)
— this file used to carry its own copies, and they went stale the first time
the sibling's moved. One defect workaround is this host's own to keep: the
TypeScript `AhpStateMirror` drops all `ahp-chat:` snapshots and actions, so
the interop tests assert on the raw action stream, never on the mirror.

What a pin bump asks of this repository is a decision, not a re-vendor:
whether the **offered wire versions** move. A version is offered only when the
dependency's vendored tables cover it, and the preferred version should have a
real, installable client to negotiate with. Record the outcome in the table
above.

## Upstream questions not yet filed

`docs/research.md` §11 lists open questions that are candidates for upstream
issues. None have been filed. Filing is a deliberate, separate decision — this
project does **not** fork or privately diverge from the spec; where something is
underspecified, the question goes upstream and the answer comes back here.
