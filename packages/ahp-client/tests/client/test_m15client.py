"""M15 client surface: denial reasons, ``Session.waiting`` and operation params."""

from __future__ import annotations

import asyncio
from typing import Any, cast

from ahp_client import ToolCallReady
from ahp_client.api.client import Client, Session
from ahp_client.client.client import AhpClient
from ahp_client.client.mirror import StateMirror
from ahp_client.serve.inputs import InputResponder

CHAT = "ahp-chat:/c"


class _Sent:
    def __init__(self) -> None:
        self.actions: list[dict[str, Any]] = []

    def __call__(self, channel: str, action: Any) -> None:
        assert channel == CHAT
        self.actions.append(dict(action))

    def dispatch(self, channel: str, action: Any) -> None:
        self(channel, action)


class _Mirror:
    def __init__(self, states: dict[str, Any]) -> None:
        self._states = states

    def state(self, uri: str) -> Any:
        return self._states.get(uri)


class _Protocol:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    async def invoke_changeset_operation(
        self, changeset: str, *, operation_id: str, **extra: Any
    ) -> dict[str, Any]:
        self.calls.append((changeset, operation_id, extra))
        return {}


class _Stub:
    def __init__(self, states: dict[str, Any] | None = None) -> None:
        self.mirror = _Mirror(states or {})
        self.protocol = _Protocol()


def _ready(sent: _Sent) -> ToolCallReady:
    envelope = {
        "channel": CHAT,
        "action": {"type": "chat/toolCallReady", "turnId": "t1", "toolCallId": "tc1"},
        "serverSeq": 1,
    }
    return ToolCallReady(envelope=envelope, dispatch=sent)


def test_m15client_deny_sends_reason_fields() -> None:
    sent = _Sent()
    _ready(sent).deny(reason_message="use b", user_suggestion={"text": "edit b"})
    _ready(sent).deny()
    assert sent.actions[0]["reasonMessage"] == "use b"
    assert sent.actions[0]["userSuggestion"] == {"text": "edit b"}
    assert "reasonMessage" not in sent.actions[1]
    assert "userSuggestion" not in sent.actions[1]


def test_m15client_confirm_tool_sends_reason_fields() -> None:
    sent = _Sent()
    responder = InputResponder(cast(AhpClient, sent), cast(StateMirror, _Mirror({})))
    entry = {"chat": CHAT, "turnId": "t1", "toolCall": {"toolCallId": "tc1"}}
    responder.confirm_tool(
        entry, approved=False, reason_message="no", user_suggestion={"text": "other"}
    )
    responder.confirm_tool(entry, approved=True, reason_message="ignored")
    assert sent.actions[0]["reasonMessage"] == "no"
    assert sent.actions[0]["userSuggestion"] == {"text": "other"}
    assert sent.actions[1]["approved"] is True
    assert "reasonMessage" not in sent.actions[1]


def test_m15client_waiting_filters_client_execution() -> None:
    state = {
        "inputNeeded": [
            {"id": "a", "kind": "toolConfirmation", "chat": "A", "turnId": "t1"},
            {"id": "b", "kind": "toolClientExecution", "chat": "A", "turnId": "t1"},
            {"id": "c", "kind": "chatInput", "chat": "B", "turnId": "t2"},
        ]
    }
    session = Session(cast(Client, _Stub({"s:/1": state})), "s:/1", "p", owned=False)
    waiting = session.waiting()
    assert [(w.id, w.kind, w.chat, w.turn_id) for w in waiting] == [
        ("a", "toolConfirmation", "A", "t1"),
        ("c", "chatInput", "B", "t2"),
    ]
    assert [w.id for w in session.waiting("B")] == ["c"]


def test_m15client_invoke_operation_sends_params() -> None:
    stub = _Stub()
    session = Session(cast(Client, stub), "s:/1", "p", owned=False)

    async def run() -> None:
        await session.invoke_operation("cs:/1", "commit", params={"message": "m"})
        await session.invoke_operation("cs:/1", "commit")
        await session.invoke_operation("cs:/1", "revert", target={"kind": "resource"})

    asyncio.run(run())
    assert stub.protocol.calls[0][2] == {"_meta": {"ahp-py/operation": {"message": "m"}}}
    assert stub.protocol.calls[1][2] == {}
    assert stub.protocol.calls[2][2] == {"target": {"kind": "resource"}}
