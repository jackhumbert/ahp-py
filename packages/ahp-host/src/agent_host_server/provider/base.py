"""The agent-provider extension point.

Per ADR 0003 a provider emits **neutral events describing what the agent did**,
never AHP actions. The host maps those to `chat/*` actions, assigns `serverSeq`,
applies the reducer and fans out.

That boundary is the difference between an adapter that survives a spec bump and
one that does not. The only prior-art provider kit has adapters emit
`session/delta` / `session/responsePart` / `session/turnComplete` directly --
spec 0.4.0 relocated all three to the chat channel, and every adapter written
against it is wire-dead today. Nothing about the agent runtimes changed.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "AgentInfo",
    "AgentProvider",
    "AgentSession",
    "AgentSessionContext",
    "ResumableAgentProvider",
    "TurnSink",
    "UserMessage",
]


@dataclass(frozen=True)
class AgentInfo:
    """What the host publishes about this provider on the root channel.

    Mirrors the protocol's ``AgentInfo``. ``protected_resources`` is left out:
    v0.1 implements no authentication, and declaring none is fully conformant --
    every discovery field is optional.
    """

    provider: str
    display_name: str
    description: str
    models: Sequence[Mapping[str, Any]] = field(default_factory=tuple)
    capabilities: Mapping[str, Any] = field(default_factory=dict)

    def to_wire(self) -> dict[str, Any]:
        wire: dict[str, Any] = {
            "provider": self.provider,
            "displayName": self.display_name,
            "description": self.description,
            "models": list(self.models),
        }
        if self.capabilities:
            wire["capabilities"] = dict(self.capabilities)
        return wire


@dataclass(frozen=True)
class UserMessage:
    """One user turn request, in provider terms."""

    text: str
    raw: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentSessionContext:
    session_uri: str
    chat_uri: str
    provider_id: str
    #: Plural since 0.7.0; a host without the multipleWorkingDirectories
    #: capability keeps only the first entry.
    working_directories: Sequence[str] = ()
    model: str | None = None
    config: Mapping[str, Any] = field(default_factory=dict)
    resume_state: Mapping[str, Any] | None = None


@runtime_checkable
class TurnSink(Protocol):
    """What a provider may report while a turn runs.

    Deliberately *not* AHP actions. The host owns ordering -- including the rule
    that a `chat/responsePart` must precede any `chat/delta` for it, which is
    pinned by conformance fixture 161 and is the easiest thing for an adapter
    author to get wrong.
    """

    async def text_delta(self, text: str) -> None: ...

    async def reasoning_delta(self, text: str) -> None: ...

    async def tool_call_started(self, call_id: str, name: str, tool_input: Any = None) -> None: ...

    async def tool_call_completed(self, call_id: str, result: Any = None) -> None: ...

    async def turn_failed(self, message: str) -> None: ...


@runtime_checkable
class AgentSession(Protocol):
    """One live conversation with an agent runtime."""

    async def send_user_message(self, message: UserMessage, sink: TurnSink) -> None:
        """Run one turn, reporting progress through *sink*.

        Cancellation is two-level: the host cancels the asyncio task for a
        cooperative unwind, and calls :meth:`cancel` for anything the runtime
        needs told explicitly.
        """
        ...

    async def cancel(self, reason: str | None = None) -> None: ...

    async def aclose(self) -> None: ...


@runtime_checkable
class AgentProvider(Protocol):
    @property
    def agent(self) -> AgentInfo: ...

    async def create_session(self, context: AgentSessionContext) -> AgentSession: ...


@runtime_checkable
class ResumableAgentProvider(AgentProvider, Protocol):
    """A provider whose sessions survive a host restart.

    Split from :class:`AgentProvider` so the host can feature-detect with
    ``isinstance`` rather than inventing a capability flag. The host persists the
    opaque resume state; only the provider interprets it.
    """

    async def resume_session(self, context: AgentSessionContext) -> AgentSession: ...

    async def resume_state_of(self, session: AgentSession) -> Mapping[str, Any] | None: ...
