# ahp-gateway

A federated gateway for the [Agent Host Protocol](https://github.com/jackhumbert/ahp-py/tree/main/packages/ahp-protocol)
(AHP): one endpoint and one login behind which a developer sees a single flat
"my sessions everywhere" view across every box that runs an agent for them.

It is an AHP **host** facing the surfaces (web, Windows, CLI) and an AHP
**client** facing each node. Nodes implement the AHP server only; surfaces
implement the AHP client only; the gateway implements both, and is the only
component that does. The data plane is AHP, unmodified - a stock AHP client
still works against a bare node with no fleet in sight.

It is the newest member of the family:

```
                ahp-protocol              ← the shared layer
                  ▲        ▲         ▲
                  │        │         │
        ahp-host │ ahp-client
                  ▲       │       ▲        ← the gateway is BOTH
                  └───────┴───────┘
                    ahp-gateway       ← this repo
```

Status: pre-alpha. The multiplexer (build-order unit 1) works end to end:
stock clients through the gateway to stock hosts, in-process and over
WebSocket. A surface sees one session list, one agent list (a shared agent
offering only what every machine running it can do), one automation catalogue
and one telemetry stream per OTel signal, and every node's files, chats and
diff content route back to the machine they live on.
[`docs/plan.md`](docs/plan.md) is the design; §9 records what is built and
what is not.

## Install

The family is distributed from GitHub, not from an index. The protocol package
is the floor and installs first:

```bash
pip install "ahp-protocol @ git+https://github.com/jackhumbert/ahp-py#subdirectory=packages/ahp-protocol"
pip install "ahp-host @ git+https://github.com/jackhumbert/ahp-py#subdirectory=packages/ahp-host"
pip install "ahp-client @ git+https://github.com/jackhumbert/ahp-py#subdirectory=packages/ahp-client"
pip install "ahp-gateway @ git+https://github.com/jackhumbert/ahp-py#subdirectory=packages/ahp-gateway"
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
