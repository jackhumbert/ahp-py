# ahp-py

Python implementations of the [Agent Host Protocol](https://github.com/microsoft/agent-host-protocol)
(AHP) — Microsoft's protocol for synchronized multi-client state over AI agent
sessions. One repository, several independently installable packages:

| Package | What it is |
|---|---|
| [`ahp-protocol`](packages/ahp-protocol) | Wire types, pure state reducers and upstream's conformance corpora. The shared floor; no runtime dependencies. |
| [`ahp-host`](packages/ahp-host) | The host: serves sessions to any AHP client (VS Code's Agent Sessions view, the Python client, the iOS app). |
| [`ahp-host-claude`](packages/ahp-host-claude) | Claude, via the Claude Agent SDK, as a host provider. |
| [`ahp-host-acp`](packages/ahp-host-acp) | Any [Agent Client Protocol](https://agentclientprotocol.com/) agent as a host provider. |
| [`ahp-client`](packages/ahp-client) | The client. |
| [`ahp-gateway`](packages/ahp-gateway) | One endpoint and one login in front of many hosts: an AHP host to clients, an AHP client to each node. |

```
                 ahp-protocol
               ▲      ▲      ▲
               │      │      │
        ahp-host  ahp-gateway  ahp-client
          ▲                  (host + client)
          │
  ahp-host-claude, ahp-host-acp
```

The native iOS client lives separately, in `ahp-client-ios`.

## Install

Distributed from GitHub, not from a package index. `ahp-protocol` is the floor
and installs first, because the others pin it and no index carries it:

```bash
pip install "ahp-protocol @ git+https://github.com/jackhumbert/ahp-py#subdirectory=packages/ahp-protocol"
pip install "ahp-host[ws] @ git+https://github.com/jackhumbert/ahp-py#subdirectory=packages/ahp-host"
```

Each package's README has its own install lines.

## Develop

```bash
uv sync --all-packages --all-extras --all-groups
scripts/check.sh                  # the CI gate: lint, format, types, contracts, tests
scripts/check.sh ahp-host         # one package
```

Sibling dependencies resolve to the workspace (`[tool.uv.sources]` in the root
`pyproject.toml`), so a change in `ahp-protocol` is what every other package
tests against.

Each package's own README and AGENTS.md describe it; [`AGENTS.md`](AGENTS.md)
covers the repository as a whole.
