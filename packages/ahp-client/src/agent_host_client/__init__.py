"""A Python client for the Agent Host Protocol.

The wire types, the seven reducers, version negotiation, the error codes and the
transport abstraction all come from `agent_host_protocol`, the layer this shares
with the sibling host. Nothing protocol-shaped is defined here (ADR 0001).
"""

from __future__ import annotations

from agent_host_client.client import (
    AhpClient,
    AhpClientError,
    ClientClosed,
    ClientConfig,
    RequestTimeout,
    RpcError,
    Subscription,
    TransportError,
    is_session_gone,
)

__version__ = "0.1.0.dev0"

__all__ = [
    "AhpClient",
    "AhpClientError",
    "ClientClosed",
    "ClientConfig",
    "RequestTimeout",
    "RpcError",
    "Subscription",
    "TransportError",
    "__version__",
    "is_session_gone",
]
