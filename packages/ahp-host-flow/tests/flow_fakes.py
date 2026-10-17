"""Inner-provider fakes for the flow tests; not a test module.

Loaded by import path (`tests.flow_fakes:<Name>`), as a flow.toml would.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from ahp_host.provider.base import (
    AgentInfo,
    AgentSessionContext,
    ModelInfo,
    TurnSink,
    UserMessage,
)


@dataclass
class RecordingSession:
    context: AgentSessionContext
    output_tokens: int = 5
    messages: list[UserMessage] = field(default_factory=list)
    overlays: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    config_changes: list[dict[str, Any]] = field(default_factory=list)
    cancelled: list[str | None] = field(default_factory=list)
    closed: bool = False
    overlay_seen: asyncio.Event = field(default_factory=asyncio.Event)

    async def send_user_message(self, message: UserMessage, sink: TurnSink) -> None:
        self.messages.append(message)
        if message.text.startswith("wait-overlay"):
            await self.overlay_seen.wait()
        overlay = self.overlays[-1][1] if self.overlays else {}
        suffix = f" overlay={json.dumps(overlay, sort_keys=True)}" if overlay else ""
        await sink.text_delta(f"rec: {message.text}{suffix}")
        await sink.usage(input_tokens=1, output_tokens=self.output_tokens)

    async def chat_config_changed(self, chat: str, values: Any) -> None:
        self.overlays.append((chat, dict(values)))
        self.overlay_seen.set()

    async def config_changed(self, values: Any) -> None:
        self.config_changes.append(dict(values))

    async def cancel(self, reason: str | None = None) -> None:
        self.cancelled.append(reason)

    async def aclose(self) -> None:
        self.closed = True


class RecordingProvider:
    """Answers `rec: <text>` and remembers every session, message and config change."""

    instances: ClassVar[list[RecordingProvider]] = []

    def __init__(
        self, *, provider_id: str = "rec", output_tokens: int = 5, config: Path | None = None
    ) -> None:
        self.provider_id = provider_id
        self.output_tokens = output_tokens
        self.config_path = config
        self.sessions: list[RecordingSession] = []
        RecordingProvider.instances.append(self)

    @property
    def agent(self) -> AgentInfo:
        return AgentInfo(
            provider=self.provider_id,
            display_name="Recorder",
            description="Records what it is given.",
            models=(
                ModelInfo(id="rec-large", name="Rec Large", vendor="Acme"),
                ModelInfo(id="rec-small", name="Rec Small"),
            ),
        )

    async def create_session(self, context: AgentSessionContext) -> RecordingSession:
        session = RecordingSession(context, self.output_tokens)
        self.sessions.append(session)
        return session


@dataclass
class HangingSession:
    context: AgentSessionContext
    started: asyncio.Event = field(default_factory=asyncio.Event)
    cancelled: list[str | None] = field(default_factory=list)
    closed: bool = False
    _release: asyncio.Event = field(default_factory=asyncio.Event)

    async def send_user_message(self, message: UserMessage, sink: TurnSink) -> None:
        self.started.set()
        await self._release.wait()
        await sink.text_delta("released")

    async def cancel(self, reason: str | None = None) -> None:
        self.cancelled.append(reason)
        self._release.set()

    async def aclose(self) -> None:
        self.closed = True


class HangingProvider:
    """A turn that never ends until the session is cancelled."""

    instances: ClassVar[list[HangingProvider]] = []

    def __init__(self, *, provider_id: str = "hang") -> None:
        self.provider_id = provider_id
        self.sessions: list[HangingSession] = []
        HangingProvider.instances.append(self)

    @property
    def agent(self) -> AgentInfo:
        return AgentInfo(
            provider=self.provider_id,
            display_name="Hanger",
            description="Never answers until cancelled.",
            models=(),
        )

    async def create_session(self, context: AgentSessionContext) -> HangingSession:
        session = HangingSession(context)
        self.sessions.append(session)
        return session


def not_a_provider() -> object:
    return object()


def reset_fakes() -> None:
    RecordingProvider.instances.clear()
    HangingProvider.instances.clear()


class BrokenSessionProvider:
    """A provider whose `create_session` raises."""

    def __init__(self, *, provider_id: str = "broken") -> None:
        self.provider_id = provider_id

    @property
    def agent(self) -> AgentInfo:
        return AgentInfo(
            provider=self.provider_id,
            display_name="Broken",
            description="Cannot make a session.",
            models=(),
        )

    async def create_session(self, context: AgentSessionContext) -> HangingSession:
        raise RuntimeError("no session for you")
