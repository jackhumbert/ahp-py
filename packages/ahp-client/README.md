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
| Server→client requests | TS ships a typed registry with zero implementations; `ahpx` does 2 of 10, read-only | 10 of 10 |
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

The whole suite runs offline: no model, no credentials, no network. Install the
sibling host as well (`pip install -e ../agent-host-server-py`) to include the
interop test.

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
