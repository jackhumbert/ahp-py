# Agent guide

For AI agents and humans building this repository. Assume the reader starts
with zero context.

**Read first:** [`docs/plan.md`](docs/plan.md). It is the whole design and
every section below is a pointer into it. If this file and the plan disagree,
one of them is wrong - fix it in the same PR, never leave them silently
inconsistent.

## Keep it generic

This repository is public. It is a library for anyone to embed or run against
their own deployment, so nothing tracked may name or depend on one particular
setup: no hostnames, machine names, domains, home-directory paths, IP
addresses, tokens, employers or internal projects. Use placeholders
(`example.com`, `my-mac-mini`, `/Users/me`) in code, tests, docs *and* commit
messages — a commit message is as public as the code.

Deployment glue (service files, reverse-proxy config, one fleet's layout)
belongs in the deployment, not here. A feature one setup needs is generalised
into an option or left out.

Anything an agent needs to know about the local setup lives in
`AGENTS.local.md`, gitignored by `*.local.*`. Read it if it exists; never copy
from it into a tracked file.

## What this project is

The **gateway** of the federated AHP fleet: the one component that implements
**both** AHP edges. It is an AHP host facing the surfaces (so web/Windows/CLI
all speak plain AHP to one endpoint, one login) and an AHP client facing each
node (because AHP is strictly client-dials-host and the agent loop, the
filesystem and the terminal are all host-side).

- **Nodes** implement the AHP **server** only.
- **Surfaces** implement the AHP **client** only.
- **This gateway** implements both - and is the only thing that does.

```
                ahp-protocol              ← the shared layer
                  ▲        ▲         ▲
                  │        │         │
        ahp-host │ ahp-client
                  ▲       │       ▲        ← the gateway is BOTH
                  └───────┴───────┘
                    ahp-gateway       ← this repo
```

**Current state: the multiplexer (plan §7 unit 1) exists**, with the
registry's interfaces and a declarative inventory behind it. `docs/plan.md`
§9 records how it works and what it does not do yet; §7 is the build order.

## Depend on the shared layer; do not fork it

`ahp-protocol` holds the wire types, the reducers, version
negotiation, the error taxonomy and the transport ABC. **Do not copy any of
it into this repository.** The gateway's client edge is `ahp-client`'s
`AhpClient`. Its host edge is a **frame router**, not `ahp-host`'s
`Host`: `Host` owns sessions, turns, terminals and resources itself, and a
gateway built on it would re-host every session instead of relaying the node's
(plan §9). What the host edge does take from `ahp-host` is its
`ConnectionInfo` and its WebSocket server. If neither sibling exposes
something the gateway needs, extend the sibling, not this repo - the gateway is
a control plane beside AHP, never a fork of it.

## Federation stays out of the AHP packages

A lone node must stay speakable by a stock AHP client (VS Code). Nothing in
`ahp-{protocol,host,client}` may learn that a fleet exists, and
nothing in this repo may be needed to talk to one host. The only new wire
contract this project owns is the dial-out node's registration + heartbeat
handshake (`relay/`); everything session-level rides AHP untouched.

## Layering

`docs/plan.md` §5 has the layout. Two contracts (import-linter):

- **`core` is the floor its edges plug into.** `registry` supplies node
  records and admission decisions; `ws` and `relay` supply transports and
  tunnels from above. If `core` needs to import one of them, an interface is
  missing at the floor.
- **`registry` stays offline.** It is a directory with admission logic -
  "may user X start on node Y" is decided *before* AHP starts (precedent:
  the host's 403-at-handshake), and a registry import reaching for a
  transport means routing policy leaked below the floor.

## Invariants that must not break

1. **The surfaces never learn federation exists.** One endpoint, one login,
   one flat session list. If a surface needs to know a node exists, the
   aggregation is broken.
2. **Admission before AHP.** The identity check gates whether you may start
   a session on a node at all; the node's OS gates what that session can
   touch. Neither check is deferred into the other's layer.
3. **Gateway↔node is just AHP.** No private action types, no extension
   handshake, no header the sibling host does not already speak.
4. **Never advertise a capability we do not implement** - the family
   invariant, and it doubles here: the gateway advertises to the surfaces
   what the fleet can do, not what one node can.

## Conventions

- Conventional commits. `CHANGELOG.md` from the first release.
- Every protocol claim is backed by a test; prose contradicting a checkable
  fact fails a test (`tests/test_docs.py` is the pattern).
- Docstrings say why, not what.
- Dependency pins are protocol-version statements (`~=0.1.0`), not freshness
  concerns - Dependabot deliberately does not manage them.
