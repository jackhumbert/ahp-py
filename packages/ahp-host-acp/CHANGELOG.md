# Changelog

## [Unreleased]

### Added

- `type = "acp"` for `agent-host-node` (agent-host-server): the package
  registers an `acp` entry in the `agent_host_server.agents` group, so one node
  can serve several ACP agents (goose, opencode) beside Claude from one host,
  port and folder tree. Options are the config file's per-agent settings:
  `provider_id`, `agent_name`, `description`, `command`, `env`, `models`,
  `model_command`, `config_options`.
- An ACP client provider: one agent process per session, streaming text,
  reasoning, tool calls and usage; permission requests as approval prompts
  (granted once, never "always"); session resume via `session/resume` or
  `session/load`.
- A model picker from the config file, switched with `session/set_model`, or
  with a `model_command` prompt for agents without ACP model support
  (OpenClaw: `/model {model} -s`).
- Models switched through a `model` session config option when the agent
  offers one (opencode).
- `[config_options]`: ACP session config options set on every session
  (OpenClaw: `thought_level`).
- A TOML config file and named roots, as in agent-host-server-claude.

### Changed

- `roots` and `paths` re-export `agent_host_server.node`'s instead of keeping
  copies. `python -m agent_host_server_acp` still runs a one-agent host.
