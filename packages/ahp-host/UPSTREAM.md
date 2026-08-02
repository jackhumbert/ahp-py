# Upstream pin

This project implements an external specification. Everything protocol-shaped
here — types, reducers, error codes, conformance fixtures — is derived from a
single pinned upstream revision. **The spec moves weekly and lands breaking
changes in MINOR bumps, so an unpinned implementation is unmaintainable.**

## Current pin

| Field | Value |
|---|---|
| Upstream | [`microsoft/agent-host-protocol`](https://github.com/microsoft/agent-host-protocol) (MIT) |
| Spec tag | `spec/v0.7.0` |
| Spec commit | `ea6fae670c4012721fdc02d587b3a46ecdc871c0` |
| Working revision read during research | `bd27d354b39c1b2090fbcc6db392d406b743280c` (2026-07-31) |
| **Protocol version negotiated on the wire** | **`0.7.0`** (we offer `0.7.0, 0.6.0`) |
| Conformance corpus vendored from | `spec/v0.7.0` → `types/test-cases/` |
| Reference client for interop tests | `@microsoft/agent-host-protocol@0.6.0` (npm) |

### Why the wire version and the corpus tag differ

They answer different questions.

- **`0.6.0` on the wire** is what a client you can actually install negotiates.
  The npm package's newest published version is `0.6.0`; there is no
  `typescript/v0.7.0` release. Targeting a version with no client would make the
  host untestable against anything real. See `docs/research.md` §1.
- **`spec/v0.7.0` for the corpus** because the fixtures are versioned with the
  spec, and 0.7.0 fixed a schema bug that made the 0.6.0 artifacts reject valid
  traffic. Fixtures covering post-0.6.0 actions are skipped by action name, not
  by editing the corpus.

Both numbers move independently. Record every change here.

## What is vendored, and from where

| Vendored | Source | Why not fetched at runtime |
|---|---|---|
| `types/test-cases/reducers/**` (247 fixtures) | git tag only | not published as a release asset |
| `types/test-cases/round-trips/**` (39 fixtures) | git tag only | same |
| `schema/*.schema.json` (5 files) | release asset **or** git tag | asset exists, but must match the pinned tag |
| `registry-snapshot.json` | release asset | machine-readable action→version map |

Only the schemas and `registry-snapshot.json` ship as release assets on
`spec/vX.Y.Z`. The conformance corpora exist **only in the git repository**, so
they are vendored from the tag:

```bash
git archive --remote=https://github.com/microsoft/agent-host-protocol.git \
  spec/v0.7.0 types/test-cases | tar -x
```

Vendored files are committed, never fetched during a test run. Tests must pass
offline.

## Procedure for bumping the pin

1. Read the upstream `CHANGELOG.md` between the old and new tag. Enumerate every
   entry under **Removed** and **Changed** — the changelog states outright that
   breaking changes land in MINOR bumps.
2. Update the table above, in the same commit as the vendored files.
3. Re-vendor `types/test-cases/**`, the five schemas, and `registry-snapshot.json`.
4. Run the conformance suite. New fixtures that fail identify the reducer
   branches to port.
5. **Diff `types/channels-*/reducer.ts` between the two tags by hand.** Do not
   rely on new fixtures existing for new branches: upstream's own
   `--branches 100` reducer coverage gate has been vacuous since commit
   `ad3f9b96`, because it still globs `types/reducers.ts`, which is now a
   re-export shim with zero branches.
6. Re-derive the generated data tables (`IS_CLIENT_DISPATCHABLE`, the action
   version registry) from `registry-snapshot.json` and
   `types/action-origin.generated.ts`.
7. Decide separately whether the **wire** version moves. It should move only
   when a real client that negotiates it is installable.
8. Record the bump in `CHANGELOG.md` under a `Changed` entry naming both the old
   and new spec version.

## Known upstream defects this pin works around

Tracked so they can be removed when fixed upstream. Full detail and evidence in
`docs/research.md`.

| Defect | Effect here |
|---|---|
| Published `spec/v0.6.0` schemas mark `T \| undefined` properties as `required` (`ActionEnvelope.origin`, `Turn.usage`, `ActiveTurn.usage`, …). Fixed at 0.7.0. | Do not generate types from the 0.6.0 schemas. |
| `errors.schema.json` omits `-32011 Conflict` — the generator hardcodes the enum. | Error codes are taken from `types/common/errors.ts`, not the schema. |
| `actions.schema.json` ships a malformed `{"$ref": "#/$defs/"}` as `StateAction.oneOf[0]` — present in **every** tag from `spec/v0.5.0` to `v0.7.0`. Causes `RecursionError` in `datamodel-code-generator` and `PointerToNowhere` in Python's `jsonschema`. | No schema-validation gate; no generation from schema. Re-check on every pin bump. |
| `SessionStatus` is a bitset emitted as a closed `enum: [1,2,8,24,32,64]`, which rejects `33, 40, 56, 65, 72, 2147483720` — values present in upstream's own conformance corpora. | Same. |
| `chatReducer` reads the wall clock in six places, so reducers are not pure. | Reducers take an injectable clock; the conformance harness pins it to `9999`. |
| No unknown-action fixture exists for the `chat` reducer. | Covered by a local unit test instead. |
| `AhpStateMirror` drops all `ahp-chat:` snapshots and actions. | Interop tests assert on the raw action stream, not the mirror. |
| JS signed-int32 bitwise on `SessionStatus` diverges from the u32 used by every runtime client. | We mask to u32, matching four of five clients. |

## Upstream questions not yet filed

`docs/research.md` §11 lists open questions that are candidates for upstream
issues. None have been filed. Filing is a deliberate, separate decision — this
project does **not** fork or privately diverge from the spec; where something is
underspecified, the question goes upstream and the answer comes back here.
