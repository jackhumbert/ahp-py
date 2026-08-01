"""An agent provider with no model, no network and no credentials.

This is the fixture the whole suite runs against, and the thing that makes
`pytest` work offline on a laptop with no API key. It is also the smallest
complete example of the provider interface, so it doubles as the reference an
adapter author reads.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from agent_host_server.provider.base import (
    AgentInfo,
    AgentSessionContext,
    InputQuestion,
    InputRequest,
    SessionDescription,
    TurnSink,
    UserMessage,
)
from agent_host_server.provider.demo_customizations import (
    demo_customizations,
    demo_server_tools,
)

__all__ = ["EchoProvider", "EchoSession"]


def _selected(answer: Any) -> str | None:
    """The chosen option id out of a `single-select` answer, or None.

    Read defensively: the answer came off a client-dispatched action, and the
    reducer stores whatever it was sent.
    """
    if not isinstance(answer, Mapping):
        return None
    value = answer.get("value")
    return value if isinstance(value, str) else None


class EchoSession:
    def __init__(
        self,
        context: AgentSessionContext,
        *,
        delay: float = 0.0,
        customizations: bool = False,
        elicit: bool = False,
    ) -> None:
        self.context = context
        self._delay = delay
        self._customizations = customizations
        self._elicit = elicit
        self._cancelled = False

    async def describe(self) -> SessionDescription:
        """Contribute a fully-populated customization tree, when asked to.

        Off by default: it exists to find out what a client renders, not to
        pretend the echo agent has plugins.
        """
        if not self._customizations:
            return SessionDescription()
        return SessionDescription(
            customizations=demo_customizations(), server_tools=demo_server_tools()
        )

    async def send_user_message(self, message: UserMessage, sink: TurnSink) -> None:
        self._cancelled = False
        if self._elicit:
            # ADR 0005: the one sink method that suspends. The answer arrives on
            # whichever client the user used, which may not be the one that sent
            # this message -- so an adapter never sees a connection here.
            outcome = await sink.request_input(
                InputRequest(
                    message="Echo it back how?",
                    questions=[
                        InputQuestion(
                            id="style",
                            kind="single-select",
                            message="Style",
                            options=[
                                {"id": "plain", "label": "Plain"},
                                {"id": "shout", "label": "SHOUTING"},
                            ],
                        )
                    ],
                )
            )
            if not outcome.accepted:
                await sink.text_delta("(cancelled)")
                return
            style = _selected(outcome.answers.get("style"))
            text = message.text.upper() if style == "shout" else message.text
            await sink.text_delta(f"You said: {text}")
            return

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
        customizations: bool = False,
        elicit: bool = False,
        capabilities: Mapping[str, Any] | None = None,
    ) -> None:
        self._delay = delay
        self._customizations = customizations
        self._elicit = elicit
        # `display_name` is what a client labels the agent with; `models` become
        # entries in VS Code's chat model picker (AgentHostLanguageModelProvider
        # reads them straight out of root state). They are deliberately different
        # strings here so it is obvious which is which in the UI.
        #
        # `capabilities` is empty by default and that is the conformant choice:
        # every entry in `AgentCapabilities` is a client MUST NOT that only its
        # presence lifts, so a host that declares nothing is a host with the
        # narrowest surface, not an incomplete one.
        self._info = AgentInfo(
            provider=provider_id,
            display_name=display_name,
            description=description,
            models=({"id": "echo-1", "name": model_name},),
            capabilities=dict(capabilities or {}),
        )

    @property
    def agent(self) -> AgentInfo:
        return self._info

    async def create_session(self, context: AgentSessionContext) -> EchoSession:
        return EchoSession(
            context,
            delay=self._delay,
            customizations=self._customizations,
            elicit=self._elicit,
        )
