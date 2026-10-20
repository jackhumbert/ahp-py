# ahp-py

Python implementations of the [Agent Host Protocol](https://github.com/microsoft/agent-host-protocol)
(AHP) — Microsoft's protocol for synchronized multi-client state over AI agent
sessions. One repository, several independently installable packages:

| Package | What it is |
|---|---|
| [`ahp-protocol`](packages/ahp-protocol) | Wire types, pure state reducers and upstream's conformance corpora. The shared floor; no runtime dependencies. |
| [`ahp-host`](packages/ahp-host) | The host: serves sessions to any AHP client (VS Code's Agent Sessions view, the Python client, the [iOS app][ios]). |
| [`ahp-host-claude`](packages/ahp-host-claude) | Claude, via the Claude Agent SDK, as a host provider. |
| [`ahp-host-acp`](packages/ahp-host-acp) | Any [Agent Client Protocol](https://agentclientprotocol.com/) agent as a host provider, plus a small reference agent (`ahp-acp-openai`) for any OpenAI-compatible endpoint. |
| [`ahp-acp`](packages/ahp-acp) | A plain Agent Client Protocol client: spawn an agent, speak JSON-RPC over stdio. Standard library only; imports nothing here. |
| [`ahp-client`](packages/ahp-client) | The client. |
| [`ahp-gateway`](packages/ahp-gateway) | One endpoint and one login in front of many hosts: an AHP host to clients, an AHP client to each node. |

```
                 ahp-protocol
               ▲      ▲      ▲
               │      │      │
        ahp-host  ahp-gateway  ahp-client
          ▲                  (host + client)
          │
  ahp-host-claude, ahp-host-acp ──▶ ahp-acp (standalone: imports nothing here)
```

## Support matrix

What each provider's session supports. Steering, Structured output, Client tools, Turn resume, Session resume and History truncation are read off the classes named (Session resume proves the host side: where the agent's own conversation may not survive, the cell says so); the other columns are stated from the code; `packages/ahp-host-flow/tests/docs/test_supportmatrix.py` fails when either disagrees with this table.

| Provider | Session | Steering | Structured output | Client tools | Host policy | Turn resume | Session resume | Fork | History truncation | Changesets | Usage | Cost | Reasoning stream |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| echo | `EchoSession` | yes | native | no | none | no | yes | transcript only | yes | demo | turn end (estimated) | no | no |
| ACP (any agent) | `AcpSession` | no | native | yes | advisory; hard with `host_tools` | no | yes (agent-native through `session/resume` or `session/load`, else host-side only) | native (`session/fork`), else as context | no | from tool diffs | turn end | if the agent reports it | yes |
| ACP (reference agent) | `AcpSession` | no | native | yes | advisory; hard with `host_tools` | no | host-side only (the agent starts a new conversation) | as context | no | from tool diffs | turn end | no | yes (`reasoning_content`) |
| Claude | `ClaudeSession` | yes | native | yes | hard | no | yes | native | no | from file edits | turn end; per request with `usage_per_message` | yes | yes |
| flow | `FlowSession` | yes | prompted | yes | `[policy]`, folded into workers' | yes | yes | transcript only | no | worktrees | per worker call | summed from workers | on worker chats |

native = the agent gets a StructuredOutput tool; prompted = the host asks and parses; advisory = the agent may not ask; hard = every call is checked.

### ACP agents known to work

| Agent | Command | In practice (as of 2026-10-19) |
|---|---|---|
| opencode | `opencode acp` | Text and reasoning stream; tools run in the session folder and ask per its `permission` config; the model is switched with `session/set_config_option`; structured output through the relay (`tests/test_live_structured.py`). |
| OpenClaw | `openclaw acp` | Text and reasoning stream; runs tools in its own workspace under its own exec policy (the session folder does not confine it) and asks only when that policy says to; the model is switched with `model_command`; needs the OpenClaw Gateway running. |
| reference agent | `ahp-acp-openai` | Any OpenAI-compatible `/v1/chat/completions` endpoint: text, reasoning, client tools and structured output through the relay, cancel, and one built-in file write, asked about first (through `fs/write_text_file` under `[policy] host_tools`); no fork, load or resume; usage at turn end; no cost. |

The native iPhone and iPad client, **Agent Host**, lives in its own repository:
[`ahp-client-ios`][ios]. It speaks the same spec revision and lists the
machines behind an `ahp-gateway`.

## Install

Distributed from GitHub, not from a package index. `ahp-protocol` is the floor
and installs first, because the others pin it and no index carries it:

```bash
pip install "ahp-protocol @ git+https://github.com/jackhumbert/ahp-py#subdirectory=packages/ahp-protocol"
pip install "ahp-host[ws] @ git+https://github.com/jackhumbert/ahp-py#subdirectory=packages/ahp-host"
```

Each package's README has its own install lines.

Which versions work together: [`COMPATIBILITY.md`](COMPATIBILITY.md).

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

[ios]: https://github.com/jackhumbert/ahp-client-ios
