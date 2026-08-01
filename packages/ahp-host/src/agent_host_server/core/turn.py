"""The agent event mapper: neutral provider events -> AHP chat actions.

This is the layer ADR 0003 exists to create. Providers report what the agent did
(`text_delta`, `tool_call_started`, ...) and this translates that into the
protocol's action vocabulary, so an adapter never encodes protocol ordering
rules and survives a spec bump untouched.

The ordering it guarantees is pinned by conformance fixture
``161-chat-turn-lifecycle-on-chat.json``: a markdown ``chat/responsePart`` must
exist before any ``chat/delta`` targets it. An adapter emitting raw actions has
to know that; here it is impossible to get wrong.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

from agent_host_server.core.pending import PendingRequests
from agent_host_server.core.sequencer import Sequencer
from agent_host_server.provider.base import (
    AgentSession,
    InputOutcome,
    InputRequest,
    UserMessage,
)

__all__ = ["ActionTurnSink", "TurnRunner", "turn_scope"]


def turn_scope(channel: str, turn_id: str) -> str:
    """The lifetime a suspended request is bound to (ADR 0005).

    The turn, not the session and not the connection: a cancelled turn must free
    everything waiting under it, and the connection that started the turn is
    frequently not the one that answers.
    """
    return f"{channel}#{turn_id}"


class ActionTurnSink:
    """A :class:`~agent_host_server.provider.base.TurnSink` that publishes actions."""

    def __init__(
        self,
        sequencer: Sequencer,
        channel: str,
        turn_id: str,
        pending: PendingRequests | None = None,
    ) -> None:
        self._sequencer = sequencer
        self._channel = channel
        self._turn_id = turn_id
        self._pending = pending if pending is not None else PendingRequests()
        self._markdown_part_id: str | None = None
        self._reasoning_part_id: str | None = None

    async def _ensure_markdown_part(self) -> str:
        if self._markdown_part_id is None:
            self._markdown_part_id = f"md-{uuid.uuid4()}"
            await self._sequencer.publish(
                self._channel,
                {
                    "type": "chat/responsePart",
                    "turnId": self._turn_id,
                    "part": {
                        "kind": "markdown",
                        "id": self._markdown_part_id,
                        "content": "",
                    },
                },
            )
        return self._markdown_part_id

    async def text_delta(self, text: str) -> None:
        part_id = await self._ensure_markdown_part()
        await self._sequencer.publish(
            self._channel,
            {
                "type": "chat/delta",
                "turnId": self._turn_id,
                "partId": part_id,
                "content": text,
            },
        )

    async def reasoning_delta(self, text: str) -> None:
        if self._reasoning_part_id is None:
            self._reasoning_part_id = f"re-{uuid.uuid4()}"
            await self._sequencer.publish(
                self._channel,
                {
                    "type": "chat/responsePart",
                    "turnId": self._turn_id,
                    "part": {
                        "kind": "reasoning",
                        "id": self._reasoning_part_id,
                        "content": "",
                    },
                },
            )
        await self._sequencer.publish(
            self._channel,
            {
                "type": "chat/reasoning",
                "turnId": self._turn_id,
                "partId": self._reasoning_part_id,
                "content": text,
            },
        )

    async def tool_call_started(self, call_id: str, name: str, tool_input: Any = None) -> None:
        # `chat/toolCallStart` creates its own toolCall response part; emitting
        # an extra `chat/responsePart` for it would duplicate the part.
        action: dict[str, Any] = {
            "type": "chat/toolCallStart",
            "turnId": self._turn_id,
            "toolCallId": call_id,
            "toolName": name,
        }
        if tool_input is not None:
            action["toolInput"] = tool_input
        await self._sequencer.publish(self._channel, action)

    async def tool_call_completed(self, call_id: str, result: Any = None) -> None:
        action: dict[str, Any] = {
            "type": "chat/toolCallComplete",
            "turnId": self._turn_id,
            "toolCallId": call_id,
        }
        if result is not None:
            action["result"] = result
        await self._sequencer.publish(self._channel, action)

    async def turn_failed(self, message: str) -> None:
        await self._sequencer.publish(
            self._channel,
            {
                "type": "chat/error",
                "turnId": self._turn_id,
                "error": {"message": message},
                "duration": 0,
            },
        )

    async def request_input(self, request: InputRequest) -> InputOutcome:
        """Publish an input request and suspend until a client resolves it.

        The id is minted by the registry, never by the provider (ADR 0005) --
        the provider names its questions, the host names the request.

        Note the request is *parked before it is published*. Publishing first
        would open a window where a very fast client could dispatch
        `chat/inputCompleted` against a request the registry does not know
        about yet, and the host would drop the answer.
        """
        parked = self._pending.open(turn_scope(self._channel, self._turn_id), "input")

        wire: dict[str, Any] = {"id": parked.id}
        if request.message is not None:
            wire["message"] = request.message
        if request.url is not None:
            wire["url"] = request.url
        if request.questions:
            wire["questions"] = [
                {
                    **question.extra,
                    "kind": question.kind,
                    "id": question.id,
                    "message": question.message,
                    **({"options": list(question.options)} if question.options else {}),
                }
                for question in request.questions
            ]

        await self._sequencer.publish(
            self._channel,
            {"type": "chat/inputRequested", "turnId": self._turn_id, "request": wire},
        )

        outcome = await parked.future
        answers = outcome.payload if isinstance(outcome.payload, Mapping) else {}
        return InputOutcome(response=outcome.response, answers=answers)


class TurnRunner:
    """Runs one turn: hand the message to the agent, publish what comes back."""

    def __init__(
        self, sequencer: Sequencer, channel: str, pending: PendingRequests | None = None
    ) -> None:
        self._sequencer = sequencer
        self._channel = channel
        self._pending = pending if pending is not None else PendingRequests()

    async def run(self, agent_session: AgentSession | None, started: Mapping[str, Any]) -> None:
        turn_id = started.get("turnId")
        if not isinstance(turn_id, str):
            return
        sink = ActionTurnSink(self._sequencer, self._channel, turn_id, self._pending)

        if agent_session is None:
            await sink.turn_failed("no agent session")
            return

        message = started.get("message") or {}
        text = message.get("text") if isinstance(message, Mapping) else None
        try:
            await agent_session.send_user_message(
                UserMessage(text=text if isinstance(text, str) else "", raw=message),
                sink,
            )
        except Exception as exc:
            if self._is_active(turn_id):
                await sink.turn_failed(f"{type(exc).__name__}: {exc}")
            return
        finally:
            # ADR 0005: the turn is the scope. However this turn ended -- return,
            # raise or cancellation -- nothing may still be parked under it, or
            # the provider stays blocked on a future nobody will ever resolve
            # and the chat sits in `InputNeeded` until the session is disposed.
            self._pending.cancel_scope(turn_scope(self._channel, turn_id), "turn ended")

        # A provider that returns normally after being cancelled would otherwise
        # complete a turn a client already ended. The reducer would no-op on it,
        # but it still burns a serverSeq and broadcasts a `turnComplete` for a
        # turn every client has already seen cancelled.
        if not self._is_active(turn_id):
            return

        await self._sequencer.publish(
            self._channel,
            {"type": "chat/turnComplete", "turnId": turn_id, "duration": 0},
        )

    def _is_active(self, turn_id: str) -> bool:
        """Whether *turn_id* is still the channel's active turn."""
        state = self._sequencer.state_of(self._channel)
        if not isinstance(state, Mapping):
            return False
        active = state.get("activeTurn")
        return isinstance(active, Mapping) and active.get("id") == turn_id
