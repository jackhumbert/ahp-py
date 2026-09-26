"""The routing core.

Interfaces only at this layer's floor: what a node connection is, what a
session route is, and the aggregated namespace the surfaces see. Concrete
transports (`ws`) and the relay (`relay`) plug in from above; the registry
supplies node records and admission decisions from below.
"""

from agent_host_broker.core.broker import Authenticator, Broker, BrokerInfo
from agent_host_broker.core.node import (
    AhpNodeLink,
    NodeConnector,
    NodeLink,
    NodeRequestHandler,
    NodeUnavailableError,
    open_node_link,
)
from agent_host_broker.core.uris import (
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
    "Broker",
    "BrokerInfo",
    "ForeignUriError",
    "NodeConnector",
    "NodeLink",
    "NodeRequestHandler",
    "NodeUnavailableError",
    "is_virtual_root",
    "node_of",
    "open_node_link",
    "qualify_file_uris",
    "root_of",
    "unqualify_file_uris",
]
