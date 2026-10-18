# ahp-client

[![CI](https://github.com/jackhumbert/ahp-py/actions/workflows/ci.yml/badge.svg)](https://github.com/jackhumbert/ahp-py/actions/workflows/ci.yml)

A Python **client** for the [Agent Host Protocol][ahp] (AHP) — Microsoft's
protocol for synchronized multi-client state over AI agent sessions.

> **"AHP" here means the Agent Host Protocol, not the Analytic Hierarchy
> Process.** The PyPI names `ahp` and `pyahp` belong to packages for the latter,
> which is why this one is spelled out.

> ### ⚠️ Status: working, pre-alpha.
>
> M1–M8 of [`docs/plan.md`](docs/plan.md) §12 are done: the client core, all 32
> commands, the state mirror with write-ahead reconciliation, the front door,
> the per-host supervisor, the reverse direction, wire logs and `doctor`. A full
> turn runs against the sibling Python host. Distributed from this repository —
> deliberately not on PyPI — and the API is not stable.

## Install

From GitHub, protocol package first — its `~=` pin resolves against what is
already installed, and no index carries these names, so on its own the second
line fails with `No matching distribution found for ahp-protocol`:

```bash
pip install "ahp-protocol @ git+https://github.com/jackhumbert/ahp-py#subdirectory=packages/ahp-protocol"
pip install "ahp-client[ws] @ git+https://github.com/jackhumbert/ahp-py#subdirectory=packages/ahp-client"
```

No release is tagged yet — **`ahp-client/v0.1.0` is pending** — so these lines install
`main`. Once the first tag lands, pin it by appending the tag
(`…ahp-client@v0.1.0`), and each
[GitHub release](https://github.com/jackhumbert/ahp-py/releases)
will carry the built wheel and sdist. `[ws]` is the WebSocket transport —
leave it off where a `Transport` comes from somewhere else, such as a notebook
reading a wire log.

```python
import asyncio
from ahp_client import connect, Delta, ToolCallReady, TurnCompleted


async def main() -> None:
    async with connect("ws://localhost:4321") as client:
        async with await client.create_session(provider="echo", cwd=".") as session:
            async for event in session.prompt("Summarise README.md"):
                match event:
                    case Delta(text=text):
                        print(text, end="", flush=True)
                    case ToolCallReady() as call:
                        call.approve()
                    case TurnCompleted():
                        print()


asyncio.run(main())
```

Or, if you do not care about streaming:

```python
result = await session.prompt("Summarise README.md", approvals="reads")
print(result.text)
```

### Starting a session on another's behalf

`create_session(origin=delegation_origin(session, chat, tool_call_id))`
(`ahp_protocol.ext.delegation`) names the session, chat and tool call the new
session is started for. It is an ahp-py extension ahead of the spec, sent in
`createSession` `_meta["ahp-py/origin"]`; an `ahp-py` host puts it in the
session's `origin`, and a stock host ignores it. Anything that is not a
delegation origin is a `ValueError`, before any request.

### Structured results

A turn can ask for a result that conforms to a JSON Schema. This is an ahp-py
extension ahead of the spec: the schema rides in `Message._meta`, and a stock
host ignores it, so the turn then completes with `.structured is None`.

```python
result = await session.prompt(
    "How many files changed?",
    output_schema={"type": "object", "properties": {"count": {"type": "integer"}}},
)
print(result.structured)  # {"count": 3}, already validated by the host
```

For a chat driven by an ahp-host-flow workflow, `chat.flow_view()` and
`session.flow_view()` fold its typed progress descriptors into a run view
(phases, steps, status, results, usage).

### Redaction (ahp-py extension)

A host can redact a response part or a turn's user message after publishing it.
That is an ahp-py extension, so a client opts in:

```python
async with connect(url, redaction=True) as client:
    ...
```

The opt-in rides in `initialize`'s `_meta` (and `reconnect`'s), never in
`capabilities`. The mirror then applies `ahp-py/chat/partRedacted` and
`ahp-py/chat/messageRedacted` with `ahp_protocol.ext.redaction.apply_redaction`,
replacing the content with a `[redacted]` marker, and the event stream yields
`PartRedacted` or `MessageRedacted`. Without the opt-in the host never sends the
action: the client sees the redacted state on its next snapshot (resubscribe or
reconnect), and until then still shows what it already had.

### Terminals

A terminal is the one channel whose state is *contested*: the claim decides who
may type, and the host can refuse to hand it over. The object says who holds it,
and refuses input it knows would be dropped rather than letting keystrokes
vanish into a notification nobody reads.

```python
async with await client.create_terminal(name="build", cwd="file:///work") as term:
    term.write("make -j4\n")  # TerminalNotHeld if we lost the claim
    async for event in term.events():
        match event:
            case TerminalOutput(data=chunk):
                print(chunk, end="")
            case TerminalCommandFinished() as c:
                print(f"exit {c.exit_code}")
            case TerminalRefused(mine=True) as r:
                print(f"refused: {r.reason}")
            case TerminalExited():
                break
```

`client.terminal_command("!ls")` answers the other half: whether the host will
run a chat message as a shell command instead of sending it to the agent. It
follows the prefix the *host* advertised — absence means no shorthand, so a
client that hardcodes `!` offers it to hosts that never claimed it. It is a
heuristic calibrated against the reference host, not a protocol guarantee; the
prefix itself is on `client.terminal_command_prefix`.

### Changesets

What the agent changed, and what you may do about it. Read-mostly: one of the
eight `changeset/*` actions is client-dispatchable and the rest is server push.
The bytes are **not** on disk — they live behind a `ContentRef` in a store the
host owns, which is why a changeset renders on a host that exposes no filesystem
and why a diff's `before` is readable at all.

```python
entry = session.changesets()[0]  # the catalogue, no subscription needed
changeset = await session.open_changeset(entry)
await changeset.wait_until_ready()  # an empty `files` while computing is not "no changes"

for file in changeset.files():
    print(f"{file.change:9} +{file.added}/-{file.removed}  {file.id}")
    print(await changeset.read_text(file.after))  # the ContentRef, not the file URI

if entry.reviewable:  # capabilities.review, re-read every call
    changeset.mark_reviewed([f.id for f in changeset.files()])

for op in changeset.operations():
    if op.destructive:  # a `confirmation` is a client MUST
        print(f"{op.label}: {op.confirmation_text}")
        await changeset.invoke(op.id, resource=file.id, confirmed=user_said_yes)
```

`invoke()` checks `operationId` and `target.kind` against what *this* changeset
declared right now — the list is recomputed by the host and "Commit" is simply
absent while nothing is staged — and refuses an operation carrying a
`confirmation` until you pass `confirmed=True`.

## Why

Upstream publishes AHP clients for Rust, TypeScript, Kotlin, Swift and Go. There
is no Python client. There is a Python **host** — [`ahp-host`][server]
— and the two are meant to meet.

```
                ahp-protocol
                  ▲             ▲
                  │             │
        ahp-host   ahp-client   ← this repo
```

Python is where a large share of agent infrastructure already lives — harnesses,
orchestrators, eval platforms, internal agent services. A conformant client lets
those systems *drive* an agent session that something else is hosting, and lets a
test harness, a CLI or a TUI attach to one over a standard protocol instead of a
bespoke stream per front-end.

## What "fully featured" means here

Not "the TypeScript client, in Python." The reference clients leave real ground
uncovered, and matching them exactly would inherit their gaps:

| Surface | Reference state | Here |
|---|---|---|
| Reducers in the state mirror | TS covers 4 of 7 and **silently ignores every `ahp-chat:` snapshot** | all 7 |
| Channel → reducer binding | TS routes on URI **scheme**; VS Code session URIs are `<provider>:/<uuid>`, so nothing binds | bound at registration |
| Write-ahead reconciliation | specified upstream; **implemented by no reference client** | implemented |
| Server→client requests | TS ships a typed registry with zero implementations; `ahpx` does 2 of 10, read-only | 10 of 10 routed, 9 served — `createResourceWatch` is declined on purpose ([parity](docs/parity.md)) |
| `initialize` payload | TS helper cannot send `clientInfo` or `capabilities` | both |
| Version negotiation | client accepts a version it never offered | verified |
| Server notifications | TS handles 5 of 9; the rest reach neither subscriptions nor `events()` | 9 of 9 |
| Sequence gaps | detected by nobody | detected, reported, never fatal |

Every divergence from a reference client is an ADR in
[`docs/decisions/`](docs/decisions/). Divergence is a decision, not an accident.

## Development

The same install-order rule as above, from editable sibling checkouts:

```bash
python -m venv .venv
.venv/bin/pip install -e ../ahp-protocol
.venv/bin/pip install -e '.[ws]' && .venv/bin/pip install --group dev
.venv/bin/python -m pytest
```

The suite runs offline: no model, no credentials, no network. Modules that
drive the sibling host guard themselves with `pytest.importorskip`, and the
skip is module-wide — without the host installed, `test_m8.py` and
`test_changesets.py` sit out entirely, their pure unit tests included. Install
it (`pip install -e ../ahp-host`) to run everything.

[`CONTRIBUTING.md`](CONTRIBUTING.md) has the rest — the full gate, what a
finished change includes, and the one rule that surprises people (protocol
questions go upstream). Releases are documented in
[`RELEASING.md`](RELEASING.md).

## On conformance evidence, honestly

Driving the sibling Python host is cheap, offline and high-coverage — and it is
**not independent evidence**. Both peers share the same reducers from
`ahp-protocol` and were written from the same reading of the same spec,
so a wrong-but-symmetric reducer passes both suites. It proves that our framing,
handshake, subscriptions and reconciliation interoperate with a real host rather
than only with a fake we also wrote. It proves nothing about whether our reading
of the spec is right.

It has already earned that much: the interop run is what discovered
`AgentInfo.provider` is not `AgentInfo.id`, which the in-repo fake and every
test agreeing with it had happily asserted.

The one mechanism here that yields genuinely independent data is
`ahp_client.doctor` — a conformance probe you point at somebody else's
host. Each check names the MUST or SHOULD it comes from, so a failure is a bug
report rather than an opinion.

## Relationship to upstream

This project targets an external specification. Protocol changes come from
upstream, not from contributors' preferences — a PR that changes a wire type, an
action shape, a state field or an error code to anything other than what the pin
says is declined however sensible it is.

Licensed **MIT**, matching upstream.

[ahp]: https://microsoft.github.io/ahp-protocol/
[protocol]: https://github.com/jackhumbert/ahp-py/tree/main/packages/ahp-protocol
[server]: https://github.com/jackhumbert/ahp-py/tree/main/packages/ahp-host
