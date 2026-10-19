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
    OriginTurn,
    ProvidesOperations,
    ReadsClientPlugins,
    ResolvesOriginTurn,
    ResumableAgentProvider,
    ResumesOnRestore,
    ReturnsStructuredOutput,
    TurnSink,
    UserMessage,
)
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
    "OriginTurn",
    "ProvidesOperations",
    "ReadsClientPlugins",
    "ResolvesOriginTurn",
    "ResumableAgentProvider",
    "ResumesOnRestore",
    "ReturnsStructuredOutput",
    "TurnSink",
    "UserMessage",
]
