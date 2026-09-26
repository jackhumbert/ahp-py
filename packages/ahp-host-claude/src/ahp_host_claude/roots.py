"""The folders a host serves -- now `ahp_host.node.roots`, re-exported.

The folder tree belongs to the machine's node, not to one agent, so it moved to
ahp-host when a node learned to serve several agents.
"""

from ahp_host.node.roots import (
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
