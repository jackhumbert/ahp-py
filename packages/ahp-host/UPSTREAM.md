# Upstream pin

This project implements an external specification, but it no longer vendors it.
Everything protocol-shaped — types, reducers, error codes, conformance fixtures
— lives in [`ahp-protocol`](https://github.com/jackhumbert/ahp-py/tree/main/packages/ahp-protocol),
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
| Spec tag | `spec/v1.0.0` |
| Spec commit | `5f16d81bb7045b66d7bc768d244feab75002d943` |
| Working revision read during research | `bd27d354b39c1b2090fbcc6db392d406b743280c` (2026-07-31) |
| **Protocol version negotiated on the wire** | **`1.0.0`** (we offer `1.0.0, 0.9.0, 0.8.0, 0.7.0, 0.6.0`) |
| Conformance corpus | with the dependency, vendored there from `spec/v1.0.0` → `types/test-cases/` |
| Reference client for interop tests | `@microsoft/agent-host-protocol@1.0.0` (npm, published 2026-10-02) |

### The interop client matches the wire version

Earlier revisions of this section recorded the interop client trailing the
preferred wire version by a MINOR, until npm caught up with `spec/v0.7.0`.
Since then they have moved together: this host prefers **`1.0.0`**, npm
published `@microsoft/agent-host-protocol@1.0.0` from that same tag on
2026-10-02, and `tests/interop/` drives it and negotiates `1.0.0`. Upstream
1.0.0 advertises only `1.0.0` and `0.9.0`; `0.8.0`, `0.7.0` and `0.6.0` stay in
the offered list for one more release and the negotiation tests still cover the
downgrade path.

Negotiation follows the spec's caret rule since 1.0.0: the highest offered
version inside `^1.0.0` or `^0.9.0` (or one of the older baselines) wins and
is returned verbatim, so a `1.4.0` client is accepted and told `1.4.0`. A
malformed entry anywhere in `protocolVersions` is answered `-32602`.

A peer that negotiates an older version still gets the pinned reducers, and
some shape changes are breaking on the wire. This host deals with the ones a
client *sends*:

- `session/customizationToggled` carries `enablement` where it carried
  `enabled` (0.8.0). An old-shape toggle is **rejected** rather than reduced to
  a silent no-op, so the client reverts instead of showing a toggle that never
  happened.
- `createSession` lost session-level `fork` in 0.9.0. The host still honours
  it, for the 0.7.0 and 0.8.0 peers — VS Code's `/fork` among them.
- `chat/turnResume` (0.9.0) is rejected: the host never marks an error part
  `resumable`, so there is nothing to reopen.

Everything 1.0.0 added is additive. Where each host-side feature stands:

| 1.0.0 feature | Status |
|---|---|
| `SessionSummary.chats` / `defaultChat` | Projected from `SessionState.chats`, in catalogue order |
| `chat/isReadChanged`, `chat/isArchivedChanged` | Accepted; mirrored into `ChatSummary.status` and the compact catalogue |
| `ChangesetStatus.Recomputing` | A changeset refresh goes `recomputing` → `ready` |
| `ConfigPropertySchema.minItems` / `maxItems` | Enforced, with `items`, on every config value a client sets |
| `AuthenticateParams.expiresIn` | The token is dropped at expiry and `auth/required` goes out with reason `expired`; an empty token revokes |
| `McpServerStartingState.blocking`, `session/mcpServerBackgroundRequested` | Routed to `BackgroundsMcpServers`; a provider that refuses, or lacks it, gets `blocking: true` reasserted |
| `moveChat`, `ChatState.movable` | Declined with `PermissionDenied`: no chat is movable yet |
| Chat background work, per-chat `changes` and changesets, canvases, automation `disableConditions` and `customizations`, historical terminal results | Not yet |

What the host *publishes* is 0.9.0-shaped for everyone: errors as response
parts, terminal `lifecycle`, session claims with `chat`, and a `failed`
session lifecycle.

## Vendoring moved out

Nothing is vendored here — this repository has no `vendor/` directory. The
conformance corpora, the five schemas and the TypeScript codegen inputs are
vendored once, in `ahp-protocol`, and arrive as package data with the
dependency (`ahp_protocol.conformance.corpus.CORPUS_ROOT`). The
wire-schema gate in `tests/conformance/schemas.py` reads the dependency's copy
for exactly that reason: a second copy here could pin a different tag, and the
gate would then assert against a spec the reducers do not implement.

The vendored-file inventory, the pin-bump procedure and the ledger of known
upstream defects live in
[`ahp-protocol`'s `UPSTREAM.md`](https://github.com/jackhumbert/ahp-py/blob/main/packages/ahp-protocol/UPSTREAM.md)
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
