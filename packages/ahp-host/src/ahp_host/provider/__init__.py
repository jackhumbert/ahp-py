"""The agent-provider extension point and the offline echo implementation."""

from __future__ import annotations

from ahp_host.provider.base import (
    HOST_CLIENT_ID,
    AgentInfo,
    AgentProvider,
    AgentSession,
    AgentSessionContext,
    GatesSteering,
    HostTool,
    ObservesChatState,
    ObservesSessionState,
    OriginTurn,
    ProvidesOperations,
    PublishesChatActivity,
    ReadsClientPlugins,
    ReopensToolChats,
    ResolvesOriginTurn,
    ResumableAgentProvider,
    ResumesOnRestore,
    ReturnsStructuredOutput,
    RunCredential,
    TurnSink,
    UserMessage,
)
from ahp_host.provider.changes import OperationContext
from ahp_host.provider.echo import EchoProvider, EchoSession

__all__ = [
    "HOST_CLIENT_ID",
    "AgentInfo",
    "AgentProvider",
    "AgentSession",
    "AgentSessionContext",
    "EchoProvider",
    "EchoSession",
    "GatesSteering",
    "HostTool",
    "ObservesChatState",
    "ObservesSessionState",
    "OperationContext",
    "OriginTurn",
    "ProvidesOperations",
    "PublishesChatActivity",
    "ReadsClientPlugins",
    "ReopensToolChats",
    "ResolvesOriginTurn",
    "ResumableAgentProvider",
    "ResumesOnRestore",
    "ReturnsStructuredOutput",
    "RunCredential",
    "TurnSink",
    "UserMessage",
]
