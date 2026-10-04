# Upstream pin

This package implements an external specification. Everything protocol-shaped
here — types, reducers, error codes, conformance fixtures — is derived from a
single pinned upstream revision. **The spec moves weekly and lands breaking
changes in MINOR bumps, so an unpinned implementation is unmaintainable.**

This repository owns the pin for the whole Python AHP ecosystem. A peer that
depends on `ahp-protocol` does not vendor anything, does not run
`vendor_upstream.sh`, and does not maintain its own copy of this document — it
pins a version of this distribution and asserts
`ahp_protocol.UPSTREAM_PROTOCOL_VERSION` in its own `tests/docs/`
suite, so a dependency bump that moves the spec under it fails loudly.

## Current pin

| Field | Value |
|---|---|
| Upstream | [`microsoft/agent-host-protocol`](https://github.com/microsoft/agent-host-protocol) (MIT) |
| Spec tag | `spec/v1.0.0` |
| Spec commit | `5f16d81bb7045b66d7bc768d244feab75002d943` |
| Conformance corpus vendored from | `spec/v1.0.0` → `types/test-cases/` |
| `UPSTREAM_SUPPORTED_PROTOCOL_VERSIONS` | `1.0.0, 0.9.0` — what *upstream* declares |
| `DEFAULT_SUPPORTED_VERSIONS` | `1.0.0, 0.9.0, 0.8.0, 0.7.0, 0.6.0` — what a peer built on this pin can honestly speak |

**Those last two rows are different numbers and must stay different.** Upstream's
constant is a fact about upstream; ours is a claim about what this package's
vendored action and state tables actually cover. Offering a version whose tables
you have not vendored means negotiating a protocol you cannot reduce — you pass
your own compatibility check and then apply the wrong reducer branches. Any peer
defaulting its offered list to the upstream constant has this bug; a test should
assert the offered list is a subset of what the pin covers.

Since 1.0.0 upstream calls its list "released compatibility baselines,
independent of the current development `PROTOCOL_VERSION`", and it dropped
`0.8.0` through `0.5.1` from it. Ours is wider by `0.8.0, 0.7.0, 0.6.0` for one
release (step 8 below); the next pin bump drops them.

For the record, as of **2026-10-04**: `spec/v1.0.0` (2026-10-02) is the newest
spec tag, and upstream `main` is 5 commits past it.

The `spec/v0.9.0` → `spec/v1.0.0` absorb, reducer by reducer (every
`reducer.ts` diff read by hand). Every change is **additive** — no existing
branch changed behaviour, and all 81 pre-existing JS-semantics cases reproduce
unchanged against the 1.0.0 reducers:

- **new: `canvas`** (experimental) — one live canvas on an `ahp-canvas:`
  channel; `canvas/stateChanged` replaces the whole state. Ten reducers now.
- **`chat`** — `chat/backgroundWorkSet` / `chat/backgroundWorkRemoved` (upsert
  and remove by `id`), `chat/movableChanged`, `chat/changesetsChanged` and
  `chat/canvasesChanged` (destructure and re-add on truthiness, like
  `session/changesetsChanged`), and `chat/isReadChanged` /
  `chat/isArchivedChanged`, which flip the same status bits a session's do.
- **`session`** — `session/chatsReordered`, an authoritative full order that is
  a no-op unless it is an exact permutation of the current catalogue, and the
  client-dispatchable `session/mcpServerBackgroundRequested`, which clears
  `blocking` on a `starting` MCP server.
- **negotiation** — normative now, with its own corpus
  (`types/test-cases/version-negotiation.json`, 22 cases): the highest offered
  version inside the caret range of any baseline, returned verbatim, and a
  malformed offer is an error rather than skipped. `versions.py` reversed its
  old "offered may not exceed ours" rule to match.
- **commands** — one new command, `moveChat` (`commands-chat.ts`): an atomic
  transfer of a chat to another session under a stable URI. Nothing here
  dispatches commands, so it reaches only the peers' parity gates.

The `spec/v0.8.0` → `spec/v0.9.0` absorb added the `automation` and
`automationRun` reducers, moved a turn's error into an `ErrorResponsePart`,
made every reducer clock-free, gave `terminal/exited` a `lifecycle`, renamed
the session's `creationFailed` lifecycle to `failed`, and retired
`ContentNotFound` (`-32006` stays reserved). The `spec/v0.7.0` → `spec/v0.8.0`
absorb changed exactly one reducer, `channels-session/reducer.ts`.

npm carries `@microsoft/agent-host-protocol@1.0.0`, built from the same tag
this pin vendors.

## What is vendored, and from where

Written by `scripts/vendor_upstream.sh` into `vendor/upstream/`, committed, and
mapped into the wheel at `ahp_protocol/conformance/_upstream/` by hatch's
`force-include`. There is exactly one copy under version control.

| Vendored | Source | Why not fetched at runtime |
|---|---|---|
| `types/test-cases/reducers/**` (308 fixtures) | git tag only | not published as a release asset |
| `types/test-cases/round-trips/**` (67 fixtures)
| `types/test-cases/version-negotiation.json` (22 cases) | git tag only | same | | git tag only | same |
| `schema/*.schema.json` (5 files) | release asset **or** git tag | asset exists, but must match the pinned tag |
| `ts/` — `registry.ts`, `action-origin.generated.ts`, `actions.ts`, `errors.ts`, `session-state.ts` | git tag only | the codegen inputs for `_generated.py` |
| `ts/` — `messages.ts`, `commands.ts`, and `commands-{root,session,chat,terminal,changeset,resource-watch,automation}.ts` | git tag only | the command/notification maps and per-channel params the peers' parity and scoping gates read |

The conformance corpora exist **only in the git repository**, never as release
assets. Vendored files are committed and never fetched during a test run; the
suite must pass offline.

## Procedure for bumping the pin

1. Read the upstream `CHANGELOG.md` between the old and new tag. Enumerate every
   entry under **Removed** and **Changed** — the changelog states outright that
   breaking changes land in MINOR bumps.
2. Update the table above, in the same commit as the vendored files.
3. Run `scripts/vendor_upstream.sh` (it hard-fails if the tag has moved off the
   pinned commit).
4. Run `python scripts/generate_tables.py`; CI re-runs it and `git diff
   --exit-code`s, so a stale `_generated.py` is a failing test.
5. Run the conformance suite. New fixtures that fail identify the reducer
   branches to port.
6. **Diff `types/channels-*/reducer.ts` between the two tags by hand.** Do not
   rely on new fixtures existing for new branches: upstream's own
   `--branches 100` reducer coverage gate has been vacuous since commit
   `ad3f9b96`, because it still globs `types/reducers.ts`, which is now a
   re-export shim with zero branches.
7. Re-run `scripts/regenerate_js_semantics.sh` (needs Node and a checkout of
   upstream) so the JS-semantics oracle reflects the new reducers.
8. Decide separately whether `DEFAULT_SUPPORTED_VERSIONS` moves. It widens only
   when the vendored tables cover the added version, and narrows one release
   after upstream drops a version from its own supported set.
9. Record the bump in `CHANGELOG.md` under a `Changed` entry naming both the old
   and new spec version, and bump this distribution's MINOR.

Because both peers pin `ahp-protocol ~= 0.1.0`, a MINOR bump here is a
deliberate, visible upgrade on each of them rather than something that arrives
silently.

## Known upstream defects this pin works around

Tracked so they can be removed when fixed upstream.

| Defect | Effect here |
|---|---|
| Published `spec/v0.6.0` schemas mark `T \| undefined` properties as `required` (`ActionEnvelope.origin`, `Turn.usage`, `ActiveTurn.usage`, …). Fixed at 0.7.0. | Do not generate types from the 0.6.0 schemas. |
| `errors.schema.json` omits `-32011 Conflict` — the generator hardcodes the enum. | Error codes are taken from `types/common/errors.ts`, not the schema. `errors.from_json` accepts any integer code for the same reason. |
| `actions.schema.json` ships a malformed `{"$ref": "#/$defs/"}` as `StateAction.oneOf[0]` — present in **every** tag from `spec/v0.5.0` to `v1.0.0`. Causes `RecursionError` in `datamodel-code-generator` and `PointerToNowhere` in Python's `jsonschema`. | Validation is **per-definition** (`conformance/schemas.py` resolves one `#/$defs/<name>` at a time), which never touches the broken ref — but `validate_against('actions', 'StateAction', …)` or `'ActionEnvelope'` still hits `PointerToNowhere`, so those two definitions stay unvalidatable. No generation from schema. `tests/unit/test_generated_tables.py` pins the defect so a fix upstream is noticed. |
| `SessionStatus` is a bitset emitted as a closed `enum: [1,2,8,24,32,64]`, which rejects `33, 40, 56, 65, 72, 2147483720` — values present in upstream's own conformance corpora. | Same. Pinned by a test. |
| `chatReducer` read the wall clock in six places, so reducers were not pure. **Fixed at 0.9.0.** | The injectable clock stays for peers stamping `startedAt`; the harness still pins it to `9999`, which no 0.9.0 reducer reads. |
| No unknown-action fixture exists for the `chat` reducer. | Covered by a local unit test instead. |
| `ACTION_INTRODUCED_IN` (1.0.0) dates every new chat and session action `0.9.0`, though none is in the `spec/v0.9.0` tag, and dates `chat/canvasesChanged` and `canvas/stateChanged` `0.10.0`, a version that was never released. | Copied verbatim into `_generated.py`. Do not gate sending one of these on the negotiated version: a `0.9.0` peer would be told it has actions its reducers lack. Its reducer ignores them (invariant 3), so sending is harmless; withholding on these dates would be wrong. |
| `canvasReducer` returns `undefined` as the next state for a `canvas/stateChanged` without `canvas`. | We return the state unchanged instead (invariant 3). |
| The reducer corpus's comparator drops `null`-valued keys on both sides, so it cannot express absent-vs-explicit-`null`. | A second corpus (`tests/conformance/fixtures/js-semantics.json`) is generated by running adversarial cases through the real pinned TypeScript reducers and frozen verbatim. Compared byte-for-byte. |
| JS signed-int32 bitwise on `SessionStatus` diverges from the u32 used by every runtime client. | We mask to u32, matching four of five clients. |

## Relationship to the spec

This project targets an external specification. Protocol changes come from
upstream, not from contributors' preferences. A PR that changes a wire type, an
action shape, a state field or an error code to anything other than what the
pinned upstream says is declined however sensible the change is. Where something
is underspecified, the question goes upstream — this project does not fork the
spec or diverge privately.
