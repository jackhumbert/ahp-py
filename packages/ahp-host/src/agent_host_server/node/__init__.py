"""A machine's node: every agent it runs, served from one AHP host.

`runner` is the process (`agent-host-node`); `config` reads its TOML; `roots`
is the folder tree it serves. Agent packages plug in through the
`agent_host_server.agents` entry-point group and receive a `NodeContext`.
"""

from agent_host_server.node.roots import NamedRootsResourceProvider, Roots, as_roots
from agent_host_server.node.runner import AGENTS_GROUP, NodeContext

__all__ = ["AGENTS_GROUP", "NamedRootsResourceProvider", "NodeContext", "Roots", "as_roots"]
