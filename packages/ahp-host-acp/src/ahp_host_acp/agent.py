"""`type = "acp"` in an ahp-node config.

    [[agents]]
    type = "acp"
    provider_id = "opencode"
    agent_name = "opencode"
    command = ["opencode", "acp"]
    model_command = "/model {model} -s"    # agents without ACP model support
    # launcher = ["docker", "run", "-i", "--rm", "-v", "{cwd}:{cwd}", "-w", "{cwd}", "img"]
    # env_allow = ["PATH", "HOME", "LANG"]  # host variables the agent may inherit
    # relay_command = ["python3", "-m", "ahp_host_acp.relay"]  # the relay inside the container
    # relay_host = "172.17.0.1"               # where the host listens for it

    [agents.env]
    OPENCODE_CONFIG = '~/.config/ahp/opencode.json'

    [agents.config_options]                 # fixed on every session
    thought_level = "low"

    [[agents.mcp_servers]]                  # given to the agent on every session
    name = "files"
    command = ["mcp-server-filesystem", "/Users/me/projects"]

    [[agents.models]]
    id = "ollama/glm-5.3-flash:cloud"
    name = "GLM 5.3 Flash"
    context_window = 1048576

The same settings as this package's own config file, less what now belongs to
the node (roots, port, bind, token, state). What the agent reports about
itself (its options, models and commands) is kept in the node's state
directory for this agent, `agent.json`. Registered as the `acp` entry in the
`ahp_host.agents` group.

`from_config` builds the same provider from Python, for a flow.toml (or any
embedder) with no node:

    [providers.opencode]
    load = "ahp_host_acp:from_config"
    config = "~/.config/ahp/opencode.toml"   # passed as config=Path(...)

or with the options inline, each key a keyword argument:

    [providers.opencode]
    load = "ahp_host_acp:from_config"

    [providers.opencode.config]
    roots = ["~/projects"]

The file is this package's own config file (server-only keys such as `port` in
it are ignored; as a keyword they are refused); keyword options override it.
flow.toml's loader hands `from_config` the host's `roots` (plus the runs folder)
and a `state_dir` unless the table sets them; setting `roots` in the table stops
that, and a worktree's working directory (under the runs folder) then falls
outside the roots.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ahp_host.node import NodeContext

from ahp_host_acp.config import (
    DEFAULT_AGENT_NAME,
    DEFAULT_PROVIDER_ID,
    DEFAULT_STATE,
    SERVER_ONLY_KEYS,
    ConfigError,
    _command,
    _env_allow,
    _launcher,
    _mcp_servers,
    _models,
    _option_values,
    _path,
    _policy,
    _relay_command,
    _relay_host,
    _roots_from_file,
    _strings,
    read_config_file,
    roots_option,
)
from ahp_host_acp.provider import (
    CATALOGUE_FILE,
    DEFAULT_DESCRIPTION,
    AcpProvider,
    AgentSpec,
    is_valid_provider_id,
)
from ahp_host_acp.roots import Roots

AGENT_OPTIONS = frozenset(
    {
        "provider_id",
        "agent_name",
        "description",
        "command",
        "launcher",
        "env_allow",
        "relay_command",
        "relay_host",
        "policy",
        "env",
        "models",
        "model_command",
        "config_options",
        "mcp_servers",
    }
)


def create(options: Mapping[str, Any], node: NodeContext) -> AcpProvider:
    return provider_from_options(options, node.roots, node.state_dir)


def provider_from_options(options: Mapping[str, Any], roots: Roots, state_dir: Path) -> AcpProvider:
    unknown = sorted(set(options) - AGENT_OPTIONS)
    if unknown:
        raise ConfigError(f"acp agent: unknown option(s): {', '.join(unknown)}")
    provider_id = str(options.get("provider_id", DEFAULT_PROVIDER_ID))
    if not is_valid_provider_id(provider_id):
        raise ConfigError(f"provider id {provider_id!r}: use letters, digits, '-' and '_'")
    model_command = options.get("model_command")
    if model_command is not None and (
        not isinstance(model_command, str) or "{model}" not in model_command
    ):
        raise ConfigError("model_command must be a string containing {model}")
    description = options.get("description")
    policy, client_methods = _policy(options)
    return AcpProvider(
        roots,
        AgentSpec(
            command=_command(None, options),
            launcher=_launcher(options),
            env_allow=_env_allow(options),
            relay_command=_relay_command(options),
            relay_host=_relay_host(options),
            policy=policy,
            client_methods=client_methods,
            env=_strings(options, "env"),
            model_command=model_command,
            config_options=_option_values(options),
            mcp_servers=_mcp_servers(options),
        ),
        display_name=str(options.get("agent_name", DEFAULT_AGENT_NAME)),
        models=_models(None, options),
        provider_id=provider_id,
        description=str(description) if description is not None else DEFAULT_DESCRIPTION,
        catalogue_file=state_dir / CATALOGUE_FILE,
    )


def from_config(
    config: str | os.PathLike[str] | None = None,
    *,
    roots: Roots
    | str
    | os.PathLike[str]
    | Sequence[str | os.PathLike[str]]
    | Mapping[str, str | os.PathLike[str]]
    | None = None,
    state_dir: str | os.PathLike[str] | None = None,
    **options: Any,
) -> AcpProvider:
    """A provider from a config file and/or keywords, with no node; keywords win."""
    data = read_config_file(Path(config).expanduser()) if config is not None else {}
    unknown = sorted(set(options) - AGENT_OPTIONS)
    if unknown:
        raise ConfigError(f"acp from_config: unknown option(s): {', '.join(unknown)}")
    # The file's server-only keys (port, token_file, ...) are ignored. As keywords they stay
    # unknown options and are refused above. What is left of the file, minus the node-level
    # keys (root, roots, state_dir) read below, is the per-agent options.
    from_file = {key: value for key, value in data.items() if key not in SERVER_ONLY_KEYS}
    merged = {key: value for key, value in from_file.items() if key in AGENT_OPTIONS}
    merged.update(options)
    served = roots_option(roots) if roots is not None else _roots_from_file(data)
    if served is None:
        raise ConfigError(
            "no folder to serve: pass roots=, or set root / [roots] in the config file"
        )
    for path in served.paths:
        if not path.is_dir():
            raise ConfigError(f"{path} is not a directory")
    if state_dir is not None:
        state = Path(state_dir).expanduser()
    elif "state_dir" in data:
        state = _path(data["state_dir"], "state_dir")
    else:
        state = DEFAULT_STATE
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    return provider_from_options(merged, served, state)
