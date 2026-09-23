# agent-host-protocol

The [Agent Host Protocol][ahp] (AHP) as a Python library: wire types, the seven
pure state reducers, version negotiation, the error taxonomy, the transport
abstraction, and **upstream's own conformance corpora, shipped inside the
wheel**.

> **"AHP" here means the Agent Host Protocol, not the Analytic Hierarchy
> Process.** The PyPI names `ahp` and `pyahp` belong to packages for the latter,
> a decision-making method with no relationship to this project — which is why
> this one is spelled out.

> ### ⚠️ Status: pre-alpha.
>
> Extracted from [`agent-host-server-py`][server], whose reducers this is. All
> **256** upstream reducer fixtures, all **39** round-trip fixtures and the
> 63-case JS-semantics oracle pass. Distributed from this repository —
> deliberately not on PyPI — and the API is not stable.

## What this is for

This package is not a host and not a client. It is the layer both of them stand
on, so that the parts of AHP that must be *identical* on both ends exist exactly
once:

```
                agent-host-protocol
                  ▲             ▲
                  │             │
        agent-host-server   agent-host-client
```

That shape is not an invention — it is what every other AHP ecosystem converged
on. Rust ships `ahp-types` / `ahp` / `ahp-ws`; Go ships `ahptypes` / `ahp` /
`ahpws`. Upstream publishes clients for Rust, TypeScript, Kotlin, Swift and Go,
and a host library in no language at all.

**Why one copy and not two.** The reducers are ~2,500 lines of hand-ported
JavaScript semantics with a documented history of six defects that only an
adversarial oracle could find. A fork with a drift-detecting CI job makes
divergence *detectable*; it does not make it impossible, and the failure mode is
silent — the 256-fixture comparator normalises `null` away on both sides, so a
null-passthrough fix landing on one side leaves both suites green while the two
implementations disagree about what a peer just sent.

## Install

Straight from this repository — the packages in this family are not on PyPI:

```bash
pip install "agent-host-protocol @ git+https://github.com/jackhumbert/agent-host-protocol-py"
```

No release is tagged yet — **`v0.1.0` is pending** — so the line above
installs `main`. Once the first tag lands, pin it by appending the tag
(`…agent-host-protocol-py@v0.1.0`), and each
[GitHub release](https://github.com/jackhumbert/agent-host-protocol-py/releases)
will carry the built wheel and sdist it was cut from.

Zero runtime dependencies, permanently. Anything this package required, both
peers would inherit.

## What is in it

| Module | Contents |
|---|---|
| `types/` | wire values, `TypedDict` views, `TypeSpec` validation, the generated upstream data tables |
| `reducers/` | all seven reducers, the injectable clock, and `js.py` |
| `channels.py` | `ROOT_URI`, `classify()`, and `reducer_for_state()` |
| `versions.py` | `parse_version`, `is_compatible`, `negotiate` |
| `errors.py` | `AhpError`, `to_json`/`from_json`, the spec's codes |
| `transport/` | the `Transport` protocol and an in-process pair |
| `conformance/` | fixture loaders over the vendored corpora |

```python
from agent_host_protocol import REDUCERS, reducer_for_state

state = {"agents": [], "activeSessions": 0}
name = reducer_for_state(state)
assert name == "root"

state = REDUCERS[name](state, {"type": "root/activeSessionsChanged", "activeSessions": 3})
assert state == {"agents": [], "activeSessions": 3}

# An action from a newer peer is returned unchanged, never raised on. Forward
# compatibility is a protocol requirement and the corpus tests it.
assert REDUCERS[name](state, {"type": "root/somethingFromTomorrow"}) is state
```

Every fenced `python` block in this file is executed by
`tests/docs/test_readme_is_true.py`, so an example that stops working is a
failing test rather than stale prose. That check earned its place immediately:
the first draft of the block above dispatched `root/sessionCountChanged`, which
is not an action — the reducer's forward-compatibility fallthrough returned the
state unchanged and the example silently did nothing.

### Wire values are plain dicts

There is no parse-into-objects step. `decode` is `json.loads`, `encode` is
`json.dumps`, and static typing comes from `TypedDict` views over the same
objects. Unknown keys, unknown union variants, unknown enum values and integers
beyond int32 all survive by construction.

That is not a shortcut, it is the requirement: a peer is authoritative for state
it relays to peers **newer than itself**, and one that parses into closed models
silently corrupts anything it does not recognise. It paid off immediately in
practice — VS Code's `createSession` carries an `activeClient.tools` array and
session state fields v0.1 does not model, and a host that parsed them away would
have dropped them while remaining authoritative for that state.

### Never route a reducer on a URI scheme

`classify()` tells you what a URI *advertises*, for display and hints. It is not
a routing function, and there is deliberately no `reducer_name_for(uri)`.

Real clients mint URIs the scheme table does not describe: VS Code uses
`<provider>:/<uuid>` for sessions, `ahp-chat://<chatId>/<base64 session uri>`
for chats, and three separate `agenthost-terminal:` forms for terminals — none
of them `ahp-terminal:`. Routing on a scheme therefore applies *no* reducer,
which freezes state silently while actions keep arriving.

Bind the reducer when you register the channel. Where you cannot,
`reducer_for_state()` reads the shape instead, and is verified against all 256
fixtures: every one classifies to the reducer it declares, with no
unclassifiable case.

## Conformance

"Conformant" without a conformance test is a lie, and this package's entire
value is that other implementations can trust it.

- **All 256 upstream reducer fixtures**, consumed unmodified — the same artifact
  the Rust, Go, Kotlin and Swift clients are gated on. All 256, not a subset.
- **All 39 round-trip fixtures**, plus `encode(decode(x)) == x` over the whole
  reducer corpus.
- **The corpus's own blind spot, covered separately.** Its comparator drops
  `null`-valued keys on both sides, so it cannot express the difference between
  an absent key and an explicit `null` — the single most common porting defect,
  and one an audit found in four reducers at once. A second corpus is generated
  by running adversarial cases through the **real pinned TypeScript reducers**
  under Node and freezing the output verbatim, nulls and all. Comparison is
  byte-for-byte, offline.
- **The shape classifier** is asserted against all 256 fixtures' declared
  reducers.

The corpora ship **inside the wheel**, so a downstream implementation can run
the same gate:

```python
from agent_host_protocol.conformance.corpus import reducer_fixtures
```

The suite runs offline with no network and no credentials. Regenerating the
JS-semantics oracle needs Node and a checkout of upstream, and is only done on a
pin bump.

## Versions

Two constants, and they are not the same number:

- `UPSTREAM_SUPPORTED_PROTOCOL_VERSIONS` — what upstream declares.
- `DEFAULT_SUPPORTED_VERSIONS` — what a peer built on this pin can honestly
  speak (`0.8.0`, `0.7.0`, `0.6.0`).

Offer the second. Offering a version whose action and state tables are not
vendored here means negotiating a protocol you cannot reduce: you pass your own
compatibility check and then apply the wrong reducer branches.

This distribution's SemVer is independent of the spec's, exactly as upstream's
own clients are. The pinned spec revision and the bump procedure are in
[`UPSTREAM.md`](UPSTREAM.md); this repository owns that pin for the whole Python
AHP ecosystem, so a peer never vendors anything itself.

## Relationship to upstream

This project targets an external specification. Protocol changes come from
upstream, not from contributors' preferences — a PR that changes a wire type, an
action shape, a state field or an error code to anything other than what the pin
says is declined however sensible it is. Where something is underspecified, the
question goes upstream.

Licensed **MIT**, matching upstream — this repository vendors upstream's
MIT-licensed conformance fixtures and ports its reducers, so identical terms
avoid any compatibility question.

[ahp]: https://microsoft.github.io/agent-host-protocol/
[server]: https://github.com/jackhumbert/agent-host-server-py
