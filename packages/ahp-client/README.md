# agent-host-client

A Python **client** for the [Agent Host Protocol][ahp] (AHP) — Microsoft's
protocol for synchronized multi-client state over AI agent sessions.

> **"AHP" here means the Agent Host Protocol, not the Analytic Hierarchy
> Process.** The PyPI names `ahp` and `pyahp` belong to packages for the latter,
> which is why this one is spelled out.

> ### ⚠️ Status: working, pre-alpha. Not published.
>
> M1–M8 of [`docs/plan.md`](docs/plan.md) §12 are done: the client core, all 27
> commands, the state mirror with write-ahead reconciliation, the front door,
> the per-host supervisor, the reverse direction, wire logs and `doctor`. A full
> turn runs against the sibling Python host. Not on PyPI; the API is not stable.

```python
import asyncio
from agent_host_client import connect, Delta, ToolCallReady, TurnCompleted


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
is no Python client. There is a Python **host** — [`agent-host-server`][server]
— and the two are meant to meet.

```
                agent-host-protocol
                  ▲             ▲
                  │             │
        agent-host-server   agent-host-client   ← this repo
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

The shared layer is not published yet, so install it from the sibling checkout
first:

```bash
python -m venv .venv
.venv/bin/pip install -e ../agent-host-protocol-py
.venv/bin/pip install -e '.[ws]' && .venv/bin/pip install --group dev
.venv/bin/python -m pytest
```

The suite runs offline: no model, no credentials, no network. Modules that
drive the sibling host guard themselves with `pytest.importorskip`, and the
skip is module-wide — without the host installed, `test_m8.py` and
`test_changesets.py` sit out entirely, their pure unit tests included. Install
it (`pip install -e ../agent-host-server-py`) to run everything.

## On conformance evidence, honestly

Driving the sibling Python host is cheap, offline and high-coverage — and it is
**not independent evidence**. Both peers share the same reducers from
`agent-host-protocol` and were written from the same reading of the same spec,
so a wrong-but-symmetric reducer passes both suites. It proves that our framing,
handshake, subscriptions and reconciliation interoperate with a real host rather
than only with a fake we also wrote. It proves nothing about whether our reading
of the spec is right.

It has already earned that much: the interop run is what discovered
`AgentInfo.provider` is not `AgentInfo.id`, which the in-repo fake and every
test agreeing with it had happily asserted.

The one mechanism here that yields genuinely independent data is
`agent_host_client.doctor` — a conformance probe you point at somebody else's
host. Each check names the MUST or SHOULD it comes from, so a failure is a bug
report rather than an opinion.

## Relationship to upstream

This project targets an external specification. Protocol changes come from
upstream, not from contributors' preferences — a PR that changes a wire type, an
action shape, a state field or an error code to anything other than what the pin
says is declined however sensible it is.

Licensed **MIT**, matching upstream.

[ahp]: https://microsoft.github.io/agent-host-protocol/
[protocol]: https://github.com/jackhumbert/agent-host-protocol-py
[server]: https://github.com/jackhumbert/agent-host-server-py
