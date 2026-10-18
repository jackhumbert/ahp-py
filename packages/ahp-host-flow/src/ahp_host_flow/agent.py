"""`type = "flow"` in an ahp-node config.

    [[agents]]
    type = "flow"
    config = "~/.config/ahp/flow.toml"
    user_workflows_dir = "~/.config/ahp/workflows"    # optional

The flow.toml is this package's own config file. When it sets no `state_dir`,
runs persist in the node's state directory for this agent. Registered as the
`flow` entry in the `ahp_host.agents` group.
"""

from __future__ import annotations

import dataclasses
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ahp_host.node import NodeContext

from ahp_host_flow.config import ConfigError, load_config
from ahp_host_flow.provider import FlowProvider

_OPTIONS = frozenset({"config", "user_workflows_dir"})


def create(options: Mapping[str, Any], node: NodeContext) -> FlowProvider:
    unknown = sorted(set(options) - _OPTIONS)
    if unknown:
        raise ConfigError(f"flow agent: unknown option(s): {', '.join(unknown)}")
    raw = options.get("config")
    if not isinstance(raw, str) or not raw:
        raise ConfigError("flow agent: config (a path to flow.toml) is required")
    path = Path(raw).expanduser()
    cfg = load_config(path)
    if "state_dir" not in tomllib.loads(path.read_text(encoding="utf-8-sig")):
        cfg = dataclasses.replace(cfg, state_dir=node.state_dir)
    workflows = options.get("user_workflows_dir")
    if workflows is None:
        return FlowProvider(cfg, roots=node.roots)
    return FlowProvider(cfg, roots=node.roots, user_workflows_dir=Path(str(workflows)).expanduser())
