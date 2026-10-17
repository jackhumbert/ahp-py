"""Structured turns: a message with an output schema ends with a validated result.

The host owns the whole exchange -- it offers the StructuredOutput host tool,
validates what the agent sends, lets it retry, and falls back to prompt-and-parse
for a provider that cannot take a tool -- so every case here drives a real Host
over the in-memory transport against a scripted agent and reads the outcome out
of the finished turn in chat state.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import pytest
from ahp_protocol.ext.structured import (
    OUTPUT_SCHEMA_META_KEY,
    STRUCTURED_OUTPUT_ERROR_TYPE,
    STRUCTURED_OUTPUT_TOOL,
    STRUCTURED_OUTPUT_TOOL_META_KEY,
    tool_input_schema,
)

from ahp_host.core import Host, LoopbackSingleUserPolicy
from ahp_host.core.structured import TOOL_FOLLOW_UP, schema_text
from ahp_host.provider import EchoProvider, EchoSession
from ahp_host.provider.base import (
    HOST_CLIENT_ID,
    AgentSessionContext,
    ClientToolCall,
    ToolResult,
    TurnSink,
    UserMessage,
)
from tests.unit.hosting import (
    Wire,
    assert_frames_valid,
    connect,
    finished,
    open_session,
    shut,
    state,
    turn_started,
)

pytestmark = pytest.mark.anyio

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"answer": {"type": "integer"}},
    "required": ["answer"],
    "additionalProperties": False,
}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class ScriptedSession(EchoSession):
    """Records every message and tool outcome; replays a script per send.

    *capable*: each entry is a list of argument payloads (or ``(name, args)``
    pairs) sent through ``run_client_tool``, stopping at the first accepted
    result, or a string streamed as text. Not capable: each entry is a reply
    streamed as text. A missing entry means the send does nothing. With no
    script it is plain EchoSession.
    """

    def __init__(
        self,
        context: AgentSessionContext,
        *,
        script: Sequence[Any] | None,
        capable: bool,
        structured: bool,
    ) -> None:
        super().__init__(context, structured=structured)
        self.script = script
        self.capable = capable
        self.messages: list[UserMessage] = []
        self.results: list[ToolResult | LookupError] = []
        self._calls = 0

    def offers_host_tools(self) -> bool:
        return self.capable if self.script is not None else super().offers_host_tools()

    async def send_user_message(self, message: UserMessage, sink: TurnSink) -> None:
        index = len(self.messages)
        self.messages.append(message)
        if self.script is None:
            await super().send_user_message(message, sink)
            return
        entry = self.script[index] if index < len(self.script) else None
        if not self.capable or isinstance(entry, str):
            if entry is not None:
                await sink.text_delta(entry)
            return
        for payload in entry or []:
            name, arguments = (
                payload if isinstance(payload, tuple) else (STRUCTURED_OUTPUT_TOOL, payload)
            )
            self._calls += 1
            try:
                result = await sink.run_client_tool(
                    ClientToolCall(
                        call_id=f"so-{self._calls}",
                        name=name,
                        client_id=HOST_CLIENT_ID,
                        tool_input=arguments,
                    )
                )
            except LookupError as exc:
                self.results.append(exc)
                continue
            self.results.append(result)
            if result.accepted:
                break


class Scripted(EchoProvider):
    def __init__(
        self,
        script: Sequence[Any] | None = None,
        *,
        capable: bool = True,
        structured: bool = False,
    ) -> None:
        super().__init__()
        self.script = script
        self.capable = capable
        self.structured = structured
        self.sessions: list[ScriptedSession] = []

    async def create_session(self, context: AgentSessionContext) -> ScriptedSession:
        session = ScriptedSession(
            context, script=self.script, capable=self.capable, structured=self.structured
        )
        self.sessions.append(session)
        return session


@dataclass
class Outcome:
    wire: Wire
    chat: str
    session: ScriptedSession
    turn: dict[str, Any]

    @property
    def calls(self) -> list[dict[str, Any]]:
        """The StructuredOutput tool calls, as the reduced `toolCall` states."""
        return [
            part["toolCall"]
            for part in self.turn["responseParts"]
            if part["kind"] == "toolCall" and part["toolCall"]["toolName"] == STRUCTURED_OUTPUT_TOOL
        ]

    @property
    def errors(self) -> list[dict[str, Any]]:
        return [part["error"] for part in self.turn["responseParts"] if part["kind"] == "error"]

    @property
    def notices(self) -> list[str]:
        return [
            part["content"]
            for part in self.turn["responseParts"]
            if part["kind"] == "systemNotification"
        ]


@asynccontextmanager
async def driven(
    provider: Scripted, *, text: str = "hello", **message: Any
) -> AsyncIterator[Outcome]:
    host = Host(provider, LoopbackSingleUserPolicy())
    wire, serving = await connect(host)
    try:
        chat = await open_session(wire, "echo:/structured")
        await wire.dispatch(chat, turn_started("t1", text, **message))
        assert await wire.until(lambda: finished(host, chat, "t1")), state(host, chat)
        [session] = provider.sessions
        yield Outcome(wire, chat, session, state(host, chat)["turns"][-1])
    finally:
        await shut(host, wire, serving)


def _meta(schema: Any = SCHEMA) -> dict[str, Any]:
    return {"_meta": {OUTPUT_SCHEMA_META_KEY: schema}}


async def test_structured_turns_accept() -> None:
    provider = Scripted([[{"answer": 42}]])
    async with driven(provider, **_meta()) as out:
        [call] = out.calls
        assert call["status"] == "completed"
        assert call["success"] is True
        assert call["structuredContent"] == {"answer": 42}
        assert call["_meta"][STRUCTURED_OUTPUT_TOOL_META_KEY] is True
        assert out.errors == []

        [first] = out.session.messages
        [tool] = first.host_tools
        assert tool.name == STRUCTURED_OUTPUT_TOOL
        assert tool.input_schema == SCHEMA
        assert first.text.startswith("hello")
        assert "StructuredOutput" in first.text
        assert first.raw["_meta"] == {OUTPUT_SCHEMA_META_KEY: SCHEMA}
        assert first.continues_turn is False
        assert_frames_valid(out.wire, ("chat", out.chat))


async def test_structured_turns_retry_after_a_mismatch() -> None:
    provider = Scripted([[{"answer": "x"}, {"answer": 7}]])
    async with driven(provider, **_meta()) as out:
        first, second = out.session.results
        assert isinstance(first, ToolResult)
        assert not first.accepted
        assert first.reason is not None
        assert "answer" in first.reason
        assert "2 attempt(s) left" in first.reason
        assert isinstance(second, ToolResult)
        assert second.accepted
        assert [call["success"] for call in out.calls] == [False, True]
        assert out.calls[-1]["structuredContent"] == {"answer": 7}
        assert out.errors == []
        assert out.turn["state"] == "complete"


async def test_structured_turns_exhausted_fails_the_turn() -> None:
    provider = Scripted([[{"answer": "a"}, {"answer": "b"}, {"answer": "c"}, {"answer": 1}]])
    async with driven(provider, **_meta()) as out:
        last = out.session.results[3]
        assert isinstance(last, ToolResult)
        assert not last.accepted
        assert last.reason is not None
        assert "No attempts left" in last.reason
        assert len(out.calls) == 3
        [error] = out.errors
        assert error["errorType"] == STRUCTURED_OUTPUT_ERROR_TYPE
        assert "3 attempt(s)" in error["message"]
        assert len(out.session.messages) == 1
        assert out.turn["state"] == "error"


async def test_structured_turns_follow_up_when_never_called() -> None:
    provider = Scripted([[], [{"answer": 5}]])
    async with driven(provider, **_meta()) as out:
        first, second = out.session.messages
        [tool] = first.host_tools
        assert second.continues_turn is True
        assert second.text == TOOL_FOLLOW_UP.format(schema=schema_text(tool.input_schema))
        assert second.host_tools == (tool,)
        assert any("attempt 2 of 3" in notice for notice in out.notices)
        assert out.calls[-1]["structuredContent"] == {"answer": 5}
        assert out.errors == []


async def test_structured_turns_never_called_fails() -> None:
    # Three tries with the tool, then one prompt-and-parse try, which also fails.
    provider = Scripted([[], [], [], "no json here"])
    async with driven(provider, **_meta()) as out:
        assert len(out.session.messages) == 4
        assert out.session.messages[3].host_tools == ()
        assert out.session.messages[3].continues_turn is True
        [error] = out.errors
        assert error["errorType"] == STRUCTURED_OUTPUT_ERROR_TYPE
        assert error["message"] == (
            "The agent did not call StructuredOutput, and its reply had no valid result "
            "either: The reply contained no JSON."
        )
        assert out.turn["state"] == "error"


async def test_structured_turns_never_called_falls_back_to_the_reply() -> None:
    # An agent that cached its tool list before StructuredOutput existed (an ACP
    # agent that never re-lists) still returns a result, in its reply.
    provider = Scripted([[], [], [], 'Here:\n```json\n{"answer": 9}\n```'])
    async with driven(provider, **_meta()) as out:
        assert len(out.session.messages) == 4
        assert any("never called StructuredOutput" in n for n in out.notices)
        assert out.calls[-1]["structuredContent"] == {"answer": 9}
        assert out.errors == []
        assert out.turn["state"] == "complete"


async def test_structured_turns_unresolvable_ref_fails_with_the_code() -> None:
    schema = {"type": "object", "properties": {"a": {"$ref": "#/$defs/missing"}}}
    provider = Scripted([[{"a": 1}]])
    async with driven(provider, **_meta(schema)) as out:
        [error] = out.errors
        assert error["errorType"] == STRUCTURED_OUTPUT_ERROR_TYPE
        assert "does not resolve" in error["message"]
        assert len(out.session.messages) == 1
        assert out.turn["state"] == "error"


@pytest.mark.parametrize(
    "ref",
    ["http://127.0.0.1:9/schema.json", "file:///etc/passwd", "other.json#/x"],
)
async def test_structured_turns_external_ref_is_refused_not_fetched(ref: str) -> None:
    # The schema comes from any client: a reference outside it is never fetched.
    schema = {"type": "object", "properties": {"a": {"$ref": ref}}}
    provider = Scripted([[{"a": 1}]])
    async with driven(provider, **_meta(schema)) as out:
        [error] = out.errors
        assert error["errorType"] == STRUCTURED_OUTPUT_ERROR_TYPE
        assert "may only reference itself" in error["message"]
        assert out.session.messages == []


async def test_structured_turns_fallback_parses_the_reply() -> None:
    provider = Scripted(
        ['Sure.\n```json\n{"answer": "no"}\n```', 'Fixed:\n```json\n{"answer": 3}\n```'],
        capable=False,
    )
    async with driven(provider, **_meta()) as out:
        first, second = out.session.messages
        assert first.host_tools == ()
        assert '"answer"' in first.text
        assert "```json" in first.text
        assert second.continues_turn is True
        assert second.text.startswith("Your last reply did not contain a valid result")
        [call] = out.calls
        assert call["structuredContent"] == {"answer": 3}
        assert call["success"] is True
        assert out.errors == []


async def test_structured_turns_fallback_exhausted() -> None:
    provider = Scripted(["no json", "still none", '{"answer": "x"}'], capable=False)
    async with driven(provider, **_meta()) as out:
        assert len(out.session.messages) == 3
        [error] = out.errors
        assert error["errorType"] == STRUCTURED_OUTPUT_ERROR_TYPE


async def test_structured_turns_wrap_a_non_object_schema() -> None:
    schema = {"type": "array", "items": {"type": "integer"}}
    provider = Scripted([[[1, 2], {"value": [1, 2]}]])
    async with driven(provider, **_meta(schema)) as out:
        [tool] = out.session.messages[0].host_tools
        assert tool.input_schema == tool_input_schema(schema)
        first = out.session.results[0]
        assert isinstance(first, ToolResult)
        assert not first.accepted
        assert first.reason is not None
        assert "must be a JSON object" in first.reason
        assert out.calls[-1]["structuredContent"] == {"value": [1, 2]}


async def test_structured_turns_invalid_schema_fails_before_the_agent() -> None:
    provider = Scripted([[{"answer": 1}]])
    async with driven(provider, **_meta({"type": "nope"})) as out:
        [error] = out.errors
        assert error["errorType"] == STRUCTURED_OUTPUT_ERROR_TYPE
        assert "not a valid JSON Schema" in error["message"]
        assert out.session.messages == []


async def test_structured_turns_stock_client_is_unaffected() -> None:
    provider = Scripted([[{"answer": 1}]])
    async with driven(provider) as out:
        [message] = out.session.messages
        assert message.host_tools == ()
        assert message.text == "hello"
        assert message.continues_turn is False
        assert out.turn["state"] == "complete"
        assert out.calls == []

    async with driven(Scripted(), **_meta("x")) as out:
        [error] = out.errors
        assert error["errorType"] == STRUCTURED_OUTPUT_ERROR_TYPE
        assert out.session.messages == []


async def test_structured_turns_unknown_host_tool() -> None:
    provider = Scripted([[("Nope", {})]])
    async with driven(provider, **_meta()) as out:
        [result, *_] = out.session.results
        assert isinstance(result, LookupError)
        assert [
            part
            for part in out.turn["responseParts"]
            if part["kind"] == "toolCall" and part["toolCall"]["toolName"] == "Nope"
        ] == []


async def test_structured_turns_echo_provider_structured() -> None:
    async with driven(Scripted(structured=True), text='{"answer": 42}', **_meta()) as out:
        assert out.calls[-1]["structuredContent"] == {"answer": 42}
        assert out.errors == []

    async with driven(Scripted(structured=True), text="nope", **_meta()) as out:
        assert len(out.session.messages) == 3
        [error] = out.errors
        assert error["errorType"] == STRUCTURED_OUTPUT_ERROR_TYPE
