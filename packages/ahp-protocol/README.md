# ahp-protocol

The [Agent Host Protocol][ahp] (AHP) as a Python library: wire types, the ten
pure state reducers, version negotiation, the error taxonomy, the transport
abstraction, and **upstream's own conformance corpora, shipped inside the
wheel**.

> **"AHP" here means the Agent Host Protocol, not the Analytic Hierarchy
> Process.** The PyPI names `ahp` and `pyahp` belong to packages for the latter,
> a decision-making method with no relationship to this project — which is why
> this one is spelled out.

> ### ⚠️ Status: pre-alpha.
>
> Extracted from [`ahp-host`][server], whose reducers this is. All
> **327** upstream reducer fixtures, all **94** round-trip fixtures, all
> **22** version-negotiation cases and the 124-case JS-semantics oracle pass.
> Distributed from this repository — deliberately not on PyPI — and the API is
> not stable.

## What this is for

This package is not a host and not a client. It is the layer both of them stand
on, so that the parts of AHP that must be *identical* on both ends exist exactly
once:

```
                ahp-protocol
                  ▲             ▲
                  │             │
        ahp-host   ahp-client
```

That shape is not an invention — it is what every other AHP ecosystem converged
on. Rust ships `ahp-types` / `ahp` / `ahp-ws`; Go ships `ahptypes` / `ahp` /
`ahpws`. Upstream publishes clients for Rust, TypeScript, Kotlin, Swift and Go,
and a host library in no language at all.

**Why one copy and not two.** The reducers are ~2,500 lines of hand-ported
JavaScript semantics with a documented history of six defects that only an
adversarial oracle could find. A fork with a drift-detecting CI job makes
divergence *detectable*; it does not make it impossible, and the failure mode is
silent — the 327-fixture comparator normalises `null` away on both sides, so a
null-passthrough fix landing on one side leaves both suites green while the two
implementations disagree about what a peer just sent.

## Install

Straight from this repository — the packages in this family are not on PyPI:

```bash
pip install "ahp-protocol @ git+https://github.com/jackhumbert/ahp-py#subdirectory=packages/ahp-protocol"
```

No release is tagged yet — **`ahp-protocol/v0.1.0` is pending** — so the line above
installs `main`. Once the first tag lands, pin it by appending the tag
(`…ahp-protocol@v0.1.0`), and each
[GitHub release](https://github.com/jackhumbert/ahp-py/releases)
will carry the built wheel and sdist it was cut from.

Zero runtime dependencies, permanently. Anything this package required, both
peers would inherit.

## What is in it

| Module | Contents |
|---|---|
| `types/` | wire values, `TypedDict` views, `TypeSpec` validation, the generated upstream data tables |
| `reducers/` | all ten reducers, the injectable clock, and `js.py` |
| `channels.py` | `ROOT_URI`, `classify()`, and `reducer_for_state()` |
| `versions.py` | `parse_version`, `is_compatible`, `negotiate`, `InvalidProtocolVersionError` |
| `errors.py` | `AhpError`, `to_json`/`from_json`, the spec's codes |
| `transport/` | the `Transport` protocol and an in-process pair |
| `conformance/` | fixture loaders over the vendored corpora |
| `ext/` | ahp-py extensions ahead of the spec, see below |

```python
from ahp_protocol import REDUCERS, reducer_for_state

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
`reducer_for_state()` reads the shape instead, and is verified against all 327
fixtures: every one classifies to the reducer it declares, with no
unclassifiable case.

### An extension a stock peer ignores

`ahp_protocol.ext.structured` holds the shared constants and the wrap rule of
ahp-py's structured turns: a client names a JSON Schema in a namespaced
`_meta` key and gets the validated value back on the completed turn. It is an
ahp-py extension ahead of the spec, carried in `_meta` and a host-internal tool,
so a stock peer ignores it.

`ahp_protocol.ext.flow` does the same for typed workflow progress: a host puts a
small descriptor under `_meta["ahp-py/flow"]` of the system notifications that
open and close a run and its phases, and of each step's tool call. The JSON
Schema of the descriptors ships as `flow.schema.json`, and `run_view` folds a
chat's reduced state into phases, steps, results and usage. A stock client
ignores the `_meta` and renders the text as before. A host also marks each custom
verb's tool call with a `verbStarted`/`verbFinished` descriptor, and `run_view` lists
them as `verbs` (kind `verb`) beside the steps. A step blocked on a request carries
`waiting` entries (`{kind, chat?}`) beside `awaitingInput`, and `RunView.waiting` lists them
as `WaitingView`s across the awaiting steps.

```python
from ahp_protocol.ext import flow

started = flow.step_started(
    ordinal=0, label="a", phase=None, chat="chat:/w0", model=None, provider="echo"
)
finished = flow.step_finished(status="ok", result="hi", input_tokens=1, output_tokens=2)
chat_state = {
    "turns": [
        {
            "id": "t1",
            "state": "complete",
            "responseParts": [
                {
                    "kind": "systemNotification",
                    "_meta": flow.meta(flow.run_started("r1", "demo")),
                },
                {
                    "kind": "toolCall",
                    "toolCall": {
                        "toolCallId": "c0",
                        "status": "completed",
                        "_meta": flow.meta(flow.step(started, finished)),
                    },
                },
                {
                    "kind": "systemNotification",
                    "_meta": flow.meta(flow.run_finished("ok")),
                },
            ],
        }
    ]
}
view = flow.run_view(chat_state)
assert view.status == "ok"
assert view.steps[0].result == "hi"
```

A client starts a workflow with a typed start under the same key, `{workflow, args?,
runId?}`, on an ordinary `chat/turnStarted` message whose text stays `/name {json}`.
`workflows_of` lists the workflows a host offers from its customizations:

```python
from ahp_protocol.ext import flow

start = flow.workflow_start("host:nightly", args={"day": "mon"}, run_id="r1")
assert start == {"workflow": "host:nightly", "args": {"day": "mon"}, "runId": "r1"}
assert flow.parse_workflow_start(flow.meta(start)).run_id == "r1"
```

A host running a workflow also keeps a client attached while its steps are silent: every
few seconds it sends a `chat/activityChanged` on the run's chat whose `_meta["ahp-py/flow"]`
is a heartbeat descriptor, `{heartbeat: {running, tick}}` (`flow.HEARTBEAT_KIND` is its one
key; `running` is the steps in flight, `tick` counts from 1). A client that resets an idle
timer on activity changes stays attached; a stock one shows the activity text.

```python
from ahp_protocol.ext import flow

beat = flow.heartbeat(running=2, tick=1)
assert beat == {flow.HEARTBEAT_KIND: {"running": 2, "tick": 1}}
assert flow.descriptor_of(flow.meta(beat)) == beat
```

`ahp_protocol.ext.usage` marks a `TurnSink.usage` report that is one model request's
usage rather than the turn's: a provider that reports per request puts
`request_usage_meta()` (`{"ahp-py/usage": {"scope": "request"}}`, key `USAGE_META_KEY`) in
the report's `_meta`, and a reader that sums a turn's usage checks `is_request_scoped(meta)`
so it neither adds the request twice nor takes it for the turn. A reader that does not know
the key treats the report as an ordinary one.

```python
from ahp_protocol.ext.usage import USAGE_META_KEY, is_request_scoped, request_usage_meta

meta = request_usage_meta()
assert USAGE_META_KEY in meta and is_request_scoped(meta)
assert not is_request_scoped({}) and not is_request_scoped(None)
```

`ahp_protocol.ext.delegation` is the origin of a session started on another
session's behalf: `delegation_origin(session, chat, tool_call_id)` builds
`{kind: "ahp-py/delegation", session, chat, toolCallId}` (`SessionOrigin.kind` is
nonexhaustive, and clients keep unknown kinds), a client sends it under
`createSession` `_meta["ahp-py/origin"]`, and `parse_delegation` reads it back
(`None` for any other kind or an incomplete one). A host seeds it into the
session's `origin`; it is the client's claim, not verified.

`ahp_protocol.ext.operation` carries a changeset operation's parameters in
`InvokeChangesetOperationParams._meta["ahp-py/operation"]`, which a stock host
ignores: `operation_meta(params)` builds the `_meta` object and `operation_params(meta)`
reads it back (`{}` for anything that is not a mapping, never raising).

`ahp_protocol.ext.redaction` is the extension for redacting content after it was
published. A host rewrites a response part (or a turn's user message) to a marker
that uses only existing part kinds, `[redacted]` plus a reason and time under
`_meta["ahp-py/redaction"]`, with ids unchanged. It sends the extension actions
`ahp-py/chat/partRedacted` and `ahp-py/chat/messageRedacted` only to a connection
that advertised the `ahp-py/redaction` capability at initialize; a stock client
never receives an action it does not know and sees the redacted state in its next
snapshot. `apply_redaction` is the pure reducer both peers share. Only the shared
view is redacted, never an agent's own context.

```python
from ahp_protocol.ext.redaction import apply_redaction, marker_of, part_redacted

state = {
    "turns": [
        {
            "id": "t1",
            "state": "complete",
            "responseParts": [{"kind": "markdown", "id": "p1", "content": "token=abc"}],
        }
    ]
}
action = part_redacted("t1", "p1", "leaked", "2026-01-01T00:00:00.000Z")
part = apply_redaction(state, action)["turns"][0]["responseParts"][0]
assert part["content"] == "[redacted]"
assert marker_of(part)["reason"] == "leaked"
```

## Conformance

"Conformant" without a conformance test is a lie, and this package's entire
value is that other implementations can trust it.

- **All 327 upstream reducer fixtures**, consumed unmodified — the same artifact
  the Rust, Go, Kotlin and Swift clients are gated on. All 327, not a subset.
- **All 22 version-negotiation cases**, against upstream's own baselines.
- **All 94 round-trip fixtures**, plus `encode(decode(x)) == x` over the whole
  reducer corpus.
- **The corpus's own blind spot, covered separately.** Its comparator drops
  `null`-valued keys on both sides, so it cannot express the difference between
  an absent key and an explicit `null` — the single most common porting defect,
  and one an audit found in four reducers at once. A second corpus is generated
  by running adversarial cases through the **real pinned TypeScript reducers**
  under Node and freezing the output verbatim, nulls and all. Comparison is
  byte-for-byte, offline.
- **The shape classifier** is asserted against all 327 fixtures' declared
  reducers.

The corpora ship **inside the wheel**, so a downstream implementation can run
the same gate:

```python
from ahp_protocol.conformance.corpus import reducer_fixtures
```

The suite runs offline with no network and no credentials. Regenerating the
JS-semantics oracle needs Node and a checkout of upstream, and is only done on a
pin bump.

## Versions

Two constants, and they are not the same number:

- `UPSTREAM_SUPPORTED_PROTOCOL_VERSIONS` — what upstream declares.
- `DEFAULT_SUPPORTED_VERSIONS` — what a peer built on this pin can honestly
  speak (`1.0.0`, `0.9.0`, `0.8.0`, `0.7.0`, `0.6.0`).

Offer the second.

`negotiate()` follows the spec's caret rule (1.0.0): it picks the **highest**
offered version inside the caret range of any supported baseline — `^1.0.0`
accepts `1.10.0`, `^0.9.0` accepts `0.9.3` but not `0.10.0` — and returns that
exact string. A malformed offer raises `InvalidProtocolVersionError` instead of
being skipped:

```python
from ahp_protocol import InvalidProtocolVersionError, negotiate

assert negotiate(["0.9.0", "1.2.0"]) == "1.2.0"
assert negotiate(["2.0.0"]) is None  # refuse: UnsupportedProtocolVersion

try:
    negotiate(["1.0"])
except InvalidProtocolVersionError as error:
    assert error.version == "1.0"
``` Offering a version whose action and state tables are not
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

[ahp]: https://microsoft.github.io/ahp-protocol/
[server]: https://github.com/jackhumbert/ahp-py/tree/main/packages/ahp-host
