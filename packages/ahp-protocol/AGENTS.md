# Agent guide

For AI agents and humans maintaining this repository. Assume the reader starts
with zero context.

**Read first:** [`README.md`](README.md) (what this is and why it exists as its
own distribution), then [`UPSTREAM.md`](UPSTREAM.md) (what revision we target
and how to move it).

## What this project is

The shared protocol layer for the Python Agent Host Protocol ecosystem. AHP is
an external specification owned by Microsoft. We implement it; we do not design
it.

```
                agent-host-protocol          ← this repo
                  ▲             ▲
                  │             │
        agent-host-server   agent-host-client
```

It is **not** a host and **not** a client. It holds only the parts that must be
byte-identical on both ends: wire values, the seven reducers, version
negotiation, the error taxonomy, the transport abstraction, and the vendored
conformance corpora.

Extracted from [`agent-host-server-py`][server], which is where all of this code
was written and where its git history lives. **The migration has landed:** the
server depends on `agent-host-protocol ~= 0.1.0` and imports it throughout
(its commit `0874225`, "feat!: depend on agent-host-protocol"), and the client
was built on this package from its first commit. There is exactly one copy of
the extracted tree — this one — so "check whether the sibling's copy drifted"
stopped being a job anyone has;
[`docs/decisions/0002-extraction.md`](docs/decisions/0002-extraction.md)
records the extraction and carries a dated postscript on the migration.

Current state: **all 247 upstream reducer fixtures, all 39 round-trip fixtures
and the 43-case JS-semantics oracle pass.** `mypy --strict`, `ruff`,
`ruff format` and `lint-imports` are all green and all four are gates.

## Commands

```bash
python -m venv .venv && .venv/bin/pip install -e . && .venv/bin/pip install pytest mypy ruff import-linter jsonschema build
.venv/bin/python -m pytest              # full suite, offline
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/mypy --strict src
.venv/bin/lint-imports                  # enforces the layering below
```

Re-vendoring upstream (only when bumping the pin — see `UPSTREAM.md`):

```bash
scripts/vendor_upstream.sh
python scripts/generate_tables.py       # CI git-diffs the result
scripts/regenerate_js_semantics.sh      # needs Node + a checkout of upstream
```

## Layout

| Path | Contents | May import |
|---|---|---|
| `src/agent_host_protocol/types/` | wire values, `TypedDict` views, `TypeSpec`, generated tables | stdlib only, **not** `json`/`os`/`pathlib`/`asyncio` |
| `src/agent_host_protocol/reducers/` | the seven reducers, injectable clock, `js.py` | `types` |
| `src/agent_host_protocol/channels.py` | `ROOT_URI`, `classify`, `reducer_for_state` | stdlib |
| `src/agent_host_protocol/versions.py` | `parse_version`, `is_compatible`, `negotiate` | stdlib |
| `src/agent_host_protocol/errors.py` | `AhpError`, `to_json`/`from_json`, spec codes | `types` |
| `src/agent_host_protocol/transport/` | the `Transport` protocol + in-memory pair | `types` |
| `src/agent_host_protocol/conformance/` | fixture loaders over the vendored corpora | `types`, `reducers` |
| `vendor/upstream/` | pinned fixtures, schemas and TS sources of truth, **committed** | — |
| `scripts/` | re-vendoring and codegen | — |

The vendored tree has **exactly one copy under version control** and is mapped
into the wheel at `agent_host_protocol/conformance/_upstream/` by hatch's
`force-include`. Do not add a second copy under `src/`; two trees can disagree.
`tests/unit/test_packaging.py` builds a real wheel and asserts the corpus is
inside it, because a source checkout cannot catch the failure this fixes — the
sibling shipped a fixture loader with no fixtures for exactly that reason.

## What may and may not live here

**May.** Anything both a host and a client need to agree on byte-for-byte, and
that performs no I/O beyond reading its own fixture files.

**May not.** Anything that is a *decision* rather than a fact about the
protocol. Concretely: connection supervision, reconnect policy, subscription
bookkeeping, state mirrors, agent providers, filesystem jails, PTYs, policy
hooks, WebSocket transports. Those belong to a peer. If a change here would let
one peer behave differently from the other, it is in the wrong repository.

A useful test: could the Rust, Go and Swift clients all be said to contain this
too? `ahp-types` is the shape to compare against.

**The WebSocket transport is deliberately absent.** The `Transport` ABC and the
in-memory pair are shared; the concrete socket is not, because a serving
`ws.serve()` path and a connecting `ws.connect()` path share almost nothing but
a JSON frame codec. Each peer ships its own behind a `[ws]` extra.

## Invariants that must not break

Each is load-bearing; breaking one produces silent, hard-to-diagnose failures in
*peers*, not in our tests.

1. **`types/` and `reducers/` perform no I/O.** Enforced by `lint-imports`,
   including `json` — a reducer that serialises has an opinion about the wire.
2. **Reducers are pure except for the injected clock.** Never call
   `time.time()`; take it from the module-level provider. The conformance
   harness pins it to `9999`.
3. **Unknown action ⇒ return the input state unchanged.** Never raise. Every
   reducer's fallthrough returns `state`.
4. **Unknown enum values and union variants must not raise either**, and must
   round-trip verbatim. Wire values are plain dicts precisely so this is free.
5. **Never use bare truthiness on an optional field.** `[]` is truthy in
   JavaScript and falsy in Python; `if x:` silently changes reducer behaviour.
   Always `if x is not None:`.
6. **`??` is not `or`.** Use `coalesce()` so every site stays greppable.
7. **`x === undefined` is not `x is None`.** In JSON an explicit `null` is not
   an absent key, and the reference reducers distinguish them. Port
   `=== undefined` as `"x" not in obj`, never as `is None` — that class of
   mistranslation lets a peer wipe a chat transcript. Where the reference uses
   `??` or truthiness, `is None` *is* correct.
8. **Use `reducers/js.py` for every JavaScript-semantics operation.** It exists
   because the same three mistakes keep recurring and the fixture corpus cannot
   see any of them:
   - `js.get` / `js.assign` for reading and writing an optional field. An
     unconditional JS spread writes an explicit `null` **through** and drops
     only an absent key; a helper that deletes on `None` conflates the two, and
     the corpus comparator normalises `null` away so it passes anyway. An audit
     found this in four reducers at once.
   - `js.strict_equal` / `js.index_of` for any `===`. Python `==` matches `True`
     against `1`, matches absent against `null`, and compares objects
     structurally where `===` compares by reference — so an id lookup selects a
     different entry than the reference does, on data a peer controls.
   - `js.key_of` for a `Map`/`Set` key, `js.to_string` for JS `+` coercion.

   New behaviour of this kind needs a case in `scripts/js_semantics_cases.py`
   and a regenerated `tests/conformance/fixtures/js-semantics.json`. The oracle
   is the reference reducer itself — never a hand-written expectation, because a
   hand-written one just restates the reading being tested.
9. **Never route a reducer on a channel URI's scheme.** Session and chat URIs
   are client-chosen and opaque: VS Code uses `<provider>:/<uuid>` for sessions,
   `ahp-chat://<chatId>/<base64 session uri>` for chats, and three
   `agenthost-terminal:` forms for terminals. Routing on the scheme applies *no*
   reducer, which freezes state silently while actions keep arriving. There is
   no `reducer_name_for(uri)` and there must not be one; `classify()` is for
   display and hints, `reducer_for_state()` reads the shape.
10. **`DEFAULT_SUPPORTED_VERSIONS` is not `UPSTREAM_SUPPORTED_PROTOCOL_VERSIONS`.**
    The first is a claim about what the vendored tables cover; the second is a
    fact about upstream. Widening the first without vendoring the tables means
    negotiating a protocol we cannot reduce.
11. **`errors.from_json` accepts any integer code.** Upstream's own schema omits
    `-32011`, and third-party hosts ship their own maps. Rejecting an
    unrecognised code turns someone else's extension into a crash.
12. **Zero runtime dependencies, permanently.** Anything this package requires,
    both peers inherit.

## If a feature is not documented, it does not exist

**Complete the documentation update before committing.** Not after, not in a
follow-up. A feature nobody can find is indistinguishable from one that was
never built.

A change is not finished until:

- **The README's claims still hold.** Its fenced `python` blocks are executed by
  `tests/docs/test_readme_is_true.py`, and its counts and version lists are
  derived from the code — so an example that stops working is a failing test,
  not stale prose. That check earned its place on its first run: the first draft
  of the README dispatched an action that does not exist, and the reducer's
  forward-compatibility fallthrough made it silently do nothing.
- **`UPSTREAM.md` still describes what is actually vendored.** The version it
  inherited from the sibling listed `registry-snapshot.json` as vendored; the
  script has never fetched it.
- **A claim that can be checked, is.** Prose contradicting itself is not
  catchable. Prose contradicting the reducer table, the corpus counts or the
  version constants is.
- **The docstring says why, not what.** The code says what it does; the comment
  says why it is not the obvious thing. Every non-obvious line here should carry
  the evidence that made it non-obvious — a spec sentence, an offset in the
  client bundle, a wire frame.

## Cutting a release

Both consumers pin this package with `~=`, so a release here is a release for
them. Four steps, in order:

1. **Bump `__version__`** in `src/agent_host_protocol/__init__.py`. That is the
   only place; `pyproject.toml` reads it from there.
2. **Give the CHANGELOG a section with that exact heading** — `## [0.1.0] - …`,
   not `## [Unreleased]`. The release job extracts the section by heading and
   **fails when it finds nothing**, deliberately: a release nobody described is
   worse than one that did not happen.
3. **Tag `v<version>`.** The publish workflow refuses a tag that disagrees with
   the packaged version, so the two cannot drift apart at the one moment it
   would be permanent.
4. That triggers a build, a wheel that is installed and *run* from outside the
   checkout, then PyPI via trusted publishing (OIDC — no token exists to leak),
   then a GitHub release.

**One-time setup before the first tag**, which nobody can do from here: PyPI
needs a *pending publisher* configured for the project — owner `jackhumbert`,
repository `agent-host-protocol-py`, workflow `publish.yml`, environment
`pypi`. Without it the publish step fails with an authentication error that
looks like a workflow bug and is not.

The order across the three repos is forced: this package must reach an index
before `agent-host-server` or `agent-host-client` can be installed at all,
because their dependency on it cannot resolve until it is there.

## Absorbing a new upstream spec release

Full procedure in [`UPSTREAM.md`](UPSTREAM.md). The step people skip: **diff
`types/channels-*/reducer.ts` between the two tags by hand.** Upstream's own
reducer branch-coverage gate is vacuous, so a new branch can land with no
fixture and our suite would stay green while diverging.

Because both peers pin `agent-host-protocol ~= 0.1.0`, a MINOR bump here is a
deliberate upgrade on each of them rather than something that arrives silently.

## Conventions

- Conventional commits. `CHANGELOG.md` maintained from the first release.
- Small, reviewable PRs.
- Every protocol claim is backed by a test. "Conformant" without a conformance
  test is a lie, and this package's whole value is that others can trust it.
- Protocol questions go **upstream**, not into a private divergence.

[server]: https://github.com/jackhumbert/agent-host-server-py
