# agent-host-server-claude

Claude as an [Agent Host Protocol](https://github.com/jackhumbert/agent-host-protocol-py)
(AHP) provider for [`agent-host-server`](https://github.com/jackhumbert/agent-host-server-py).

It wraps the [Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk):
Claude Code, running on this machine as the user who starts the host, with its
built-in tools (read, search, edit, shell, web). Any AHP client - VS Code's
Agent Sessions view, the Python client, a broker in front of several hosts -
can then start and follow Claude sessions here.

Status: pre-alpha.

## What a client sees

- Streaming text and reasoning, tool calls as rows, token usage.
- **Approvals.** Reading and searching run freely. Every edit, shell command
  and web request is put to the user first, through the protocol's tool
  confirmation (an approval prompt in VS Code). This holds even if the user's
  own Claude Code settings allow a tool: the gate is a `PreToolUse` hook, which
  runs before those settings are consulted.
- A model picker populated from Claude Code itself at start-up (the same list
  `/model` shows for the logged-in account, its default first), so new models
  appear without a release of this adapter.
- Attachments: a referenced file or folder is handed to Claude as its path
  (with the selected lines, if any); a pasted image or PDF is sent as an image
  or document block; pasted text is inlined (capped at 200k characters).
  Chat and annotation references are named but not resolved yet.
- Sessions that survive a host restart: the Agent SDK's session id is the
  host's resume state.

## Run it

```bash
pip install -e .
python -m agent_host_server_claude --root ~/Github --token-file ~/.config/agent-host/node.token
```

| Flag | Meaning |
|---|---|
| `--root DIR` | Required. Sessions must work inside it; clients may browse it (read-only) to pick a folder. |
| `--token-file PATH` | Require this connection token. Read from a file so it never appears in `ps` or logs. |
| `--port`, `--bind` | Default `127.0.0.1:4321`. Loopback only. |
| `--state-dir DIR` | Persisted sessions and sequence counter. Default `~/.local/state/agent-host-server-claude`. |
| `--agent-name NAME` | What clients call the agent. |

Authentication is Claude Code's own: the SDK uses whatever login `claude` has
on this machine (or `ANTHROPIC_API_KEY` if set).

## Security

The agent runs as the host's OS user, so whatever that user can touch, an
approved tool call can touch. The approval gate and the `--root` check are the
boundary; see `agent-host-server`'s SECURITY.md for the host's own posture.
Never bind this off loopback without a proxy that authenticates peers.

## Development

```bash
python -m venv .venv
.venv/bin/pip install -e . && .venv/bin/pip install --group dev
.venv/bin/pytest && .venv/bin/mypy && .venv/bin/ruff check . && .venv/bin/ruff format --check .
```

## License

MIT - see [LICENSE](LICENSE).
