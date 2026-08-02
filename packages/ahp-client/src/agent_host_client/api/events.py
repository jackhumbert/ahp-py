"""Turn events: frozen, ``match``-able, and carrying their own actions.

Wire values stay plain dicts forever (ADR 0001). These are **derived** -- they
never copy or mutate an envelope, they carry it -- which is the one place the
shared package's no-``Unknown``-class rule inverts: events are ours, not the
wire's, so :class:`UnknownEvent` is a feature rather than a lossy decode.

The actionable ones carry their own answer. ``ToolCallReady.approve()`` exists
because the alternative is handing a caller a tool-call id and a chat URI and
letting them assemble the action, which is where every consumer gets it wrong.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, TypeAlias

from agent_host_protocol.types import JsonObject

__all__ = [
    "Delta",
    "InputRequested",
    "Reasoning",
    "Reconnected",
    "ResponsePartAdded",
    "TitleChanged",
    "ToolCallCompleted",
    "ToolCallReady",
    "ToolCallResultReview",
    "ToolCallStarted",
    "TurnCancelled",
    "TurnCompleted",
    "TurnEvent",
    "TurnFailed",
    "TurnStarted",
    "UnknownEvent",
    "Usage",
]

#: Sends one action on the caller's behalf. Injected rather than closed over so
#: an event stays a plain frozen value that a test can construct.
Dispatcher: TypeAlias = Callable[[str, Mapping[str, Any]], None]


@dataclass(frozen=True, slots=True)
class _Base:
    envelope: JsonObject

    @property
    def channel(self) -> str:
        return str(self.envelope.get("channel", ""))

    @property
    def action(self) -> JsonObject:
        raw = self.envelope.get("action")
        return raw if isinstance(raw, dict) else {}

    @property
    def turn_id(self) -> str:
        return str(self.action.get("turnId", ""))


@dataclass(frozen=True, slots=True)
class TurnStarted(_Base):
    pass


@dataclass(frozen=True, slots=True)
class Delta(_Base):
    """A chunk of assistant text.

    ``text`` is the delta as sent. The authoritative text of a turn is the
    concatenation of its markdown response parts read from the mirror -- hosts
    fold a turn's opening characters into the subscribe snapshot rather than
    emitting them as deltas, so a consumer that renders only these shows
    "ANANA" for "BANANA".
    """

    @property
    def text(self) -> str:
        return str(self.action.get("content", ""))

    @property
    def part_id(self) -> str:
        return str(self.action.get("partId", ""))


@dataclass(frozen=True, slots=True)
class Reasoning(_Base):
    @property
    def text(self) -> str:
        return str(self.action.get("content", ""))


@dataclass(frozen=True, slots=True)
class ResponsePartAdded(_Base):
    @property
    def part(self) -> JsonObject:
        raw = self.action.get("part")
        return raw if isinstance(raw, dict) else {}


@dataclass(frozen=True, slots=True)
class _ToolCall(_Base):
    @property
    def tool_call_id(self) -> str:
        return str(self.action.get("toolCallId", ""))

    @property
    def tool_name(self) -> str:
        return str(self.action.get("toolName", ""))


@dataclass(frozen=True, slots=True)
class ToolCallStarted(_ToolCall):
    @property
    def contributor(self) -> JsonObject | None:
        raw = self.action.get("contributor")
        return raw if isinstance(raw, dict) else None


@dataclass(frozen=True, slots=True)
class ToolCallReady(_ToolCall):
    """The agent is blocked waiting for approval.

    A host arbitrates and the **first** answer wins; a later one comes back with
    a ``rejectionReason`` and the mirror reverts it. That is another client
    getting there first, not an error.
    """

    dispatch: Dispatcher | None = None

    @property
    def options(self) -> Sequence[JsonObject]:
        raw = self.action.get("options")
        return [o for o in raw if isinstance(o, dict)] if isinstance(raw, list) else []

    @property
    def tool_input(self) -> Any:
        return self.action.get("toolInput")

    def approve(self, *, option_id: str | None = None, edited_input: Any = None) -> None:
        payload: JsonObject = {
            "type": "chat/toolCallConfirmed",
            "toolCallId": self.tool_call_id,
            "approved": True,
        }
        if option_id is not None:
            payload["optionId"] = option_id
        if edited_input is not None:
            payload["editedToolInput"] = edited_input
        self._send(payload)

    def deny(self, *, reason: Literal["denied", "skipped"] = "denied") -> None:
        self._send(
            {
                "type": "chat/toolCallConfirmed",
                "toolCallId": self.tool_call_id,
                "approved": False,
                "reason": reason,
            }
        )

    def _send(self, action: JsonObject) -> None:
        if self.dispatch is None:
            raise RuntimeError("this event was constructed without a dispatcher")
        self.dispatch(self.channel, action)


@dataclass(frozen=True, slots=True)
class ToolCallResultReview(_ToolCall):
    """``requiresResultConfirmation``. Ignoring this hangs the turn forever."""

    dispatch: Dispatcher | None = None

    def confirm(self) -> None:
        self._send(True)

    def reject(self) -> None:
        self._send(False)

    def _send(self, approved: bool) -> None:
        if self.dispatch is None:
            raise RuntimeError("this event was constructed without a dispatcher")
        self.dispatch(
            self.channel,
            {
                "type": "chat/toolCallResultConfirmed",
                "toolCallId": self.tool_call_id,
                "approved": approved,
            },
        )


@dataclass(frozen=True, slots=True)
class ToolCallCompleted(_ToolCall):
    @property
    def result(self) -> Any:
        return self.action.get("result")


@dataclass(frozen=True, slots=True)
class InputRequested(_Base):
    """An elicitation. Carries its own answer, like a tool approval does."""

    dispatch: Dispatcher | None = None

    @property
    def request_id(self) -> str:
        return str(self.action.get("requestId", ""))

    def answer(self, answers: Mapping[str, Any]) -> None:
        self._send("accept", dict(answers))

    def decline(self) -> None:
        self._send("decline", {})

    def cancel(self) -> None:
        self._send("cancel", {})

    def _send(self, kind: str, answers: JsonObject) -> None:
        if self.dispatch is None:
            raise RuntimeError("this event was constructed without a dispatcher")
        self.dispatch(
            self.channel,
            {
                "type": "chat/inputAnswerChanged",
                "requestId": self.request_id,
                "answers": answers,
                "kind": kind,
            },
        )


@dataclass(frozen=True, slots=True)
class Usage(_Base):
    pass


@dataclass(frozen=True, slots=True)
class TitleChanged(_Base):
    @property
    def title(self) -> str:
        return str(self.action.get("title", ""))


@dataclass(frozen=True, slots=True)
class Reconnected(_Base):
    """The supervisor rebuilt the connection under a live turn."""


@dataclass(frozen=True, slots=True)
class TurnCompleted(_Base):
    #: Authoritative text, read from the mirror rather than accumulated.
    text: str = ""


@dataclass(frozen=True, slots=True)
class TurnFailed(_Base):
    reason: str = ""


@dataclass(frozen=True, slots=True)
class TurnCancelled(_Base):
    pass


@dataclass(frozen=True, slots=True)
class UnknownEvent(_Base):
    """An action type we do not model.

    It still reaches the caller with its envelope, and the reducer still applied
    it -- forward compatibility is a protocol requirement, not a courtesy.
    """


TurnEvent: TypeAlias = (
    TurnStarted
    | Delta
    | Reasoning
    | ResponsePartAdded
    | ToolCallStarted
    | ToolCallReady
    | ToolCallResultReview
    | ToolCallCompleted
    | InputRequested
    | Usage
    | TitleChanged
    | Reconnected
    | TurnCompleted
    | TurnFailed
    | TurnCancelled
    | UnknownEvent
)

#: Action type -> event class. Anything absent becomes `UnknownEvent`.
_BY_TYPE: dict[str, Any] = {
    "chat/turnStarted": TurnStarted,
    "chat/delta": Delta,
    "chat/reasoningDelta": Reasoning,
    "chat/responsePart": ResponsePartAdded,
    "chat/toolCallStart": ToolCallStarted,
    "chat/toolCallReady": ToolCallReady,
    "chat/toolCallResultReview": ToolCallResultReview,
    "chat/toolCallComplete": ToolCallCompleted,
    "chat/inputRequested": InputRequested,
    "chat/usage": Usage,
    "chat/titleChanged": TitleChanged,
    "chat/turnComplete": TurnCompleted,
    "chat/turnFailed": TurnFailed,
    "chat/turnCancelled": TurnCancelled,
}

_NEEDS_DISPATCH = {ToolCallReady, ToolCallResultReview, InputRequested}


def event_for(envelope: Mapping[str, Any], dispatch: Dispatcher | None = None) -> TurnEvent:
    """Wrap one envelope. Never raises; an unknown type is still delivered."""
    action = envelope.get("action")
    action_type = action.get("type") if isinstance(action, Mapping) else None
    cls = _BY_TYPE.get(str(action_type), UnknownEvent)
    payload = dict(envelope)
    if cls in _NEEDS_DISPATCH:
        return cls(payload, dispatch)  # type: ignore[no-any-return]
    return cls(payload)  # type: ignore[no-any-return]
