# ADR 0002 — Speak protocol 0.7.0 and 0.6.0, holding a set of versions

**Status:** accepted · **Date:** 2026-08-01
**Supersedes:** an earlier draft that recommended 0.6.0 alone.

## Context

The spec ships breaking changes in MINOR bumps — five releases in eight weeks —
and pre-1.0 compatibility is **per-MINOR**, so supporting another version means
implementing it, not widening a range.

Measured on 2026-08-01:

| Client | Offers | Note |
|---|---|---|
| **VS Code** 1.131.0 | **`['0.7.0']`** at runtime | vendors upstream `types/` at `.ahp-version` = `8e0a9bbf`. Its source *declares* `['0.7.0','0.6.0','0.5.2','0.5.1']`, but the client sends `[PROTOCOL_VERSION]` alone — measured, `experiments.md` E12c |
| npm `@microsoft/agent-host-protocol` | `0.6.0` latest published; `MultiHostClient` offers `[PROTOCOL_VERSION]` only | no `typescript/v0.7.0` release exists |
| `ahpx` | a single version, lockfile-pinned to `0.5.0` | `0.5.0` is no longer in upstream's own supported list |

VS Code is the primary target client, and connecting it to a third-party host is
a supported, extension-free feature (`chat.remoteAgentHostsEnabled`,
`chat.remoteAgentHosts`).

## Decision

**Support `{0.7.0, 0.6.0}`, preferring 0.7.0.** Hold a *set* of supported
versions rather than a single `current`.

Negotiation: pick the highest offered version present in our set; if the
intersection is empty, return `UnsupportedProtocolVersion` (`-32005`) with
`UnsupportedProtocolVersionErrorData` and close.

## Rationale

- 0.7.0 is what VS Code speaks — and since it offers *only* that, supporting it
  is **required**, not preferred. A 0.6.0-only host is refused outright. (This
  decision was taken before that was measured; the original reasoning was
  weaker than the fact now available.)
- 0.6.0 keeps the installable npm client usable as the CI interop counterparty.
- **The cost is close to zero.** The entire 0.6.0 → 0.7.0 action delta for
  root/session/chat is two step-up-auth actions and four multiroot actions — all
  six outside v0.1 scope. Everything from 0.4.0 up shares one state model.
- 0.8.0 is unreleased and nothing speaks it.
- 0.5.x is excluded: it buys only `ahpx`, which offers a single already-dropped
  version, and it predates input requests moving into turn `responseParts`, so
  it is a second elicitation state model.

## Holding a set is a deliberate extension

The reference host's algorithm
(`vscode/.../protocol/version/negotiation.ts`) models exactly **one** MINOR:

```
isCompatibleProtocolVersion(offered, current):
  majors must match
  if major == 0, minors must also match      # every 0.x minor bump is breaking
  offered must not be greater than current
```

A host with `current = '0.7.0'` there would *reject* an offered `0.6.0`. We keep
those within-MINOR semantics but evaluate them against each member of our
supported set. This is a superset of the reference behaviour, not a divergence
from the spec — `versioning.md` requires only that the host pick one offered
version it can speak, or refuse.

## Enforcement

- **Negotiation correctness is entirely ours.** The client does not verify the
  answer: a probe accepted a `protocolVersion` it never offered
  (`docs/experiments.md` E3).
- **An outbound action filter** keyed on the negotiated version, driven by
  `registry-snapshot.json`'s `actionIntroducedIn`, implements the spec's rule
  that a host only sends actions known to the negotiated version. Data, not a
  hand-maintained table. For v0.1 it has nothing to suppress.
- **The real per-version cost is state shapes and command params**, which
  `actionIntroducedIn` does not cover. Any future floor change requires auditing
  those by hand.
- **"Well-formed `-32005`" was not, for this host's whole life.** The `data`
  payload carried `supportedProtocolVersions`; `UnsupportedProtocolVersionErrorData`
  declares exactly one required key and it is `supportedVersions`
  (`errors.schema.json:65,74`, `vendor/upstream/ts/errors.ts:157`). Both peers in
  the Python interop run used the same wrong name, agreed with each other, and
  would have dropped the list against anything conformant — which is the failure
  mode this whole file is about, since a client that cannot read the list can
  only say "the handshake failed". Built in `core/host.py` rather than taken
  from `ahp_protocol.errors`, whose emitter still writes the old name;
  pinned by `tests/conformance/test_error_data_schema.py`, which validates the
  payload against the vendored schema rather than against our own spelling.
- **`protocolVersions` element types are checked before negotiation.** Every
  entry reaches `re.match`, so one integer in a peer-controlled array answered
  `-32603` with a raw Python `TypeError` — the host blaming itself for a
  schema-invalid request, and naming a Python type while doing it.

## Not implemented

`_vscodeUpgrade` — VS Code reads `_meta.vscodeUpgradeMethod` off an
`UnsupportedProtocolVersion` error to offer a one-click server upgrade. It is for
hosts spawned by the VS Code CLI; upstream states servers without a managing CLI
omit it. A well-formed `-32005` still renders a proper incompatibility message.

## Measured, later

Two facts found while looking for a second client to test against, both of
which bear on whether `{0.7.0, 0.6.0}` is the right set.

**The reference client speaks five versions**, not two --
`SUPPORTED_PROTOCOL_VERSIONS` in upstream's `types/version/registry.ts` is
`['0.8.0', '0.7.0', '0.6.0', '0.5.2', '0.5.1']`. A client built from current
sources therefore negotiates with us happily; the floor only bites clients
pinned to an older package.

**One is.** `ahpx`, the only third-party AHP client found in the wild, depends
on `@microsoft/agent-host-protocol@^0.5.0`, which under semver's 0-major rule
locks it to `0.5.x`. Against this host it is refused with `-32005`.

That is *not* a reason to widen the floor. `versioning.md:24` is explicit that
two pre-1.0 MINORs are not compatible, so adding `0.5.x` would be a claim that
these reducers are correct for a MINOR whose semantics nothing here has
verified -- bought to make one test pass. Supporting `0.6.0` alongside `0.7.0`
is already such a claim, and it is defensible only because the 0.7.0 additions
are additive: a 0.6.0 client never sends the working-directory actions or a
refined tool-call contributor, so those reducer arms never fire for it. Nobody
has established the same for `0.5.x`.

**The earlier checkpoint asked whether `0.6.0` could be dropped.** The answer is
no, and for a reason the checkpoint did not anticipate: real clients lag the
spec by a full MINOR or more, and a host that speaks only the newest one is a
host most clients cannot reach. If anything the pressure is the other way --
but any widening has to come with evidence that the reducers hold for that
MINOR, not with a version string.

**Upstream is developing `0.8.0`** (`PROTOCOL_VERSION` is bumped in main). Our
pin stays at `spec/v0.7.0` until absorbed deliberately, per `UPSTREAM.md`.
