# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/) — **independently of the
protocol's own version**, exactly as upstream's clients do. Every release states
the spec revision it targets.

## [Unreleased]

### Changed — the spec pin moves to `spec/v0.9.0`

- **The pinned upstream revision is `spec/v0.9.0` (`60706330`).** Re-vendored
  and regenerated: `UPSTREAM_PROTOCOL_VERSION` is `0.9.0`, 96 actions (44
  client-dispatchable), 272 reducer fixtures and 44 round-trips — all pass.
  `DEFAULT_SUPPORTED_VERSIONS` is `("0.9.0", "0.8.0", "0.7.0", "0.6.0")`.
  `channels-automation/commands.ts` joins the vendored per-channel command
  files.
- **Two new reducers, `automation` and `automationRun`** — nine in all. The
  shape classifier checks `claim` and `automation` before `lifecycle`, which
  terminal and automation-run state now carry too; `classify()` knows
  `ahp-automations://` and `ahp-automation-run:`; the schema gate maps both
  new kinds.
- **Chat: turn errors are response parts, and turns can resume.** `chat/error`
  appends its `ErrorResponsePart` to the ended turn (`Turn.error` is gone),
  `chat/responsePart` drops an error part, and `chat/turnResume` reopens the
  latest errored turn when its last part is `resumable: true`.
- **No reducer reads the clock.** `modifiedAt` is the action's `startedAt` at
  turn start and `startedAt + duration` at turn end, via the new
  `clock.add_milliseconds_to_timestamp` — a port of
  `new Date(Date.parse(t) + d).toISOString()`, verified against V8 on 19 edge
  cases. One deliberate divergence: where that expression throws (unparseable
  `startedAt`), upstream rejects the action and strands the turn; this port
  ends the turn and keeps the previous `modifiedAt`.
- **`_end_turn` is JSON-faithful:** it no longer writes `"usage": null`,
  `"activeTurn": null` or `"duration": null` where the reference emits no key
  (or `0` for a null duration) — found by the new oracle cases.
- Terminal: `terminal/exited` writes `lifecycle: {status: "exited", exitCode?}`.
  Session: the failed lifecycle is `failed`. Changeset: `contentChanged` no
  longer carries `error`. Annotations: `annotations/updated` copies `origin`,
  not `turnId`. Errors: `ContentNotFound` is removed.
- The JS-semantics oracle grows from 63 to **81** cases, covering the automation
  reducers, turn resume, error parts, derived `modifiedAt`, and the terminal
  lifecycle.

### Changed — the spec pin moves to `spec/v0.8.0`

- **The pinned upstream revision is `spec/v0.8.0` (`7153143f`), up from
  `spec/v0.7.0` (`ea6fae67`).** The vendored corpora, schemas and TypeScript
  inputs are re-vendored and `_generated.py` regenerated:
  `UPSTREAM_PROTOCOL_VERSION` is `0.8.0`, there are 86 actions (39
  client-dispatchable), and the reducer corpus grows from 247 to **256**
  fixtures — all nine new ones in `session`, and all 256 pass.
- **The session reducer ports every 0.8.0 change.** Only
  `channels-session/reducer.ts` differs between the tags; the diff was read by
  hand, per `UPSTREAM.md`:
  - `session/workingDirectoryReplaced` (new, client-dispatchable) is an atomic
    compare-and-swap on one working directory, deduplicating against the
    replacement;
  - `session/customizationToggled` now carries a complete `enablement` decision
    list that **replaces** the previous one — plugins and MCP servers store it
    verbatim, other customizations derive the legacy `enabled` flag from its
    first entry. **Breaking on the wire:** a toggle carrying only `enabled`
    (the pre-0.8.0 shape) is now a no-op. Upstream throws on it; as with
    every other throw site, this port degrades to the branch's no-op instead;
  - a `toolClientExecution` entry in `inputNeeded` no longer raises
    `SessionStatus.InputNeeded` — it is work delegated to a client, not a
    prompt, so the session stays `InProgress` while it runs.
- **`DEFAULT_SUPPORTED_VERSIONS` is `("0.8.0", "0.7.0", "0.6.0")`.** It widens
  because the vendored tables now cover 0.8.0, and does not narrow because
  upstream still declares 0.7.0 and 0.6.0 supported. The reducers are 0.8.0's
  for every negotiated version, so the `customizationToggled` shape change
  above applies to older clients too — exactly as it does against upstream's
  own reducer.
- The `Customization.plugin` wire spec takes `enablement` in place of
  `enabled`.
- The JS-semantics oracle grew from 51 to **63** cases, regenerated from the
  0.8.0 TypeScript reducers: the undefined-vs-null and `===` edges of
  `workingDirectoryReplaced`, the non-throwing malformed `enablement` shapes,
  and the client-execution exemption from `InputNeeded`. Every pre-existing
  expectation is unchanged.

### Documentation

- **The upstream watch record in `UPSTREAM.md` is re-verified as of
  2026-08-13, and its old claim retired.** `spec/v0.7.0` is still the newest
  spec tag, so the pin stands — but the recorded "the tag→HEAD `types/` diff
  is three cosmetic files" is no longer true: unreleased 0.8.0 work on
  upstream `main` now touches the session-channel reducer, state and actions,
  reshapes the `session/customization*` fixture family, and rewrites all five
  published schemas. The record now says so, and says the next bump is a full
  absorb. Also noted: `@microsoft/agent-host-protocol@0.7.0` reached npm on
  2026-08-06, built from the same tag this pin vendors.

### Changed — public on GitHub, deliberately not on PyPI

- **The distribution model is settled: these repositories are public, and the
  packages install from GitHub, not from an index.**
  `pip install "agent-host-protocol @ git+https://…"`, with releases pinned by
  tag and the built wheel and sdist attached to each GitHub release. The
  `publish.yml` workflow is `release.yml` now: the same gates — tag must match
  `__version__`, the wheel is installed and run from outside the checkout, the
  CHANGELOG must describe the version — and then the GitHub release, with no
  upload after it. The trusted-publisher setup, the `pypi` environment and the
  index-driven "protocol must publish first" ordering all cease to exist; what
  remains is a documented *install* order (this package before its consumers,
  since their `~=` pins resolve against the installed environment). CI checks
  out the sibling repositories without a token, which public repositories make
  the ordinary case rather than the fallback.

### Fixed — CI and release mechanics

- **`consumers-still-build` could not install either consumer.** Three defects
  at once, none of which can fail locally: it checks out two *private*
  repositories with a `GITHUB_TOKEN` scoped to this one; the client has since
  moved its dev requirements to a PEP 735 group, so `.consumer[dev]` names an
  extra that does not exist — which the job's own comment predicted and could
  not detect; and `--group` is a pip 25.1 feature while `setup-python` installs
  whatever pip the interpreter build bundled, so `check` and
  `codegen-is-reproducible` were one runner image away from failing on "no such
  option". The consumers' installs are now spelled out per consumer in the
  matrix rather than hidden behind a `||`, so the next move breaks this file
  loudly.

- **PyPI was published to before the gates, not after.** `publish` needed only
  `build`, and `release` needed `publish` — so the CHANGELOG gate, which lives
  in `release` and exits non-zero when a version has no section of its own, ran
  *after* the upload it exists to guard. PyPI does not let a version be
  replaced, so a release nobody described would have been permanent the moment
  that gate first fired. A GitHub release for a version that then fails to
  upload is recoverable in a click, which is the direction the irreversibility
  should point.

### Added

- **`agent_host_protocol.conformance.schemas`** — the vendored JSON Schemas as
  an assertion, shipped rather than kept in `tests/`. A host and a client each
  need to prove the same thing about opposite directions of the same wire, and
  two copies of that file is exactly the drift this package exists to prevent.
  `jsonschema` is imported at module scope and is deliberately NOT a dependency:
  whether a missing validator should skip a test or fail a build is the caller's
  policy, not ours.
- `SECURITY.md`, and a disclosure path. This package performs no I/O, so its
  surface is not what it can reach but what it computes for the peers that trust
  it — the reducers, and `IS_CLIENT_DISPATCHABLE`, which is the table a host
  uses to decide whether a peer may dispatch an action at all.
- A tag-triggered publish workflow using PyPI trusted publishing (OIDC), with a
  wheel that is installed and RUN from outside the checkout before it is
  released (`scripts/smoke_wheel.py`).
- A non-blocking CI job that builds and tests both consumers against this
  package's `main`. It is a floor, not a leaf: a change here that breaks the
  host or the client has broken the point of the extraction, and finding that
  out at their next release is too late.

### Removed

- `scripts/check_sibling_drift.py` and its CI job. It existed to make divergence
  visible while the host still carried its own copy of this tree — and that
  premise stopped being true when the host migrated onto this distribution.
  Packaging enforces what the script watched for: there is one copy now, and you
  cannot import upwards across a wheel. Its docstring had become the thing
  `AGENTS.md` warns about, a document asserting the absence of something that
  shipped.

### Changed

- The version is single-sourced from `__init__.py` through hatchling's dynamic
  version. It was declared in two places, which is how the sibling host shipped
  `0.0.0` twice.

### Fixed — a three-repo conformance review against the pin

An adversarially-verified review against the vendored `spec/v0.7.0` sources,
with the js-semantics oracle regenerated from the real pinned TypeScript
reducers as the acceptance gate. Each fix is pinned by a test.

- **The `-32005` error data emitted `supportedProtocolVersions`.** The pinned
  `UnsupportedProtocolVersionErrorData` declares exactly one field,
  `supportedVersions` (`errors.ts:157`; required by `errors.schema.json`) — a
  conformant client read the one frame that explains a handshake failure as
  empty. The sibling host's local workaround is retired.
- **The chat reducer matches the reference on peer-controlled data**:
  strict-equality (`===`/`indexOf`) id lookups (`True` is not `1`, a
  structurally-equal object id matches nothing), JS-truthiness gates (`{}`
  approves, `''` contributor is ignored), and JS object-spread of non-mapping
  `chat/inputCompleted` answers (a string spreads to index keys, a number to
  nothing) where the port raised `TypeError` on peer JSON.
- **Tool-call state literals now carry JS member semantics**: a member the
  reference computes as `undefined` is absent from the produced state — as
  `JSON.stringify` emits it — where the port wrote explicit `null` for
  `toolName`, `displayName`, `intention`, `contributor`, `_meta`,
  `invocationMessage`, `toolInput` and `usage` across the whole tool-call
  machine, and `chat/usage`/`chat/toolCallContentChanged` overrides delete the
  key exactly where the reference's literal-over-spread does. The oracle
  distinguishes the two spellings verbatim; the null-normalising corpus never
  could.
- **Session and root reducer omit-vs-null fixes**:
  `session/mcpServerStateChanged` omit-to-clear for `channel`/`state`,
  `session/changesetsChanged` null clears the catalogue (pinned fixture 146)
  while `[]` sets an empty one, `creationFailed`/`customizationToggled` and
  the id reads follow `js.get`; the root reducer no longer raises `KeyError`
  on actions with missing properties and clears omitted full-replacement keys
  as the reference does.
- **`MemoryTransport.receive()` returns `None` persistently after
  end-of-stream** — previously the side whose peer closed hung forever on the
  second call.
- The schema gate covers `resourceWatch` snapshots (was `KeyError`) and
  validates `authenticate` results instead of skipping them; `now_iso` joined
  the clock module's `__all__`; the shared `Transport.receive()` contract
  documents the one-raise-per-malformed-frame rule both peers rely on.

### Added — from the same review

- **`NOTIFICATION_INTRODUCED_IN`** — the pinned `registry.ts` 8-method
  notification→version table, vendored through the generator so a pin bump
  that adds a notification cannot land without its version. Inert at this pin
  (every method predates the oldest negotiable version) and pinned as such.
- **The js-semantics oracle grew from 43 to 51 cases**, adding the chat
  reducer to its coverage — including the case that caught the tool-call
  member-spelling divergence above. One documented divergence is pinned where
  the oracle cannot be matched without leaking a sentinel into consumer state:
  a *repeated* absent-`directory` `chat/workingDirectorySet` appends a second
  null image where the reference's in-memory `undefined` dedupes; the
  reducer's comment records the trade.

Extracted from [`agent-host-server-py`](https://github.com/jackhumbert/agent-host-server-py),
where all of this code was written. See [ADR 0002](docs/decisions/0002-extraction.md).

### Added

- Wire types, `TypedDict` views, `TypeSpec` validation and the generated
  upstream data tables (`types/`).
- All seven state reducers, the injectable clock and `js.py` (`reducers/`).
- Protocol-version negotiation (`versions.py`), the error taxonomy
  (`errors.py`), channel URIs (`channels.py`) and the transport abstraction with
  an in-process pair (`transport/`).
- `errors.from_json()` — the receiving half of the error taxonomy, which a host
  never needed. Accepts **any** integer code: upstream's own
  `errors.schema.json` omits `-32011 Conflict`, and third-party hosts ship their
  own maps, so rejecting an unrecognised code would turn someone else's
  extension into a crash.
- `channels.reducer_for_state()` — a state **shape** classifier, verified
  against all 247 upstream fixtures' declared reducers with no unclassifiable
  case (root 7, session 70, chat 123, terminal 19, changeset 16, resourceWatch
  2, annotations 10).
- The vendored conformance corpora ship **inside the wheel**, so a downstream
  implementation can run the same gate. `tests/unit/test_packaging.py` builds a
  real wheel and asserts they are in it.
- `scripts/check_sibling_drift.py` — reports divergence from the sibling host's
  still-unmigrated copy of this tree.
- `tests/docs/test_readme_is_true.py` — executes every fenced `python` block in
  the README and derives its counts and version lists from the code.

### Removed

- `channels.reducer_name_for(uri)`. It was dead code that picked a reducer from
  a URI **scheme**, which is the exact failure the sibling's own invariant 15
  forbids: VS Code mints `<provider>:/<uuid>` for sessions and three
  `agenthost-terminal:` forms for terminals, so scheme routing binds no reducer
  at all and state freezes silently while actions keep arriving. Use
  `reducer_for_state()`, or bind the reducer when you register the channel.

### Fixed

- `conformance.corpus.CORPUS_ROOT` resolved to a `vendor/` directory outside the
  package, so an installed wheel carried a fixture loader and no fixtures. It
  now prefers the packaged tree and falls back to the source checkout.

### Notes

- Targets spec `spec/v0.7.0` (`ea6fae670c4012721fdc02d587b3a46ecdc871c0`).
  `DEFAULT_SUPPORTED_VERSIONS` is `0.7.0, 0.6.0`.
- `agent-host-server-py` has since been migrated onto this package (the
  `check_sibling_drift.py` removal above records the moment the premise
  changed); both consumers now import the one copy.
