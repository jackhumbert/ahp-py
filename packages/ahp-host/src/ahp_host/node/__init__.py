"""A machine's node: every agent it runs, served from one AHP host.

`runner` is the process (`ahp-node`); `config` reads its TOML; `roots`
is the folder tree it serves. Agent packages plug in through the
`ahp_host.agents` entry-point group and receive a `NodeContext`.
"""

from ahp_host.node.roots import NamedRootsResourceProvider, Roots, as_roots
from ahp_host.node.runner import AGENTS_GROUP, NodeContext

__all__ = ["AGENTS_GROUP", "NamedRootsResourceProvider", "NodeContext", "Roots", "as_roots"]
