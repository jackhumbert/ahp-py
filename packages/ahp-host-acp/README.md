# ahp-host-acp

Any [Agent Client Protocol](https://agentclientprotocol.com/) (ACP) agent as an
[Agent Host Protocol](https://github.com/jackhumbert/ahp-py/tree/main/packages/ahp-protocol)
(AHP) provider for [`ahp-host`](https://github.com/jackhumbert/ahp-py/tree/main/packages/ahp-host).

ACP is what editors such as Zed use to drive coding agents: the agent runs as
a subprocess and speaks JSON-RPC over stdio. This adapter is the editor's half.
Each AHP session starts the configured agent command (`openclaw acp`, or any
other ACP agent), and any AHP client (VS Code's Agent Sessions view, the Python
client, a gateway in front of several hosts) can then start and follow sessions
with it.

Status: pre-alpha. Tested against opencode (`opencode acp`) and OpenClaw's ACP
bridge (`openclaw acp`).

## What a client sees

- Streaming text and reasoning (`agent_message_chunk`, `agent_thought_chunk`).
- Tool calls as rows named for their ACP `kind` ("Run command", "Read",
  "Edit"...), with the agent's own one-line purpose under them while they run,
  their output as it arrives, and "Ran `echo hi`" or "Failed: ..." when done.
- **Approvals**: every `session/request_permission` the agent sends becomes an
  approval prompt. Approving picks the agent's *allow once* option, never
  *allow always*, so no approval outlives the call it was given for.
- A model picker: the config file's `[[models]]` (first is the default), or,
  without any, the agent's own list from its `model` config option, with the
  model it starts a session on first. An agent with a `model` session config
  option (opencode) is switched with `session/set_config_option`, one that
  reports ACP session models (an API ACP has since removed) with
  `session/set_model`, and one with neither (OpenClaw) with `model_command`, a
  prompt such as `/model {model} -s` sent as its own turn, whose reply is not
  shown.
- **Session config from the agent**: its ACP config options (`select` and
  `boolean`) or, for an agent without any, its session modes, as the
  session's config, in the agent's order and with its starting values as
  defaults. A client's choice, at creation or later, is sent with
  `session/set_config_option` (or `session/set_mode`); a change the agent makes
  itself (`config_option_update`, `current_mode_update`) is shown to every
  client, and so is a value the agent refuses, which is put back. Options set
  in the config file's `[config_options]` are shown read-only. The model
  option is left to the model picker when there is one.
- The session's **title** from the agent's `session_info_update`.
- The agent's **slash commands** (`available_commands_update`) as completions
  after a `/` at the start of a message.
- The agent's **plan** as one "Update plan" row per change, with the task list
  as its result ("Updated the plan: 2 of 5 done"), and the entry in progress
  as the session's activity.
- **The Changes view**: every file a completed tool call's `diff` edited, as
  the session's changeset ("Session changes", with +/- counts in the session
  list), from what the file was before the agent first touched it to what it
  is now. The diff is also summarised as text in the tool's result.
- A context gauge, from the agent's `usage_update` (or per-turn `usage`). The
  usage's `_meta.acpUsage` carries ACP's own `used`, `size` (the context
  window) and `cost`, and `size` becomes the model's context window in the
  picker when the config does not give one.
- MCP servers from the config file (`[[mcp_servers]]`), given to the agent on
  every session.
- Sessions that survive a host restart, through `session/resume` or
  `session/load` when the agent offers them.

What the agent reports about itself -- its options and starting values, its
models, its commands, each model's context window -- is kept in
`agent.json` in the state directory (an `ahp-node` agent's own state
directory). A session's config schema is fixed when it is created, and the
agent starts on a session's first turn, so the very first session an agent
ever has offers no config, and a model picker built from the agent's list
appears after the host next starts.

Not yet: attachments (only the message text is sent); each tool call's own
diff view (a `fileEdit` result; the Changes view has them meanwhile); the
agent's permission options beyond allow and reject once; a live model list
(it changes on restart); one ACP session per AHP chat (side chats share the
session's). Under `ahp-node`, slash commands complete only once the node
advertises `/` as a completion trigger, which it does not yet. The client
filesystem and terminal methods are deliberately not offered.

## Run it

```bash
pip install -e .
python -m ahp_host_acp --config ~/.config/ahp/openclaw.toml
```

### opencode

opencode on Ollama Cloud's GLM 5.3 Flash, through the local signed-in Ollama.
opencode keeps its own settings; point it at a file of its own for these
sessions with `OPENCODE_CONFIG`:

```jsonc
// ~/.config/ahp/opencode.json
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "ollama": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "Ollama",
      "options": { "baseURL": "http://127.0.0.1:11434/v1" },
      "models": {
        "glm-5.3-flash:cloud": { "name": "GLM 5.3 Flash", "tool_call": true, "reasoning": true }
      }
    }
  },
  "model": "ollama/glm-5.3-flash:cloud",
  "permission": { "edit": "ask", "bash": "ask", "webfetch": "ask" }
}
```

```toml
# ~/.config/ahp/opencode.toml
agent_name = "opencode (GLM 5.3 Flash)"
provider_id = "opencode"
port = 4323
token_file = "~/.config/ahp/opencode.token"
state_dir = "~/.local/state/ahp-host-acp/opencode"

command = ["opencode", "acp"]

[env]
OPENCODE_CONFIG = 'C:\Users\me\.config\ahp\opencode.json'

[[models]]
id = "ollama/glm-5.3-flash:cloud"
name = "GLM 5.3 Flash"
context_window = 1048576
vision = true

[roots]
llm = 'G:\llm'
```

opencode runs its tools in the session's folder and, with the `permission`
block above, asks before every edit, shell command and fetch.

### OpenClaw

OpenClaw on the same model, through the local signed-in Ollama:

```toml
agent_name = "OpenClaw (GLM 5.3 Flash)"
provider_id = "openclaw"
port = 4322
token_file = "~/.config/ahp/openclaw.token"
state_dir = "~/.local/state/ahp-host-acp/openclaw"

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
| `[config_options]` | ACP session config options (`session/set_config_option`), fixed on every session: a value id string, or `true`/`false` for a boolean option. Clients see them read-only and cannot change them. OpenClaw offers `thought_level`, `reasoning_level`, `verbose_level`, `elevated_level`, ... |
| `[[mcp_servers]]` | MCP servers for the agent to connect to: `name`, and `command` (a list, or one string split shell-style; its program is looked up on `PATH`) with an optional `[mcp_servers.env]` table; or `type = "http"` / `"sse"` with `url` and an optional `[mcp_servers.headers]` table, given only to an agent that says it takes that transport. |
| `[roots]` / `--root NAME=PATH` (repeatable) | Named folders. Clients browse (read-only) a small tree of them; sessions may start in any. |
| `root` / `--root PATH` | One unnamed folder, served as itself. |
| `token_file` / `--token-file` | Require this connection token. Read from a file so it never appears in `ps` or logs. |
| `port`, `bind` / `--port`, `--bind` | Default `127.0.0.1:4322` (4321 is the Claude host's). Loopback only. |
| `state_dir` / `--state-dir` | Persisted sessions, the sequence counter and `agent.json`. Default `~/.local/state/ahp-host-acp`. Give each host its own. |
| `agent_name`, `description` / `--agent-name` | What clients call the agent. |
| `provider_id` / `--provider-id` | The agent's id (default `acp`). Hosts behind one gateway with the same id are merged into one agent. |

Unknown settings are an error, so a typo cannot silently fall back to a
default.

## Security

**The folder a session starts in does not confine the agent** (any agent:
opencode at least works in it; OpenClaw does not). It is passed as
ACP's `cwd`, and it is where the agent process starts, but what the agent can
read, write and run is decided by the agent itself. OpenClaw, for example,
runs its tools in its own workspace (`~/.openclaw/workspace`) under its own
exec-approval policy, and asks (through `session/request_permission`) only
when that policy says to. Commands it considers safe run without a prompt.

So this host's approvals are exactly as strict as the agent's own. Configure
the agent's policy before exposing it (opencode: `permission` in its config;
OpenClaw: `openclaw approvals`, and `tools.exec` in its config). The agent runs as the host's OS user. Never bind
this off loopback without a proxy that authenticates peers.

**Clients can change the agent's own settings.** Every config option and mode
the agent reports is a session setting a client may change, and some loosen
the agent's policy -- an agent's "bypass permissions" mode, or an
auto-approve toggle, means it stops asking. To keep one fixed, set it in
`[config_options]`: it is then read-only for clients.

Two more things read or carry what an agent touches: the Changes view reads
the files a tool call's diff names back from disk, but only inside the served
folders (outside them it shows the agent's own text); and an MCP server's
`headers` (an `Authorization` header, say) sit in the config file in plain
text, so keep that file private.

## Development

```bash
python -m venv .venv
.venv/bin/pip install -e ../ahp-protocol -e "../ahp-host[ws]" -e .
.venv/bin/pip install --group dev
.venv/bin/pytest && .venv/bin/mypy && .venv/bin/ruff check . && .venv/bin/ruff format --check .
```

Tests drive `tests/fake_agent.py`, a scripted ACP agent, as a real subprocess.

## License

MIT - see [LICENSE](LICENSE).
