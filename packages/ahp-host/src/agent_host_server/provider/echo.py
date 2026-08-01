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

    def __init__(
        self,
        *,
        provider_id: str = "echo",
        display_name: str = "Echo",
        description: str = "Echoes your message back. No model, no network.",
        model_name: str = "Echo Model v1",
        delay: float = 0.0,
    ) -> None:
        self._delay = delay
        # `display_name` is what a client labels the agent with; `models` become
        # entries in VS Code's chat model picker (AgentHostLanguageModelProvider
        # reads them straight out of root state). They are deliberately different
        # strings here so it is obvious which is which in the UI.
        self._info = AgentInfo(
            provider=provider_id,
            display_name=display_name,
            description=description,
            models=({"id": "echo-1", "name": model_name},),
        )

    @property
    def agent(self) -> AgentInfo:
        return self._info

    async def create_session(self, context: AgentSessionContext) -> EchoSession:
        return EchoSession(context, delay=self._delay)
