# agent-host-client

A Python **client** for the [Agent Host Protocol][ahp] (AHP) — Microsoft's
protocol for synchronized multi-client state over AI agent sessions.

> **"AHP" here means the Agent Host Protocol, not the Analytic Hierarchy
> Process.** The PyPI names `ahp` and `pyahp` belong to packages for the latter,
> which is why this one is spelled out.

> ### ⚠️ Status: under construction. Nothing is usable yet.
>
> The design is [`docs/plan.md`](docs/plan.md) and the build order is its §12.
> The shared protocol layer this stands on —
> [`agent-host-protocol`][protocol] — exists and is green.
> Not on PyPI.

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

The whole suite runs offline: no model, no credentials, no network.

## Relationship to upstream

This project targets an external specification. Protocol changes come from
upstream, not from contributors' preferences — a PR that changes a wire type, an
action shape, a state field or an error code to anything other than what the pin
says is declined however sensible it is.

Licensed **MIT**, matching upstream.

[ahp]: https://microsoft.github.io/agent-host-protocol/
[protocol]: https://github.com/jackhumbert/agent-host-protocol-py
[server]: https://github.com/jackhumbert/agent-host-server-py
