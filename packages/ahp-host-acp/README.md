# agent-host-server-acp

Any [Agent Client Protocol](https://agentclientprotocol.com/) (ACP) agent as an
[Agent Host Protocol](https://github.com/jackhumbert/agent-host-protocol-py)
(AHP) provider for [`agent-host-server`](https://github.com/jackhumbert/agent-host-server-py).

ACP is what editors such as Zed use to drive coding agents: the agent runs as
a subprocess and speaks JSON-RPC over stdio. This adapter is the editor's half.
Each AHP session starts the configured agent command (`openclaw acp`, or any
other ACP agent), and any AHP client (VS Code's Agent Sessions view, the Python
client, a broker in front of several hosts) can then start and follow sessions
with it.

Status: pre-alpha. Built and tested against OpenClaw's ACP bridge.

## What a client sees

- Streaming text and reasoning (`agent_message_chunk`, `agent_thought_chunk`).
- Tool calls as rows named for their ACP `kind` ("Run command", "Read",
  "Edit"...), with the agent's own one-line purpose under them while they run,
  their output as it arrives, and "Ran `echo hi`" or "Failed: ..." when done.
- **Approvals**: every `session/request_permission` the agent sends becomes an
  approval prompt. Approving picks the agent's *allow once* option, never
  *allow always*, so no approval outlives the call it was given for.
- A model picker from the config file (`[[models]]`, first is the default).
  An agent that supports ACP session models is switched with
  `session/set_model`; one that does not (OpenClaw) is switched with
  `model_command`, a prompt such as `/model {model} -s` sent as its own turn,
  whose reply is not shown.
- A context gauge, from the agent's `usage_update` (or per-turn `usage`).
- Sessions that survive a host restart, through `session/resume` or
  `session/load` when the agent offers them.

Not yet: attachments (only the message text is sent), ACP session modes,
per-session config options chosen by the client (they are fixed in the config
file), plans, slash-command completion. The client filesystem and terminal
methods are deliberately not offered.

## Run it

```bash
pip install -e .
python -m agent_host_server_acp --config ~/.config/agent-host/openclaw.toml
```

OpenClaw on Ollama Cloud's GLM 5.3 Flash, through the local signed-in Ollama:

```toml
agent_name = "OpenClaw (GLM 5.3 Flash)"
provider_id = "openclaw"
port = 4322
token_file = "~/.config/agent-host/openclaw.token"
state_dir = "~/.local/state/agent-host-server-acp/openclaw"

command = ["openclaw", "acp"]
model_command = "/model {model} -s"

[env]
OPENCLAW_HIDE_BANNER = "1"
OPENCLAW_SUPPRESS_NOTES = "1"

# GLM 5.3 Flash via Ollama with thinking off still reasons, into the reply
# (then "</think>"); asking for thinking keeps reply and reasoning apart.
[config_options]
thought_level = "low"

[[models]]
id = "ollama/glm-5.3-flash:cloud"
name = "GLM 5.3 Flash"
context_window = 1048576
vision = true

[roots]
llm = 'G:\llm'
```

`openclaw acp` needs the OpenClaw Gateway running (`openclaw gateway status`).
The model id is OpenClaw's model ref: `ollama/<name>:cloud` goes through the
local Ollama (signed in to Ollama's cloud), `ollama-cloud/<name>` straight to
ollama.com with `OLLAMA_API_KEY`.

| Setting / flag | Meaning |
|---|---|
| `command` / `--command` | The ACP agent to run, as a list (or one string, split shell-style). Required. |
| `[env]` | Extra environment for the agent. |
| `[[models]]` / `--model ID` (repeatable) | `id`, `name`, `context_window`, `vision`. The first is each new session's default. None: no picker, the agent's own default. |
| `model_command` / `--model-command` | Prompt template switching the model, `{model}` standing for the id. For agents without ACP model support. |
| `[config_options]` | ACP session config options (`session/set_config_option`), set on every session. OpenClaw offers `thought_level`, `reasoning_level`, `verbose_level`, `elevated_level`, ... |
| `[roots]` / `--root NAME=PATH` (repeatable) | Named folders. Clients browse (read-only) a small tree of them; sessions may start in any. |
| `root` / `--root PATH` | One unnamed folder, served as itself. |
| `token_file` / `--token-file` | Require this connection token. Read from a file so it never appears in `ps` or logs. |
| `port`, `bind` / `--port`, `--bind` | Default `127.0.0.1:4322` (4321 is the Claude host's). Loopback only. |
| `state_dir` / `--state-dir` | Persisted sessions and sequence counter. Default `~/.local/state/agent-host-server-acp`. Give each host its own. |
| `agent_name`, `description` / `--agent-name` | What clients call the agent. |
| `provider_id` / `--provider-id` | The agent's id (default `acp`). Hosts behind one broker with the same id are merged into one agent. |

Unknown settings are an error, so a typo cannot silently fall back to a
default.

## Security

**The folder a session starts in does not confine the agent.** It is passed as
ACP's `cwd`, and it is where the agent process starts, but what the agent can
read, write and run is decided by the agent itself. OpenClaw, for example,
runs its tools in its own workspace (`~/.openclaw/workspace`) under its own
exec-approval policy, and asks (through `session/request_permission`) only
when that policy says to. Commands it considers safe run without a prompt.

So this host's approvals are exactly as strict as the agent's own. Configure
the agent's policy before exposing it (OpenClaw: `openclaw approvals`, and
`tools.exec` in its config). The agent runs as the host's OS user. Never bind
this off loopback without a proxy that authenticates peers.

## Development

```bash
python -m venv .venv
.venv/bin/pip install -e ../agent-host-protocol-py -e "../agent-host-server-py[ws]" -e .
.venv/bin/pip install --group dev
.venv/bin/pytest && .venv/bin/mypy && .venv/bin/ruff check . && .venv/bin/ruff format --check .
```

Tests drive `tests/fake_agent.py`, a scripted ACP agent, as a real subprocess.

## License

MIT - see [LICENSE](LICENSE).
