"""`type = "acp"` in an ahp-node config.

    [[agents]]
    type = "acp"
    provider_id = "opencode"
    agent_name = "opencode"
    command = ["opencode", "acp"]
    model_command = "/model {model} -s"    # agents without ACP model support

    [agents.env]
    OPENCODE_CONFIG = '~/.config/ahp/opencode.json'

    [agents.config_options]
    thought_level = "low"

    [[agents.models]]
    id = "ollama/glm-5.3-flash:cloud"
    name = "GLM 5.3 Flash"
    context_window = 1048576

The same settings as this package's own config file, less what now belongs to
the node (roots, port, bind, token, state). Registered as the `acp` entry in
the `ahp_host.agents` group.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ahp_host.node import NodeContext

from ahp_host_acp.config import (
    DEFAULT_AGENT_NAME,
    DEFAULT_PROVIDER_ID,
    ConfigError,
    _command,
    _models,
    _strings,
)
from ahp_host_acp.provider import (
    DEFAULT_DESCRIPTION,
    AcpProvider,
    AgentSpec,
    is_valid_provider_id,
)

_OPTIONS = frozenset(
    {
        "provider_id",
        "agent_name",
        "description",
        "command",
        "env",
        "models",
        "model_command",
        "config_options",
    }
)


def create(options: Mapping[str, Any], node: NodeContext) -> AcpProvider:
    unknown = sorted(set(options) - _OPTIONS)
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
    return AcpProvider(
        node.roots,
        AgentSpec(
            command=_command(None, options),
            env=_strings(options, "env"),
            model_command=model_command,
            config_options=_strings(options, "config_options"),
        ),
        display_name=str(options.get("agent_name", DEFAULT_AGENT_NAME)),
        models=_models(None, options),
        provider_id=provider_id,
        description=str(description) if description is not None else DEFAULT_DESCRIPTION,
    )
