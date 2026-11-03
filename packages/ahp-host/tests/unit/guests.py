"""Two recording agents: what the per-chat-agent tests drive.

Not a test module. `Recorded` is an echo session that remembers every per-chat
and session-wide call it is given, so a test can say which agent a call reached.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from typing import Any

from ahp_host.core import Host, LoopbackSingleUserPolicy
from ahp_host.provider import EchoProvider, EchoSession
from ahp_host.provider.base import (
    AgentSessionContext,
    ChatContext,
    TurnSink,
    UserMessage,
)

CHATS: dict[str, Any] = {"multipleChats": {"fork": True, "sideChat": True}}


class Recorded(EchoSession):
    def __init__(self, context: AgentSessionContext, delay: float = 0.0) -> None:
        super().__init__(context, delay=delay)
        self.messages: list[UserMessage] = []
        self.opened: list[ChatContext] = []
        self.closed: list[str] = []
        self.cancelled: list[tuple[str, str | None]] = []
        self.session_cancels = 0
        self.archived: list[bool] = []
        self.directories: list[list[str]] = []
        self.config_values: list[dict[str, Any]] = []
        self.was_closed = False
        self.was_disposed = False
        self.fail_chat_config = False
        self.released = asyncio.Event()

    async def send_user_message(self, message: UserMessage, sink: TurnSink) -> None:
        self.messages.append(message)
        await super().send_user_message(message, sink)

    async def chat_opened(self, context: ChatContext) -> None:
        self.opened.append(context)

    async def chat_closed(self, chat_uri: str) -> None:
        self.closed.append(chat_uri)

    async def cancel_chat(self, chat_uri: str, reason: str | None = None) -> None:
        self.cancelled.append((chat_uri, reason))

    async def cancel(self, reason: str | None = None) -> None:
        self.session_cancels += 1
        await super().cancel(reason)

    async def archived_changed(self, is_archived: bool) -> None:
        self.archived.append(is_archived)

    async def working_directories_changed(self, directories: Sequence[str]) -> None:
        self.directories.append(list(directories))

    async def config_changed(self, values: Mapping[str, Any]) -> None:
        self.config_values.append(dict(values))
        await super().config_changed(values)

    async def chat_config_changed(self, chat: str, values: Mapping[str, Any]) -> None:
        if self.fail_chat_config:
            raise RuntimeError("cannot apply chat config")
        await super().chat_config_changed(chat, values)

    async def disposed(self) -> None:
        self.was_disposed = True

    async def aclose(self) -> None:
        self.was_closed = True
        await super().aclose()


class RecordingProvider(EchoProvider):
    """An echo agent that keeps every agent session it made, and resumes them."""

    def __init__(self, provider_id: str = "echo", *, delay: float = 0.0, **kwargs: Any) -> None:
        kwargs.setdefault("capabilities", {**CHATS, "multipleWorkingDirectories": {}})
        kwargs.setdefault("configurable", True)
        super().__init__(
            provider_id=provider_id, display_name=provider_id.title(), delay=delay, **kwargs
        )
        self.sessions: list[Recorded] = []
        self.resumed: list[AgentSessionContext] = []
        self.contexts: list[AgentSessionContext] = []
        self._turn_delay = delay
        #: Raised from `create_session` when set.
        self.create_error: Exception | None = None
        self.state_tag = f"{provider_id}-state"

    async def create_session(self, context: AgentSessionContext) -> Recorded:
        if self.create_error is not None:
            raise self.create_error
        self.contexts.append(context)
        made = Recorded(context, delay=self._turn_delay)
        self.sessions.append(made)
        return made

    async def resume_session(self, context: AgentSessionContext) -> Recorded:
        self.resumed.append(context)
        made = Recorded(context, delay=self._turn_delay)
        self.sessions.append(made)
        return made

    async def resume_state_of(self, session: Any) -> Mapping[str, Any] | None:
        return {"tag": self.state_tag}


def pair(
    *, delay: float = 0.0, second_delay: float | None = None, **host_kwargs: Any
) -> tuple[Host, RecordingProvider, RecordingProvider]:
    """A host serving `echo` (the session's) and `echo2` (the guest), per-chat agents on."""
    own = RecordingProvider("echo", delay=delay)
    guest = RecordingProvider("echo2", delay=delay if second_delay is None else second_delay)
    host_kwargs.setdefault("multiple_chat_providers", True)
    return Host([own, guest], LoopbackSingleUserPolicy(), **host_kwargs), own, guest
