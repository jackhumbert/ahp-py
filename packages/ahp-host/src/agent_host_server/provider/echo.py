"""An agent provider with no model, no network and no credentials.

This is the fixture the whole suite runs against, and the thing that makes
`pytest` work offline on a laptop with no API key. It is also the smallest
complete example of the provider interface, so it doubles as the reference an
adapter author reads.
"""

from __future__ import annotations

import asyncio

from agent_host_server.provider.base import (
    AgentInfo,
    AgentSessionContext,
    TurnSink,
    UserMessage,
)

__all__ = ["EchoProvider", "EchoSession"]


class EchoSession:
    def __init__(self, context: AgentSessionContext, *, delay: float = 0.0) -> None:
        self.context = context
        self._delay = delay
        self._cancelled = False

    async def send_user_message(self, message: UserMessage, sink: TurnSink) -> None:
        self._cancelled = False
        for chunk in ("You said: ", message.text):
            if self._cancelled:
                return
            if self._delay:
                await asyncio.sleep(self._delay)
            await sink.text_delta(chunk)

    async def cancel(self, reason: str | None = None) -> None:
        self._cancelled = True

    async def aclose(self) -> None:
        self._cancelled = True


class EchoProvider:
    """Echoes the user's message back, one text delta at a time."""

    def __init__(self, *, provider_id: str = "echo", delay: float = 0.0) -> None:
        self._delay = delay
        self._info = AgentInfo(
            provider=provider_id,
            display_name="Echo",
            description="Echoes your message back. No model, no network.",
            models=({"id": "echo-1", "name": "Echo"},),
        )

    @property
    def agent(self) -> AgentInfo:
        return self._info

    async def create_session(self, context: AgentSessionContext) -> EchoSession:
        return EchoSession(context, delay=self._delay)
