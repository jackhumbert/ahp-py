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
| Spec ref | `main` — ahead of any release; upstream's tree declares `1.1.0`, unreleased |
| Spec commit | `c5628cd4b09f76cf143ebe655e872c0fb3f1b8d8` |
| Conformance corpus vendored from | `main` @ `c5628cd4` → `types/test-cases/` |
| `UPSTREAM_PROTOCOL_VERSION` | `1.1.0` — upstream's development version at that commit |
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
release (step 8 below). No release of this package has carried the 1.0.0 pin
yet — the newest tag is still `ahp-protocol/spec-0.7.0` — so that one release
has not happened, and this bump keeps them.

### Why a commit on `main`, not a tag

The pin is normally a `spec/vX.Y.Z` tag. This one is not, because waiting was
the larger risk. Upstream's TypeScript client, built from `main`, offers
`[PROTOCOL_VERSION]` — `1.1.0` — and negotiation is a caret rule, so every host
here accepts that offer under the `1.0.0` baseline. On 1.0.0 tables a host
would answer "1.1.0" and then reduce with 1.0.0 branches: a client that queues
two steering messages keeps both, the host keeps only the latest, and the
earlier one sits in the client's pending list for good.

`scripts/vendor_upstream.sh` takes a branch as well as a tag for this. A tag
must still resolve to the pinned commit; a branch moves, so the commit only has
to be on it. Either way the archive is cut from the **commit**. When
`spec/v1.1.0` is tagged, re-pin to it and diff every `reducer.ts` between
`c5628cd4` and the tag — upstream may change 1.1.0 again before releasing it.

For the record, as of **2026-10-25**: `spec/v1.0.0` (2026-10-02) is still the
newest spec tag, and the pinned commit is upstream `main`'s head, 56 commits
past it. Upstream's `CHANGELOG.md` has only an empty `[1.1.0] — Unreleased`
heading; the change notes live in `docs/.changes/*.json` until release.

The `main` @ `1ef06429` → `main` @ `c5628cd4` absorb (2026-10-25). **No
`reducer.ts` changed** (diffed by hand, as step 6 requires), and all 124
JS-semantics cases regenerate byte-identically against the new reducers. Three
additive, optional wire fields, and eleven new fixtures (reducers 308–315,
round-trips 076–078) that pass unchanged:

- **`authenticate` — `target`** (security). `AuthenticateParams.target` is
  `{kind: "mcpServer", session, customizationId}` (`AuthenticateTargetKind` is
  `@nonexhaustive`). When present, the host MUST apply the token to that one MCP
  server in that session only, `resource` must still equal the server's
  advertised one, and an unknown session, unknown customization or a
  customization that is not an MCP server is `InvalidParams`. Without `target`
  nothing changes. Wire only here; the enforcement belongs to the host.
- **changesets — nonces.** `ChangesetFile.nonce` and
  `ChangesetOperationTarget.nonce` (both the resource and range variants); when
  both are present a mismatch must fail with `Conflict`. No host here publishes
  nonces, so that rule cannot trigger yet.
- **changesets — `ChangesetFile.isEditing`**, advisory only.

The `spec/v1.0.0` → `main` @ `1ef06429` absorb, reducer by reducer (every
`reducer.ts` diff read by hand). **Only `chat` changed, and not additively** —
the first behaviour change to an existing branch since 0.9.0. Every one of the
102 pre-existing JS-semantics cases still reproduces unchanged; 22 new ones
cover the branches below.

- **`chat` — one pending list.** Steering and queued input share
  `queuedMessages`, each entry carrying an optional `kind` (absent means
  `queued`). `chat/pendingMessageSet` appends a new id and replaces a known one
  in place, kind and all; `chat/pendingMessageRemoved` removes the first entry
  whose id **and** effective kind match. `steeringMessage` is deprecated and
  kept only as a mirror of the latest steering set, for 1.0.0 clients: a
  matching steering removal, a matching `chat/turnStarted`, or re-setting the
  same id as queued clears it, and nothing back-fills it. The old fixture 040
  (one steering slot, replaced) is gone, 052 is renamed, and 039, 041–043 and
  045–047 are rewritten.
- **`chat` — `chat/turnStarted`** consumes a pending message of either kind,
  and tests `queuedMessageId !== undefined` where it tested truthiness — so
  `""` and an explicit `null` consume now (fixture 303).
- **`chat` — `chat/configChanged`** (client-dispatchable): a per-chat overlay,
  `ChatState.config.values`, over the session's config. Merged, or replaced when
  `replace` is set or there is no overlay yet. Only properties marked
  `chatMutable` in the session's schema may be set; the reducer does not check
  that, the host must.
- **wire, no reducer** — `CreateChatParams.title`, `SessionModelInfo.vendor`,
  `SessionConfigPropertySchema.chatMutable`, and `EmptyState` (`{}`) as a
  `Snapshot.state`, which a server may omit under ordinary delivery and a
  client must tolerate, as it must any state it does not recognise.
- **experimental, opt-in** — windowed delivery (`subscribe.flowControl`,
  `reconnect.windows`, `channel/frame`, `channel/credit`, `channel/snapshot`)
  and the TCP channel (`createTcpConnection`, `tcp/data`, `tcp/eof`,
  `tcp/reset`, error `-32012 TcpConnectionOpenFailed`). A host that returns no
  `flowControl` has chosen ordinary delivery, which is conformant; no peer here
  offers or accepts either yet. Their six notifications are the first that
  `NOTIFICATION_INTRODUCED_IN` withholds from a 1.0.0 connection.

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
| `types/test-cases/reducers/**` (335 fixtures) | git repository only | not published as a release asset |
| `types/test-cases/round-trips/**` (97 fixtures) | git repository only | same |
| `types/test-cases/version-negotiation.json` (22 cases) | git repository only | same |
| `schema/*.schema.json` (5 files) | release asset **or** git repository | asset exists, but must match the pinned commit |
| `ts/` — `registry.ts`, `action-origin.generated.ts`, `actions.ts`, `errors.ts`, `session-state.ts` | git repository only | the codegen inputs for `_generated.py` |
| `ts/` — `messages.ts`, `commands.ts`, and `commands-{root,session,chat,terminal,changeset,resource-watch,automation,tcp}.ts` | git repository only | the command/notification maps and per-channel params the peers' parity and scoping gates read |

The conformance corpora exist **only in the git repository**, never as release
assets. Vendored files are committed and never fetched during a test run; the
suite must pass offline.

## Procedure for bumping the pin

1. Read the upstream `CHANGELOG.md` between the old and new tag. Enumerate every
   entry under **Removed** and **Changed** — the changelog states outright that
   breaking changes land in MINOR bumps. Pinning a commit on `main` instead,
   read `docs/.changes/*.json`: the changelog gets those entries only at
   release.
2. Update the table above, in the same commit as the vendored files.
3. Set `SPEC_REF` and `SPEC_COMMIT` in `scripts/vendor_upstream.sh` and run it
   (it hard-fails if a tag has moved off the pinned commit, or the commit is
   not on the pinned branch).
4. Run `python scripts/generate_tables.py`; CI re-runs it and `git diff
   --exit-code`s, so a stale `_generated.py` is a failing test.
5. Run the conformance suite. New fixtures that fail identify the reducer
   branches to port.
6. **Diff `types/channels-*/reducer.ts` between the two tags by hand.** Do not
   rely on new fixtures existing for new branches: upstream's own
   `--branches 100` reducer coverage gate has been vacuous since commit
   `ad3f9b96`, because it still globs `types/reducers.ts`, which is now a
   re-export shim with zero branches.
7. Check out the pinned commit in `.research/agent-host-protocol` and re-run
   `scripts/regenerate_js_semantics.sh` (needs Node) so the JS-semantics oracle
   reflects the new reducers. The script runs that checkout's **working tree**,
   not the pin: left on the old tag, it regenerates the old reducers' answers.
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
