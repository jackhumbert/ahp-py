"""A host/server implementation of the Agent Host Protocol (AHP) for Python.

The names an embedder needs are re-exported here, so the first line of a host
is `from agent_host_server import Host, Policy` rather than a tour of the
package layout. Everything else stays where it is: `agent_host_server.core` for
the runtime internals, `agent_host_server.provider` for the adapter surface.

    from agent_host_server import AgentProvider, Host, LoopbackSingleUserPolicy
    from agent_host_protocol.transport import memory_pair

Nothing was exported here at all until it was noticed that
`from agent_host_server import Host` -- the first thing anyone types -- raised
ImportError against an installed wheel.
"""

from __future__ import annotations

from agent_host_server.core import (
    DEFAULT_SUPPORTED_VERSIONS,
    ROOT_URI,
    AhpError,
    ConnectionInfo,
    Host,
    HostInfo,
    LoopbackSingleUserPolicy,
    Policy,
)
from agent_host_server.provider.base import (
    AgentInfo,
    AgentProvider,
    AgentSession,
    AgentSessionContext,
    ModelInfo,
    ModelSelection,
    TurnSink,
    UserMessage,
)

#: The single source of truth. `pyproject.toml` reads it from here through
#: hatchling's dynamic version, so there is no second place to forget.
__version__ = "0.1.0"

__all__ = [
    "DEFAULT_SUPPORTED_VERSIONS",
    "ROOT_URI",
    "AgentInfo",
    "AgentProvider",
    "AgentSession",
    "AgentSessionContext",
    "AhpError",
    "ConnectionInfo",
    "Host",
    "HostInfo",
    "LoopbackSingleUserPolicy",
    "ModelInfo",
    "ModelSelection",
    "Policy",
    "TurnSink",
    "UserMessage",
    "__version__",
]
