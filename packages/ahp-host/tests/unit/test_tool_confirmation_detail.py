"""What a tool confirmation offers, and everything its answer carries back.

`ToolConfirmationOutcome` kept `approved` and `tool_input` and dropped the rest
of `chat/toolCallConfirmed`: a user who denied a call and said why -- "no, edit
the other file" -- was heard by the agent as a bare "no", so it tried the same
thing again. And a provider could offer neither richer choices
(`ConfirmationOption`) nor a diff to look at before deciding (`edits`), both of
which `chat/toolCallReady` declares.
"""

from __future__ import annotations

from typing import Any

import pytest
from ahp_protocol.channels import ROOT_URI

from ahp_host.core import Host, LoopbackSingleUserPolicy
from ahp_host.provider import EchoProvider, EchoSession
from ahp_host.provider.base import (
    AgentSessionContext,
    ConfirmationOption,
    ToolConfirmation,
    ToolConfirmationOutcome,
    TurnSink,
    UserMessage,
)
from ahp_host.provider.changes import FileChange

from .hosting import Wire, assert_frames_valid, connect, open_session, shut, state, turn_started

pytestmark = pytest.mark.anyio

_OPTIONS = (
    ConfirmationOption(id="once", label="Allow once", kind="approve", group=1),
    ConfirmationOption(id="always", label="Allow in this session", kind="approve", group=1),
    ConfirmationOption(id="deny", label="Deny with reason", kind="deny", group=2),
)
_EDIT = FileChange(uri="file:///work/a.txt", before=b"one\ntwo\n", after=b"one\n2\nthree\n")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class Asking(EchoSession):
    def __init__(self, context: AgentSessionContext) -> None:
        super().__init__(context)
        self.outcomes: list[ToolConfirmationOutcome] = []

    async def send_user_message(self, message: UserMessage, sink: TurnSink) -> None:
        await sink.tool_call_started("call-1", "edit", {"path": "a.txt"}, display_name="Edit")
        outcome = await sink.confirm_tool_call(
            ToolConfirmation(
                call_id="call-1",
                name="edit",
                invocation_message="Edit a.txt",
                tool_input={"path": "a.txt"},
                confirmation_title="Write file",
                options=_OPTIONS,
                edits=(_EDIT,),
            )
        )
        self.outcomes.append(outcome)
        await sink.tool_call_completed("call-1", success=outcome.approved)


class AskingProvider(EchoProvider):
    def __init__(self) -> None:
        super().__init__()
        self.sessions: list[Asking] = []

    async def create_session(self, context: AgentSessionContext) -> Asking:
        session = Asking(context)
        self.sessions.append(session)
        return session


def _pending(host: Host, chat: str) -> dict[str, Any]:
    active = state(host, chat).get("activeTurn") or {}
    for part in active.get("responseParts", []):
        call = part.get("toolCall")
        if isinstance(call, dict) and call.get("status") == "pending-confirmation":
            return call
    return {}


async def _asked(host: Host, wire: Wire, uri: str) -> str:
    chat = await open_session(wire, uri)
    await wire.dispatch(chat, turn_started("t1", "edit it"))
    assert await wire.until(lambda: bool(_pending(host, chat))), state(host, chat)
    return chat


def _confirm(**fields: Any) -> dict[str, Any]:
    return {"type": "chat/toolCallConfirmed", "turnId": "t1", "toolCallId": "call-1", **fields}


def test_an_option_round_trips_its_wire_shape() -> None:
    option = ConfirmationOption(id="x", label="X", kind="deny", group=3)
    assert option.to_wire() == {"id": "x", "label": "X", "kind": "deny", "group": 3}
    assert ConfirmationOption.from_wire(option.to_wire()) == option
    assert ConfirmationOption(id="y", label="Y").to_wire() == {
        "id": "y",
        "label": "Y",
        "kind": "approve",
    }
    assert ConfirmationOption.from_wire({"id": 1, "label": "nope"}) is None


async def test_the_ready_carries_options_and_an_edit_preview() -> None:
    host = Host(AskingProvider(), LoopbackSingleUserPolicy())
    wire, serving = await connect(host)
    try:
        uri = "echo:/conf-1"
        chat = await _asked(host, wire, uri)
        call = _pending(host, chat)
        assert call["options"] == [o.to_wire() for o in _OPTIONS]
        [edit] = call["edits"]["items"]
        assert edit["before"]["uri"] == edit["after"]["uri"] == _EDIT.uri
        assert edit["diff"] == {"added": 2, "removed": 1}

        # The preview is a diff, not a label: each side reads back.
        for side, expected in (("before", _EDIT.before), ("after", _EDIT.after)):
            response = await wire.request(
                "resourceRead", {"channel": ROOT_URI, "uri": edit[side]["content"]["uri"]}
            )
            assert response["result"]["data"].encode() == expected, side

        # And the session-level mirror -- what a client answering from the
        # session list renders -- carries the same decision material.
        [needed] = state(host, uri)["inputNeeded"]
        mirrored = needed["toolCall"]
        assert mirrored["options"] == call["options"]
        assert mirrored["edits"] == call["edits"]
        assert mirrored["confirmationTitle"] == "Write file"
        assert mirrored["toolInput"] == call["toolInput"]
        assert_frames_valid(wire, ("chat", chat), ("session", uri))
    finally:
        await shut(host, wire, serving)


async def test_a_denial_carries_its_reason_suggestion_and_option() -> None:
    provider = AskingProvider()
    host = Host(provider, LoopbackSingleUserPolicy())
    wire, serving = await connect(host)
    try:
        chat = await _asked(host, wire, "echo:/conf-2")
        seq = await wire.dispatch(
            chat,
            _confirm(
                approved=False,
                reason="denied",
                reasonMessage={"markdown": "not **that** file"},
                userSuggestion={"text": "edit b.txt instead", "origin": {"kind": "user"}},
                selectedOptionId="deny",
            ),
        )
        assert "rejectionReason" not in await wire.echoed(chat, seq)
        assert await wire.until(lambda: bool(provider.sessions[0].outcomes))
        outcome = provider.sessions[0].outcomes[0]
        assert not outcome.approved
        assert outcome.reason == "denied"
        assert outcome.reason_message == "not **that** file"
        assert outcome.user_suggestion is not None
        assert outcome.user_suggestion.text == "edit b.txt instead"
        assert outcome.selected_option == _OPTIONS[2]
        assert outcome.selected_option_id == "deny"
    finally:
        await shut(host, wire, serving)


async def test_an_approval_carries_the_option_it_picked() -> None:
    provider = AskingProvider()
    host = Host(provider, LoopbackSingleUserPolicy())
    wire, serving = await connect(host)
    try:
        chat = await _asked(host, wire, "echo:/conf-3")
        await wire.dispatch(
            chat, _confirm(approved=True, confirmed="user-action", selectedOptionId="always")
        )
        assert await wire.until(lambda: bool(provider.sessions[0].outcomes))
        outcome = provider.sessions[0].outcomes[0]
        assert outcome.approved
        assert outcome.selected_option == _OPTIONS[1]
        assert outcome.reason is None
        assert outcome.reason_message is None
        assert outcome.user_suggestion is None
    finally:
        await shut(host, wire, serving)


@pytest.mark.parametrize(
    ("fields", "reason"),
    [
        (
            {"approved": True, "confirmed": "user-action", "selectedOptionId": "nope"},
            "selectedOptionId does not name an option this confirmation offered",
        ),
        (
            {"approved": True, "confirmed": "user-action", "selectedOptionId": "deny"},
            "option 'deny' has kind 'deny', which contradicts approved",
        ),
        (
            {"approved": False, "reason": "denied", "selectedOptionId": "once"},
            "option 'once' has kind 'approve', which contradicts approved",
        ),
        (
            {"approved": True, "confirmed": "user-action", "selectedOptionId": 3},
            "selectedOptionId must be a string",
        ),
    ],
)
async def test_an_option_the_call_did_not_offer_is_rejected(
    fields: dict[str, Any], reason: str
) -> None:
    """The reducer drops an unknown id silently, so the transcript would show
    no choice while the provider acted on one; and an approval that picks a
    deny option is a contradiction nobody can act on."""
    provider = AskingProvider()
    host = Host(provider, LoopbackSingleUserPolicy())
    wire, serving = await connect(host)
    try:
        chat = await _asked(host, wire, "echo:/conf-4")
        seq = await wire.dispatch(chat, _confirm(**fields))
        assert (await wire.echoed(chat, seq))["rejectionReason"] == reason
        # Still asking: the refusal resolved nothing.
        assert _pending(host, chat)
        assert provider.sessions[0].outcomes == []
    finally:
        await shut(host, wire, serving)
