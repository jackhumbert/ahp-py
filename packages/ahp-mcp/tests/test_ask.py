"""A session that waits on the user is put to them through the MCP client.

The echo agent asks for real: with `confirm_tools` it waits for permission to
run its tool, with `elicit` it asks a question. The MCP side is the SDK's own
client with an elicitation callback standing in for the user, under both ways a
protocol revision carries the question: a request from server to client mid
call (`legacy`), and a result that asks and is retried (2026-07-28).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from mcp import types
from mcp.server.elicitation import render_elicitation_schema

from ahp_mcp import Fleet
from ahp_mcp.ask import _answer_value, prompt_for
from ahp_mcp.fleet import _chat_state
from tests.support import World, mcp_client
from tests.test_tools import call

MODES = ["legacy", "2026-07-28"]


class User:
    """Answers each prompt from a script, and keeps what it was shown."""

    def __init__(self, *answers: types.ElicitResult) -> None:
        self.answers = list(answers)
        self.shown: list[types.ElicitRequestParams] = []

    async def __call__(
        self, context: Any, params: types.ElicitRequestParams
    ) -> types.ElicitResult | types.ErrorData:
        self.shown.append(params)
        return self.answers.pop(0)


def accept(**content: Any) -> types.ElicitResult:
    return types.ElicitResult(action="accept", content=content)


def schema(user: User, index: int = 0) -> dict[str, Any]:
    shown = user.shown[index]
    assert isinstance(shown, types.ElicitRequestFormParams)
    return dict(shown.requested_schema)


async def answers_recorded(fleet: Fleet, session: str) -> Any:
    """The answers the session's last question was resolved with."""
    chat = _chat_state(await fleet.client(), session)
    for part in chat["turns"][-1]["responseParts"]:
        if part.get("kind") == "inputRequest":
            return part["request"].get("answers")
    return None


@pytest.fixture
async def confirming(tmp_path: Path) -> AsyncIterator[World]:
    world = World(tmp_path, confirm_tools=True)
    try:
        yield world
    finally:
        await world.aclose()


@pytest.fixture
async def asking(tmp_path: Path) -> AsyncIterator[World]:
    world = World(tmp_path, elicit=True)
    try:
        yield world
    finally:
        await world.aclose()


@pytest.mark.parametrize("mode", MODES)
async def test_the_user_allows_a_tool_and_the_session_goes_on(confirming: World, mode: str) -> None:
    user = User(accept(decision="Allow"))
    fleet = Fleet(confirming.connect_gateway)
    async with mcp_client(fleet, elicitation_callback=user, mode=mode) as mcp:
        started = await call(mcp, "start_session", host="node-a", prompt="ping")
        session = started["session"]

        answered = await call(mcp, "wait_for_session", session=session, timeout_seconds=10)
        assert "The user chose 'Allow'." in answered["next"]
        message = user.shown[0].message
        assert "Echo Tool" in message
        assert '"text": "ping"' in message
        assert schema(user)["properties"]["decision"]["enum"] == ["Allow", "Deny"]

        finished = await call(mcp, "wait_for_session", session=session, timeout_seconds=10)
        assert finished["status"] == "done"
        read = await call(mcp, "read_session", session=session)
        assert "You said: ping" in read["transcript"]
        assert len(user.shown) == 1


async def test_a_denial_carries_the_users_note(confirming: World) -> None:
    user = User(accept(decision="Deny", note="not now"))
    async with mcp_client(Fleet(confirming.connect_gateway), elicitation_callback=user) as mcp:
        started = await call(mcp, "start_session", host="node-b", prompt="ping")
        session = started["session"]
        answered = await call(mcp, "wait_for_session", session=session, timeout_seconds=10)
        assert "The user chose 'Deny'." in answered["next"]
        await call(mcp, "wait_for_session", session=session, timeout_seconds=10)
        read = await call(mcp, "read_session", session=session)
        assert "(denied)" in read["transcript"]


@pytest.mark.parametrize("mode", MODES)
async def test_the_user_answers_the_agents_question(asking: World, mode: str) -> None:
    user = User(accept(q1="SHOUTING"))
    fleet = Fleet(asking.connect_gateway)
    async with mcp_client(fleet, elicitation_callback=user, mode=mode) as mcp:
        started = await call(mcp, "start_session", host="node-a", prompt="hello")
        session = started["session"]

        answered = await call(mcp, "wait_for_session", session=session, timeout_seconds=10)
        assert "answered the session's question" in answered["next"]
        assert "Echo it back how?" in user.shown[0].message
        field = schema(user)["properties"]["q1"]
        assert field["enum"] == ["Plain", "SHOUTING"]
        assert field["title"] == "Style"

        finished = await call(mcp, "wait_for_session", session=session, timeout_seconds=10)
        assert finished["status"] == "done"
        # The option's id went back, not its label, in the schema's answer shape.
        assert await answers_recorded(fleet, session) == {
            "style": {"state": "submitted", "value": {"kind": "selected", "value": "shout"}}
        }


async def test_a_dismissed_prompt_sends_nothing(confirming: World) -> None:
    user = User(types.ElicitResult(action="cancel"), accept(decision="Allow"))
    async with mcp_client(Fleet(confirming.connect_gateway), elicitation_callback=user) as mcp:
        started = await call(mcp, "start_session", host="node-a", prompt="ping")
        session = started["session"]

        dismissed = await call(mcp, "wait_for_session", session=session, timeout_seconds=10)
        assert dismissed["status"] == "waiting on the user"
        assert "without answering" in dismissed["next"]

        # Asked again, and this time answered.
        await call(mcp, "wait_for_session", session=session, timeout_seconds=10)
        finished = await call(mcp, "wait_for_session", session=session, timeout_seconds=10)
        assert finished["status"] == "done"
        assert len(user.shown) == 2


async def test_without_elicitation_the_user_answers_elsewhere(confirming: World) -> None:
    async with mcp_client(Fleet(confirming.connect_gateway)) as mcp:
        started = await call(mcp, "start_session", host="node-a", prompt="ping")
        session = started["session"]

        waited = await call(mcp, "wait_for_session", session=session, timeout_seconds=10)
        assert waited["status"] == "waiting on the user"
        assert waited["waiting_on"] == ["toolConfirmation"]
        assert waited["asks"].startswith("permission to run Run echo tool")
        assert "AHP apps" in waited["next"]

        read = await call(mcp, "read_session", session=session)
        assert "AHP apps" in read["next"]
        # Nothing answered for the user.
        again = await call(mcp, "wait_for_session", session=session, timeout_seconds=0)
        assert again["status"] == "waiting on the user"
        await call(mcp, "cancel_session", session=session)


async def test_read_session_points_at_the_prompt_when_the_client_can_ask(
    confirming: World,
) -> None:
    user = User()
    async with mcp_client(Fleet(confirming.connect_gateway), elicitation_callback=user) as mcp:
        started = await call(mcp, "start_session", host="node-a", prompt="ping")
        session = started["session"]
        for _ in range(400):
            read = await call(mcp, "read_session", session=session)
            if read["status"] == "waiting on the user":
                break
            await asyncio.sleep(0.025)
        assert read["next"] == "Call wait_for_session to ask the user here."
        assert user.shown == []
        await call(mcp, "cancel_session", session=session)


async def test_no_tool_takes_an_answer_from_the_model(confirming: World) -> None:
    async with mcp_client(Fleet(confirming.connect_gateway)) as mcp:
        tools = {tool.name: tool for tool in (await mcp.list_tools()).tools}
        assert set(tools["wait_for_session"].input_schema["properties"]) == {
            "session",
            "timeout_seconds",
        }
        assert tools["wait_for_session"].annotations is not None
        assert tools["wait_for_session"].annotations.read_only_hint is False


def test_every_question_kind_is_a_form_the_spec_allows() -> None:
    entry = {
        "id": "e1",
        "kind": "chatInput",
        "chat": "chat:/1",
        "request": {
            "id": "r1",
            "message": "A few things",
            "questions": [
                {"id": "name", "kind": "text", "message": "Name", "required": True, "max": 40},
                {"id": "count", "kind": "integer", "message": "How many", "min": 1},
                {"id": "ratio", "kind": "number", "message": "Ratio"},
                {"id": "ok", "kind": "boolean", "message": "OK?", "defaultValue": True},
                {
                    "id": "tags",
                    "kind": "multi-select",
                    "message": "Tags",
                    "options": [{"id": "a", "label": "Alpha"}, {"id": "b", "label": "Beta"}],
                },
            ],
        },
    }
    prompt = prompt_for(entry)
    assert prompt is not None
    rendered = render_elicitation_schema(prompt.schema)
    assert rendered["required"] == ["q1"]
    assert rendered["properties"]["q5"]["items"]["enum"] == ["Alpha", "Beta"]
    answer = prompt.schema.model_validate({"q1": "Ada", "q5": ["Beta"]})
    values = {
        q.question_id: _answer_value(q, getattr(answer, name))
        for name, q in prompt.questions.items()
    }
    assert values == {"name": "Ada", "count": None, "ratio": None, "ok": True, "tags": ["b"]}


def test_a_hosts_own_options_replace_allow_and_deny() -> None:
    entry = {
        "id": "e2",
        "kind": "toolConfirmation",
        "chat": "chat:/1",
        "turnId": "t1",
        "toolCall": {
            "toolCallId": "c1",
            "toolName": "bash",
            "displayName": "Bash",
            "status": "pending-confirmation",
            "toolInput": "ls",
            "options": [
                {"id": "once", "label": "Allow once", "kind": "approve"},
                {"id": "always", "label": "Allow for this session", "kind": "approve"},
                {"id": "no", "label": "Deny", "kind": "deny"},
            ],
        },
    }
    prompt = prompt_for(entry, session_title="tidy up")
    assert prompt is not None
    assert prompt.message.startswith("A session (tidy up) asks to run Bash")
    assert prompt.decisions == {
        "Allow once": ("once", True),
        "Allow for this session": ("always", True),
        "Deny": ("no", False),
    }
    render_elicitation_schema(prompt.schema)


def test_a_sign_in_or_a_bare_link_is_not_asked_here() -> None:
    assert prompt_for({"id": "e", "kind": "toolAuthentication", "toolCall": {}}) is None
    link = {"id": "e", "kind": "chatInput", "request": {"id": "r", "url": "https://example.com"}}
    assert prompt_for(link) is None
