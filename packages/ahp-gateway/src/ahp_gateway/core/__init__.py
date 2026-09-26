"""The routing core.

Interfaces only at this layer's floor: what a node connection is, what a
session route is, and the aggregated namespace the surfaces see. Concrete
transports (`ws`) and the relay (`relay`) plug in from above; the registry
supplies node records and admission decisions from below.
"""

from ahp_gateway.core.gateway import Authenticator, Gateway, GatewayInfo
from ahp_gateway.core.node import (
    AhpNodeLink,
    NodeConnector,
    NodeLink,
    NodeRequestHandler,
    NodeUnavailableError,
    open_node_link,
)
from ahp_gateway.core.orchestrator import Orchestrator, OrchestratorConfig
from ahp_gateway.core.uris import (
    SCHEME,
    VIRTUAL_ROOT,
    ForeignUriError,
    is_virtual_root,
    node_of,
    qualify_file_uris,
    root_of,
    unqualify_file_uris,
)

__all__ = [
    "SCHEME",
    "VIRTUAL_ROOT",
    "AhpNodeLink",
    "Authenticator",
    "ForeignUriError",
    "Gateway",
    "GatewayInfo",
    "NodeConnector",
    "NodeLink",
    "NodeRequestHandler",
    "NodeUnavailableError",
    "Orchestrator",
    "OrchestratorConfig",
    "is_virtual_root",
    "node_of",
    "open_node_link",
    "qualify_file_uris",
    "root_of",
    "unqualify_file_uris",
]
