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
    ReadsClientPlugins,
    ResumableAgentProvider,
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
    "ReadsClientPlugins",
    "ResumableAgentProvider",
    "ReturnsStructuredOutput",
    "TurnSink",
    "UserMessage",
]
