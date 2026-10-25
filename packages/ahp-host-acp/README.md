# ahp-host-acp

Any [Agent Client Protocol](https://agentclientprotocol.com/) (ACP) agent as an
[Agent Host Protocol](https://github.com/jackhumbert/ahp-py/tree/main/packages/ahp-protocol)
(AHP) provider for [`ahp-host`](https://github.com/jackhumbert/ahp-py/tree/main/packages/ahp-host).

ACP is what editors such as Zed use to drive coding agents: the agent runs as
a subprocess and speaks JSON-RPC over stdio. This adapter is the editor's half,
built on [`ahp-acp`](../ahp-acp), the plain ACP client.
Each AHP session starts the configured agent command (`openclaw acp`, or any
other ACP agent), and any AHP client (VS Code's Agent Sessions view, the Python
client, a gateway in front of several hosts) can then start and follow sessions
with it.

Status: pre-alpha. Tested against opencode (`opencode acp`), OpenClaw's ACP
bridge (`openclaw acp`) and the package's own reference agent (`ahp-acp-openai`).

## What a client sees

- Streaming text and reasoning (`agent_message_chunk`, `agent_thought_chunk`).
- Tool calls as rows named for their ACP `kind` ("Run command", "Read",
  "Edit"...), with the agent's own one-line purpose under them while they run,
  their output as it arrives, and "Ran `echo hi`" or "Failed: ..." when done.
- **Approvals**: every `session/request_permission` the agent sends becomes an
  approval prompt offering the agent's own choices, worded as the agent words
  them ("Allow once", "Always allow", "Reject"...), and the agent gets exactly
  the one the user picks. A plain approve or deny means *once*. A call that
  shows a diff is previewed in the prompt, so the change can be read first.
- **Chats**: every chat in a session is its own conversation, with its own
  ACP session in the session's agent process (`multipleChats`). A chat can be
  forked when the agent can fork a session (ACP's `session/fork`), at its
  latest turn; stopping a chat cancels only that chat, and closing one closes
  its ACP session when the agent offers `session/close`.
- **Forked sessions** (`createSession.fork`): under the same rule, the new
  session's conversation is the agent's own fork (`session/fork`, reopened
  with `session/resume` or `session/load`). When that cannot be done -- the
  agent cannot fork, the source is another agent's or not running, or not at
  that turn -- the session starts fresh, its first message carries the
  copied transcript as context, and a system notification says so.
- **Attachments**, as the agent's `promptCapabilities` allow: a served file
  embedded (small text files, with `embeddedContext`) or linked to its real
  path (`resource_link`), a web link as a link, a selection named in words,
  inline images and audio, other inline data embedded, and an attached chat's
  transcript (as the host resolved it) as context. What the agent cannot take
  is left out, with a log line, rather than failing the turn.
- A model picker: the config file's `[[models]]` (first is the default), or,
  without any, the agent's own list from its `model` config option, with the
  model it starts a session on first. It updates as soon as the agent reports
  a new list. An agent with a `model` session config
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
  option is left to the model picker when there is one. The config is the
  session's, so every chat's ACP session is kept at the same values.
- The session's **title** from the default chat's `session_info_update`.
- The agent's **slash commands** (`available_commands_update`) as completions
  after a `/` at the start of a message.
- The agent's **plan** as one "Update plan" row per change, with the task list
  as its result ("Updated the plan: 2 of 5 done"), and the entry in progress
  as the session's activity.
- **Diffs**: a completed edit's result is a `fileEdit` per file -- the diff a
  client renders in the call's row, from the file before that call to after
  it. A diff whose file could not be read back keeps a text summary instead.
- **The Changes view**: every file a completed tool call's `diff` edited, in
  any chat, as the session's changeset ("Session changes", with +/- counts in
  the session list), from what the file was before the agent first touched it
  to what it is now.
- A context gauge, from the agent's `usage_update` (or per-turn `usage`). The
  usage's `_meta.acpUsage` carries ACP's own `used`, `size` (the context
  window) and `cost`, and `size` becomes the model's context window in the
  picker when the config does not give one.
- MCP servers from the config file (`[[mcp_servers]]`), given to the agent on
  every session.
- Sessions that survive a host restart, through `session/resume` or
  `session/load` when the agent offers them.

What the agent reports about itself -- its options and starting values, its
models, its commands, each model's context window, whether it can fork -- is
kept in `agent.json` in the state directory (an `ahp-node` agent's own state
directory), and the agent's entry on the root channel follows it as it
changes (`root/agentsChanged`). A session's config schema, though, is fixed
when the session is created, and AHP has no way to add to it later; the agent
starts on a session's first turn, so the very first session an agent ever has
offers no config. Every session after it does.

Side chats are opt-in (see below). Not yet: annotation attachments (they live on a host channel this
adapter does not read); telling the agent *why* a call was declined -- ACP's
permission answer has no field for a reason or a suggestion, so they stay in
the transcript. The client filesystem and terminal methods are offered only
with `[policy] host_tools = true` (see Security).

### Side chats

A client (VS Code, for one) offers "side chat" only when the agent advertises
`capabilities.multipleChats.sideChat`, and then sends `createChat` with a
`sideChat` source: the chat, the turn, and optionally the selected text. ACP
cannot give one session another's conversation, so this is off by default (no
capability, and the request is refused). Turn it on with
`AgentSpec(..., side_chat=SideChatMode())`. A side chat is then an ordinary ACP
session of its own, with its own turns; it never writes into the source chat.

It is seeded once, on its first prompt. The built-in seed puts a text block
ahead of that prompt: the source conversation through the named turn (whole
turns, newest kept, at most `SideChatMode.max_chars` characters, saying how
many were left out) and the selection, framed as reference material and not
as instructions. `SideChatMode(preamble=False)` turns that off.

To seed it another way (say, an agent that can fork its own session), subclass
`AcpSession`, override `async def side_chat_seed(self, origin: SideChatOrigin)
-> SideChatSeed | None`, and set `AcpProvider.session_class` to it. `origin`
has the source chat URI, its ACP session id (if open here), the turn id, the
selection as sent, and the source's turns through that turn. The returned
`SideChatSeed(meta=..., preamble=...)` adds `meta` as `_meta` to the side
chat's `session/new` and first `session/prompt`, and/or `preamble` as the text
block before the first prompt. The seed is saved with the chat until that
prompt is delivered, so a restart in between keeps it; it is applied once.

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
| `token_file` / `--token-file` | Require this connection token. Read from a file so it never appears in `ps` or logs. A client sends it as `?tkn=` or as an `Authorization: Bearer` header (the header stays out of a proxy's access log). |
| `port`, `bind` / `--port`, `--bind` | Default `127.0.0.1:4322` (4321 is the Claude host's). Any non-loopback `bind` (`0.0.0.0` inside a container, say) needs `token_file`, is refused without one before anything starts, and then accepts remote connections. |
| `state_dir` / `--state-dir` | Persisted sessions, the sequence counter and `agent.json`. Default `~/.local/state/ahp-host-acp`. Give each host its own. |
| `agent_name`, `description` / `--agent-name` | What clients call the agent. |
| `provider_id` / `--provider-id` | The agent's id (default `acp`). Hosts behind one gateway with the same id are merged into one agent. |

Unknown settings are an error, so a typo cannot silently fall back to a
default.

## Running the agent in a container

`launcher` runs the agent command behind a wrapper, typically `docker run`:

```toml
command = ["opencode", "acp"]
launcher = ["docker", "run", "-i", "--rm", "-v", "{cwd}:{cwd}", "-w", "{cwd}", "img"]
env_allow = ["PATH", "HOME", "LANG"]
```

The launcher's words are put in front of `command`, so the process started is
`docker run ... img opencode acp`. In the launcher, `{cwd}` is the session's
working directory, and `{uid}` and `{gid}` are the host user's ids (POSIX only;
a launcher using them is refused on Windows). Only these three are replaced;
other braces are left alone. Only the launcher's program is looked up on the
host's PATH: the agent command after it belongs to the container and is never
resolved on the host.

`env_allow` lists the host variables (exact names, or globs such as `M14_*`)
the agent may inherit; `[env]` is added on top. Setting it is recommended:
left unset, the agent inherits the host's whole environment, API keys and
tokens included. A glob never matches a secret-shaped name (`*_TOKEN`,
`*_KEY`, `*SECRET*`, ...); name such a variable exactly to pass it. A
container does not inherit the launcher's environment anyway, so to forward an
allowed variable into it put `-e NAME` in the launcher. (The client-tool
relay's token needs no `-e`: it is in the MCP entry's `env`, which the agent
hands to the relay it starts inside the container.)

With `env_allow` set the agent is started by `ahp_acp`'s
`AcpConnection.spawn(inherit_env=False)`, in its own process group, with
exactly the filtered variables plus `[env]`; unset, `inherit_env=True` keeps
the whole environment. This package passes no `user`, `group` or `umask`, so the
agent (or the launcher) runs as the host user. An embedder that calls
`AcpConnection.spawn(user=...)` itself with a `child_env` result should know
the filtered `HOME`, `USER` and `LOGNAME` are still the host user's: set the
target user's in `env` or leave them out of the allowlist. Inside a container,
drop privileges in the launcher (`docker run --user {uid}:{gid} ...`).

`relay_command` and `relay_host` let the agent's client tools work from inside
the container. The tools are served by a small relay the agent starts; by
default that is this host's Python, which the container does not have. Install
the relay in the image and name it, and give the address the host listens on:

```toml
relay_command = ["python3", "-m", "ahp_host_acp.relay"]
relay_host = "172.17.0.1"
```

`relay_host` is an address the host can bind and the container can reach (for
example the docker bridge gateway, `172.17.0.1`, or a name both sides
resolve). The bridge listens there only when it is set; otherwise it listens on
loopback. `relay_command` is not resolved on the host. The relay's token
travels in the MCP entry's environment, never argv, so the launcher needs no
`-e` for it.

The supported path mapping is identity: mount each root at the same path
(`-v {cwd}:{cwd}`), so paths the agent reports are paths on the host. A path
map is not supported.

## Loading from Python (from_config)

`ahp_host_acp.from_config` builds an `AcpProvider` without a node, for a
flow.toml `load = "ahp_host_acp:from_config"` entry or any embedder. It is
synchronous and starts no process (the agent is spawned per session).

```python
from ahp_host_acp import from_config

provider = from_config("~/.config/ahp/opencode.toml", roots=["~/projects"])
provider = from_config(roots={"work": "~/work"}, command=["opencode", "acp"])
```

The file is the config file described under Run it; `port`, `bind`,
`token_file` and `verbose` are ignored in it (as keywords they are refused, like
any unknown option). Keyword options (`provider_id`,
`agent_name`, `description`, `command`, `launcher`, `env_allow`, `relay_command`, `relay_host`, `policy`, `decline_feedback`, `env`, `models`, `model_command`,
`config_options`, `mcp_servers`) override the file, `roots=` (a path, a list of
paths or a name -> path table) overrides its root, and `state_dir=` its
state directory.

## Client tools

Tools a client publishes on its session (`activeClient.tools`) reach the agent
as an MCP server named `ahp_client`, added last to the session's `mcpServers`.
It is a small stdio relay (`python -m ahp_host_acp.relay`) the agent launches,
which talks back to this host over a loopback socket authenticated with a
per-chat random token; each call is run by the client through the host, and
shows as the client's own tool call. ACP has no request to change a live
session's `mcpServers`, so the server is fixed when the chat's session is
opened and only its tool list moves: the relay answers every `tools/list` from
the current state and sends `notifications/tools/list_changed` when it
changes. An agent that caches its tools and ignores `list_changed` keeps the
set it first listed.

## Extension requests from the agent

An ACP agent may send requests in the `_`-prefixed extension namespace (for example
`_vendor/workspace`) to its client. An embedder answers them with `ext_handlers`, a
mapping of method name to an async handler, set on `AgentSpec` or passed to `from_config`
(Python only, not a config-file key):

    async def workspace(ctx: ExtRequestContext, params: dict) -> dict:
        path = ctx.resolve_path(params["path"])   # AcpError if outside the granted folders
        return {"name": path.name, "cwd": str(ctx.cwd)}

    provider = from_config(roots=["~/projects"], ext_handlers={"_vendor/workspace": workspace})

The context has `method`, `session_id`, `cwd`, `roots`, `policy`, `resolve_path` (the
jail `fs/*` is served under; use it rather than your own) and `log`. A handler that raises
is answered with a generic error (`extension request failed`) and the exception is only
logged. A `_` method with no handler is refused as not found, as before.

## The reference agent

The package ships a small ACP *agent*, written with the standard library only,
that talks to any OpenAI-compatible `/v1/chat/completions` endpoint (Ollama at
`http://localhost:11434/v1`, for one). It is the non-Claude agent the support
matrix is checked against. Run it as `ahp-acp-openai` or
`python -m ahp_host_acp.reference`; it is configured from the environment:

| Variable | Meaning |
| --- | --- |
| `AHP_OPENAI_BASE_URL` | The endpoint's base URL, e.g. `http://localhost:11434/v1` |
| `AHP_OPENAI_MODEL` | The model name sent with every request |
| `AHP_OPENAI_API_KEY` | Bearer key, if the endpoint wants one; environment only, never argv, never logged |
| `AHP_OPENAI_TIMEOUT` | Seconds to wait on the endpoint (300) |
| `AHP_OPENAI_MAX_STEPS` | Model calls per turn before it stops (25) |
| `AHP_OPENAI_SYSTEM` | Replaces the default system prompt |

```toml
command = ["ahp-acp-openai"]

[env]
AHP_OPENAI_BASE_URL = "http://localhost:11434/v1"
AHP_OPENAI_MODEL = "qwen3"
# The key, if any, stays in the host's own environment (AHP_OPENAI_API_KEY).
```

It streams text and reasoning (`reasoning_content` or `reasoning`), offers the
client-tool relay's tools to the model as OpenAI tools (so `StructuredOutput`
works), cancels a turn by dropping the request, reports token usage at the end
of a turn, and has one built-in tool, `write_file`, which goes through
`fs/write_text_file` when `[policy] host_tools` offers it. It asks
`session/request_permission` before every tool call, so the host's policy always
runs.

It does not load, resume or fork sessions: after a host restart the AHP session
and its transcript come back (host-side resume), but the agent starts a new
conversation. It does not take images, has no HTTP proxy support and reports no cost. A `429` from the endpoint ends the turn with
an error whose text says it was rate limited (`429`).

Its tests use a packaged loopback stub, never a real endpoint. To try a live
one: `AHP_OPENAI_BASE_URL=... AHP_OPENAI_MODEL=... uv run --no-sync pytest tests/test_refhosted_live.py`.

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
OpenClaw: `openclaw approvals`, and `tools.exec` in its config). The agent runs as the host's OS user. Bind it
off loopback only with a token file, and only where the network in between is yours (a
container's published port bound to `127.0.0.1` on the outside, or a proxy that
authenticates peers): the token is the only thing between a peer and the agent.

**An approval can outlive its call, if the user says so.** The prompt offers
the agent's own options, *always allow* among them when the agent has one,
and picking it widens the agent's policy in the agent's own state, where this
host can neither see nor undo it. Nothing here ever picks it for the user: a
plain approval (a client that shows no options) is *allow once*, and if the
agent offers no such option the call is not allowed at all.

**Calls to a client's tools are allowed once, without asking.** A tool a
client offers (see Client tools) runs in that client, under its own
permissions, and not on this machine, so its owner decides whether it runs;
asking the user here as well would ask twice. The agent's *allow once* option
is picked for such a call, never *always*; if the agent offers no *allow once*,
the user is asked as for any call. A call counts as one of these when its title
names a relayed tool. An agent could title another call to match, but an ACP
agent already decides which calls it asks about, so this grants it nothing it
could not take by not asking.

**A host tool policy is advisory here.** `[policy]` (the `ahp-host` tool
policy: `deny_commands`, `deny_paths`, `ask_paths`, `allow_paths_outside_roots`,
`check`) is applied to each `session/request_permission` before anything else,
the relayed-tool shortcut included. A denied call is answered with the agent's
reject option, picked without a prompt; a call the policy marks *ask* is always
put to the user. Both the policy and the relayed-tool check judge the call as
the agent has described it so far (its `tool_call` and updates merged with the
request), not the request alone, and a call with no `kind` gets the one its
input shows (a `command` is a shell call). ACP's answer has no reason field, so the agent first sees only
a rejection; the user sees the reason, under the call's name and in its result.
With `decline_feedback = true` (the default; a top-level key, also an `acp`
agent option) the reasons of declined calls (the policy's, or what the user
typed, with any suggestion) are sent to the agent as one text block at the end
of the chat's next prompt, e.g. `[declined: Edit notes] use the other file`
(each reason capped at 300 characters). Set it to `false` to send nothing.
Confirmations also carry `read_only` (true for `read` and `search` calls) and
`policy_asked` (the policy marked the call *ask*) for clients that answer
automatically, plus `locations` (the call's `toolCall.locations` paths). The
`read_only` hint comes from the call's `kind`, which the agent chooses, so it is
marked `read_only_from_agent = true`: an agent can label any call a read, and an
automatic answerer should not approve on that alone (ahp-host-flow's
`[approvals]` rules do so only with `trust_agent_hints = true`). The policy can only judge calls the agent asks
about: an agent in its own allow-always mode never asks, so it is not bound.
Hard enforcement is `[policy] host_tools = true`, below.

```toml
[policy]
deny_commands = ["git push*"]
deny_paths = ["**/.env"]
```

**The hard path: `[policy] host_tools = true`.** The agent is told, at
`initialize`, that the host serves `fs/read_text_file`, `fs/write_text_file`
and the `terminal/*` methods, and an agent that uses them has its reads,
writes and commands run by the host. Each request is checked against the
roots jail first (a path or terminal `cwd` outside the served folders is
refused, even with `allow_paths_outside_roots`) and then against the policy:
a denial is a JSON-RPC error whose message is the policy's reason, so the
agent can show it to its model; an *ask* is put to the user in the turn.
Terminal output is capped at 1 MiB (the agent's `outputByteLimit` may lower
it). Each terminal command starts in its own process group (POSIX: its own
session), and `terminal/kill`, `terminal/release` and the session's end kill
the whole group, so what it started goes too (on Windows, `taskkill /T` while
the command still runs). A command's words are judged as paths as well, so
`cat .env` is denied by `**/.env`; that is best effort over the command text
(see `ahp-host`'s README for what it cannot see). An agent that ignores these methods and uses its own file and shell tools
is contained only by the launcher and `env_allow` sandbox, so pair them.

```toml
[policy]
host_tools = true
deny_commands = ["git push*"]
```

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
