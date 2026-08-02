# Upstream pin

This package implements an external specification. Everything protocol-shaped
here — types, reducers, error codes, conformance fixtures — is derived from a
single pinned upstream revision. **The spec moves weekly and lands breaking
changes in MINOR bumps, so an unpinned implementation is unmaintainable.**

This repository owns the pin for the whole Python AHP ecosystem. A peer that
depends on `agent-host-protocol` does not vendor anything, does not run
`vendor_upstream.sh`, and does not maintain its own copy of this document — it
pins a version of this distribution and asserts
`agent_host_protocol.UPSTREAM_PROTOCOL_VERSION` in its own README test, so a
dependency bump that moves the spec under it fails loudly.

## Current pin

| Field | Value |
|---|---|
| Upstream | [`microsoft/agent-host-protocol`](https://github.com/microsoft/agent-host-protocol) (MIT) |
| Spec tag | `spec/v0.7.0` |
| Spec commit | `ea6fae670c4012721fdc02d587b3a46ecdc871c0` |
| Conformance corpus vendored from | `spec/v0.7.0` → `types/test-cases/` |
| `UPSTREAM_SUPPORTED_PROTOCOL_VERSIONS` | `0.7.0, 0.6.0, 0.5.2, 0.5.1` — what *upstream* declares |
| `DEFAULT_SUPPORTED_VERSIONS` | `0.7.0, 0.6.0` — what a peer built on this pin can honestly speak |

**Those last two rows are different numbers and must stay different.** Upstream's
constant is a fact about upstream; ours is a claim about what this package's
vendored action and state tables actually cover. Offering a version whose tables
you have not vendored means negotiating a protocol you cannot reduce — you pass
your own compatibility check and then apply the wrong reducer branches. Any peer
defaulting its offered list to the upstream constant has this bug; a test should
assert the offered list is a subset of what the pin covers.

For the record, and re-verified at this pin: there is no `spec/v0.8.0` tag
upstream. `0.8.0` is unreleased `main`. The `spec/v0.7.0`→`HEAD` diff of
`types/` is three files — a `ResourceReponsePart`→`ResourceResponsePart`
spelling fix, an `index.ts` re-export refactor, and the registry version bump —
and the action tables are identical at both points: 85 types, 38 dispatchable.

## What is vendored, and from where

Written by `scripts/vendor_upstream.sh` into `vendor/upstream/`, committed, and
mapped into the wheel at `agent_host_protocol/conformance/_upstream/` by hatch's
`force-include`. There is exactly one copy under version control.

| Vendored | Source | Why not fetched at runtime |
|---|---|---|
| `types/test-cases/reducers/**` (247 fixtures) | git tag only | not published as a release asset |
| `types/test-cases/round-trips/**` (39 fixtures) | git tag only | same |
| `schema/*.schema.json` (5 files) | release asset **or** git tag | asset exists, but must match the pinned tag |
| `ts/` — `registry.ts`, `action-origin.generated.ts`, `actions.ts`, `errors.ts`, `session-state.ts` | git tag only | the codegen inputs for `_generated.py` |

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

Because both peers pin `agent-host-protocol ~= 0.1.0`, a MINOR bump here is a
deliberate, visible upgrade on each of them rather than something that arrives
silently.

## Known upstream defects this pin works around

Tracked so they can be removed when fixed upstream.

| Defect | Effect here |
|---|---|
| Published `spec/v0.6.0` schemas mark `T \| undefined` properties as `required` (`ActionEnvelope.origin`, `Turn.usage`, `ActiveTurn.usage`, …). Fixed at 0.7.0. | Do not generate types from the 0.6.0 schemas. |
| `errors.schema.json` omits `-32011 Conflict` — the generator hardcodes the enum. | Error codes are taken from `types/common/errors.ts`, not the schema. `errors.from_json` accepts any integer code for the same reason. |
| `actions.schema.json` ships a malformed `{"$ref": "#/$defs/"}` as `StateAction.oneOf[0]` — present in **every** tag from `spec/v0.5.0` to `v0.7.0`. Causes `RecursionError` in `datamodel-code-generator` and `PointerToNowhere` in Python's `jsonschema`. | No schema-validation gate; no generation from schema. `tests/unit/test_generated_tables.py` pins the defect so a fix upstream is noticed. |
| `SessionStatus` is a bitset emitted as a closed `enum: [1,2,8,24,32,64]`, which rejects `33, 40, 56, 65, 72, 2147483720` — values present in upstream's own conformance corpora. | Same. Pinned by a test. |
| `chatReducer` reads the wall clock in six places, so reducers are not pure. | Reducers take an injectable clock; the conformance harness pins it to `9999`. |
| No unknown-action fixture exists for the `chat` reducer. | Covered by a local unit test instead. |
| The reducer corpus's comparator drops `null`-valued keys on both sides, so it cannot express absent-vs-explicit-`null`. | A second corpus (`tests/conformance/fixtures/js-semantics.json`) is generated by running adversarial cases through the real pinned TypeScript reducers and frozen verbatim. Compared byte-for-byte. |
| JS signed-int32 bitwise on `SessionStatus` diverges from the u32 used by every runtime client. | We mask to u32, matching four of five clients. |

## Relationship to the spec

This project targets an external specification. Protocol changes come from
upstream, not from contributors' preferences. A PR that changes a wire type, an
action shape, a state field or an error code to anything other than what the
pinned upstream says is declined however sensible the change is. Where something
is underspecified, the question goes upstream — this project does not fork the
spec or diverge privately.
