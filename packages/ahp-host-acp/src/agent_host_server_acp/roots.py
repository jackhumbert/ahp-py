"""The folders a host serves -- now `agent_host_server.node.roots`, re-exported.

The folder tree belongs to the machine's node, not to one agent, so it moved to
agent-host-server when a node learned to serve several agents.
"""

from agent_host_server.node.roots import (
    NamedRootsResourceProvider,
    Roots,
    as_roots,
    is_valid_root_name,
    parse_root_arg,
)

__all__ = [
    "NamedRootsResourceProvider",
    "Roots",
    "as_roots",
    "is_valid_root_name",
    "parse_root_arg",
]
