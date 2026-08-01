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
| **VS Code** 1.132.0 | `['0.7.0','0.6.0','0.5.2','0.5.1']` | vendors upstream `types/` at `.ahp-version` = `8e0a9bbf`; the **full** list |
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

- 0.7.0 is what VS Code speaks and prefers, and is the newest released spec.
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

## Not implemented

`_vscodeUpgrade` — VS Code reads `_meta.vscodeUpgradeMethod` off an
`UnsupportedProtocolVersion` error to offer a one-click server upgrade. It is for
hosts spawned by the VS Code CLI; upstream states servers without a managing CLI
omit it. A well-formed `-32005` still renders a proper incompatibility message.
