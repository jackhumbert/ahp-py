"""A Python client for the Agent Host Protocol.

The wire types, the seven reducers, version negotiation, the error codes and the
transport abstraction all come from `agent_host_protocol`, the layer this shares
with the sibling host. Nothing protocol-shaped is defined here (ADR 0001).

```python
import asyncio
from agent_host_client import connect, Delta, ToolCallReady, TurnCompleted


async def main() -> None:
    async with connect("ws://localhost:4321") as client:
        async with await client.create_session(provider="echo", cwd=".") as session:
            async for event in session.prompt("Summarise README.md"):
                match event:
                    case Delta(text=t):
                        print(t, end="", flush=True)
                    case ToolCallReady() as call:
                        call.approve()
                    case TurnCompleted():
                        print()
```
"""

from __future__ import annotations

from agent_host_client.api import (
    ApprovalPolicy,
    Chat,
    Client,
    Delta,
    InputRequested,
    Reasoning,
    Reconnected,
    ResponsePartAdded,
    Session,
    TitleChanged,
    ToolCallCompleted,
    ToolCallReady,
    ToolCallResultReview,
    ToolCallStarted,
    TurnCancelled,
    TurnCompleted,
    TurnEvent,
    TurnFailed,
    TurnStarted,
    TurnStream,
    UnknownEvent,
    Usage,
    approve_all,
    auto,
    connect,
    deny_all,
)
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
    "ApprovalPolicy",
    "Chat",
    "Client",
    "ClientClosed",
    "ClientConfig",
    "Delta",
    "InputRequested",
    "Reasoning",
    "Reconnected",
    "RequestTimeout",
    "ResponsePartAdded",
    "RpcError",
    "Session",
    "Subscription",
    "TitleChanged",
    "ToolCallCompleted",
    "ToolCallReady",
    "ToolCallResultReview",
    "ToolCallStarted",
    "TransportError",
    "TurnCancelled",
    "TurnCompleted",
    "TurnEvent",
    "TurnFailed",
    "TurnStarted",
    "TurnStream",
    "UnknownEvent",
    "Usage",
    "__version__",
    "approve_all",
    "auto",
    "connect",
    "deny_all",
    "is_session_gone",
]
