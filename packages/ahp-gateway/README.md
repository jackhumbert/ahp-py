# agent-host-broker

A federated broker for the [Agent Host Protocol](https://github.com/jackhumbert/agent-host-protocol-py)
(AHP): one endpoint and one login behind which a developer sees a single flat
"my sessions everywhere" view across every box that runs an agent for them.

It is an AHP **host** facing the surfaces (web, Windows, CLI) and an AHP
**client** facing each node. Nodes implement the AHP server only; surfaces
implement the AHP client only; the broker implements both, and is the only
component that does. The data plane is AHP, unmodified - a stock AHP client
still works against a bare node with no fleet in sight.

It is the newest member of the family:

```
                agent-host-protocol              ← the shared layer
                  ▲        ▲         ▲
                  │        │         │
        agent-host-server │ agent-host-client
                  ▲       │       ▲        ← the broker is BOTH
                  └───────┴───────┘
                    agent-host-broker       ← this repo
```

Status: pre-alpha scaffold. [`docs/plan.md`](docs/plan.md) is the design; its
§7 is the build order.

## Install

The family is distributed from GitHub, not from an index. The protocol package
is the floor and installs first:

```bash
pip install "agent-host-protocol @ git+https://github.com/jackhumbert/agent-host-protocol-py"
pip install "agent-host-server @ git+https://github.com/jackhumbert/agent-host-server-py"
pip install "agent-host-client @ git+https://github.com/jackhumbert/agent-host-client-py"
pip install "agent-host-broker @ git+https://github.com/jackhumbert/agent-host-broker-py"
```

## Development

```bash
pip install -e '.[ws]'
pip install --group dev
pytest
mypy
ruff check . && ruff format --check .
lint-imports
```

## License

MIT - see [LICENSE](LICENSE).
