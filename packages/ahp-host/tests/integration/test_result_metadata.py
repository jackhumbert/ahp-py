"""`ToolCallResult.resultMetadata` (upstream `c301d494`): MCP `CallToolResult._meta`.

It rides next to `structuredContent` through the result. The host adds it from
`TurnSink.tool_call_completed(result_metadata=)` only when given, and a client
tool's own `chat/toolCallComplete` keeps it in state because the host publishes
the client's action as dispatched.
"""

from __future__ import annotations

from typing import Any

import pytest

from ahp_host.core import Host, LoopbackSingleUserPolicy
from ahp_host.provider import EchoProvider, EchoSession
from ahp_host.provider.base import TurnSink, UserMessage
from tests.unit.hosting import connect, open_session, shut, state, turn_started

from .test_client_tool_ownership import (
    _attach,
    _dispatch,
    _parked,
    _send,
    _session,
    _tool_call,
    _turn_over,
)

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class Completing(EchoSession):
    async def send_user_message(self, message: UserMessage, sink: TurnSink) -> None:
        text = message.text
        await sink.tool_call_started("c1", "lookup", {"q": text})
        if text == "with":
            await sink.tool_call_completed(
                "c1",
                {"content": [{"type": "text", "text": "ok"}], "structuredContent": {"n": 1}},
                result_metadata={"app": {"only": True}},
            )
        elif text == "empty":
            await sink.tool_call_completed("c1", None, result_metadata={})
        elif text == "override":
            await sink.tool_call_completed(
                "c1", {"resultMetadata": {"from": "result"}}, result_metadata={"from": "keyword"}
            )
        else:
            await sink.tool_call_completed("c1", {"content": []})


class CompletingProvider(EchoProvider):
    async def create_session(self, context: Any) -> Completing:
        return Completing(context)


def _result(host: Host, chat: str) -> dict[str, Any]:
    turns = state(host, chat)["turns"]
    [call] = [p for p in turns[-1]["responseParts"] if p.get("kind") == "toolCall"]
    # The reducer spreads a result into the call, next to `status`.
    result: dict[str, Any] = call["toolCall"]
    return result


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("with", {"app": {"only": True}}),
        ("empty", {}),
        ("override", {"from": "keyword"}),
    ],
)
async def test_the_sink_puts_result_metadata_on_the_result(
    text: str, expected: dict[str, Any]
) -> None:
    host = Host(CompletingProvider(), LoopbackSingleUserPolicy())
    wire, serving = await connect(host)
    try:
        chat = await open_session(wire, f"echo:/rm-{text}")
        await wire.dispatch(chat, turn_started("t1", text))
        assert await wire.until(lambda: bool(state(host, chat).get("turns")))
        result = _result(host, chat)
        assert result["resultMetadata"] == expected
        if text == "with":
            assert result["structuredContent"] == {"n": 1}, "next to structuredContent"
    finally:
        await shut(host, wire, serving)


async def test_it_is_absent_unless_given() -> None:
    host = Host(CompletingProvider(), LoopbackSingleUserPolicy())
    wire, serving = await connect(host)
    try:
        chat = await open_session(wire, "echo:/rm-none")
        await wire.dispatch(chat, turn_started("t1", "plain"))
        assert await wire.until(lambda: bool(state(host, chat).get("turns")))
        assert "resultMetadata" not in _result(host, chat)
    finally:
        await shut(host, wire, serving)


async def test_a_client_tools_result_metadata_stays_in_state() -> None:
    host = Host(EchoProvider(client_tools=True), LoopbackSingleUserPolicy())
    try:
        client = await _attach(host, "owner")
        _, chat_uri = await _session(host, client, "echo:/rm-client", owner="owner")
        await _send(client, chat_uri)
        await _parked(host, client, chat_uri)
        await _dispatch(
            client,
            chat_uri,
            {
                "type": "chat/toolCallComplete",
                "turnId": "t1",
                "toolCallId": "client-tool-1",
                "result": {
                    "success": True,
                    "pastTenseMessage": "Found usages",
                    "content": [{"type": "text", "text": "3 usages"}],
                    "structuredContent": {"count": 3},
                    "resultMetadata": {"ui": {"resourceUri": "ui://usages"}},
                },
            },
        )
        await _turn_over(client, chat_uri)
        result = _tool_call(host, chat_uri)
        assert result["resultMetadata"] == {"ui": {"resourceUri": "ui://usages"}}
        assert result["structuredContent"] == {"count": 3}
    finally:
        await host.aclose()
