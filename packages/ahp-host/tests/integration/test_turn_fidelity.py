"""What a turn actually looks like once it reaches a client.

Every gap here renders as *something*, which is why none of them were caught by
a test that asserts the turn completed: prose landing above the tool call it
comments on, a queued follow-up that sits in its chip forever, a context gauge
that does not exist because nothing ever told the client a number.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from agent_host_protocol.channels import ROOT_URI
from agent_host_protocol.transport import memory_pair

from agent_host_server.core import Host, LoopbackSingleUserPolicy
from agent_host_server.core.turn import ActionTurnSink
from agent_host_server.provider import EchoProvider

from .test_host_end_to_end import FakeClient

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def host() -> AsyncIterator[Host]:
    made = Host(EchoProvider(), LoopbackSingleUserPolicy())
    try:
        yield made
    finally:
        await made.aclose()


@pytest.fixture
async def tooled() -> AsyncIterator[Host]:
    made = Host(EchoProvider(confirm_tools=True), LoopbackSingleUserPolicy())
    try:
        yield made
    finally:
        await made.aclose()


async def _client(host: Host) -> FakeClient:
    client_transport, server_transport = memory_pair()
    task = asyncio.create_task(host.serve(server_transport))
    client = FakeClient(client_transport)
    client.serve_task = task  # type: ignore[attr-defined]
    await client.request(
        "initialize",
        {
            "channel": ROOT_URI,
            "clientId": "c1",
            "protocolVersions": ["0.7.0"],
            "initialSubscriptions": [ROOT_URI],
        },
    )
    return client


async def _session(host: Host, client: FakeClient, uri: str) -> str:
    await client.request("createSession", {"channel": uri, "provider": "echo"})
    await client.collect(seconds=0.3)
    state = (await client.request("subscribe", {"channel": uri}))["result"]["snapshot"]["state"]
    chat: str = state["chats"][0]["resource"]
    await client.request("subscribe", {"channel": chat})
    return chat


async def _turn(client: FakeClient, chat: str, *, text: str = "hello", turn: str = "t1") -> None:
    await client.notify(
        "dispatchAction",
        {
            "channel": chat,
            "clientSeq": 1,
            "action": {
                "type": "chat/turnStarted",
                "turnId": turn,
                "startedAt": "1970-01-01T00:00:01.000Z",
                "message": {"text": text, "origin": {"kind": "user"}},
            },
        },
    )


def _actions(client: FakeClient, chat: str) -> list[dict[str, Any]]:
    return [
        note["params"]["action"]
        for note in client.notifications
        if note.get("method") == "action" and note["params"].get("channel") == chat
    ]


def _state(host: Host, uri: str) -> dict[str, Any]:
    state = host.sequencer.state_of(uri)
    assert isinstance(state, dict)
    return state


class TestResponsePartOrdering:
    """The client renders parts in creation order and appends a delta to
    whichever part its id names, so one markdown part per turn puts prose
    written AFTER a tool call above it."""

    async def test_prose_after_a_tool_call_is_its_own_part(self, tooled: Host) -> None:
        client = await _client(tooled)
        chat = await _session(tooled, client, "echo:/f-1")

        await _turn(client, chat, text="hello")
        await client.collect(seconds=0.4)
        ready = next(a for a in _actions(client, chat) if a["type"] == "chat/toolCallReady")
        await client.notify(
            "dispatchAction",
            {
                "channel": chat,
                "clientSeq": 2,
                "action": {
                    "type": "chat/toolCallConfirmed",
                    "turnId": "t1",
                    "toolCallId": ready["toolCallId"],
                    "approved": True,
                },
            },
        )
        await client.collect(seconds=0.6)

        kinds = [
            "tool" if a["type"] == "chat/toolCallStart" else a["part"]["kind"]
            for a in _actions(client, chat)
            if a["type"] in ("chat/toolCallStart", "chat/responsePart")
        ]
        assert kinds == ["tool", "markdown"], kinds

    async def test_a_run_of_text_still_shares_one_part(self, host: Host) -> None:
        """Only the switch is a boundary. A part per delta would be a part per
        token."""
        client = await _client(host)
        chat = await _session(host, client, "echo:/f-2")

        await _turn(client, chat, text="hello")
        await client.collect(seconds=0.5)

        parts = [a for a in _actions(client, chat) if a["type"] == "chat/responsePart"]
        deltas = [a for a in _actions(client, chat) if a["type"] == "chat/delta"]
        assert len(parts) == 1
        assert len(deltas) > 1
        assert {d["partId"] for d in deltas} == {parts[0]["part"]["id"]}


class TestSegmentation:
    """`_open_segment` on its own, where the transitions are cheap to state."""

    def test_switching_away_and_back_makes_a_new_part(self) -> None:
        sink = ActionTurnSink(None, "chat", "t1")  # type: ignore[arg-type]
        sink._markdown_part_id = "md-1"
        sink._segment = "markdown"

        sink._open_segment("tool")
        assert sink._markdown_part_id is None

        sink._markdown_part_id = "md-2"
        sink._segment = "markdown"
        sink._open_segment("markdown")
        assert sink._markdown_part_id == "md-2", "a run of one kind was split"

    def test_reasoning_and_text_interleave(self) -> None:
        sink = ActionTurnSink(None, "chat", "t1")  # type: ignore[arg-type]
        sink._open_segment("reasoning")
        sink._reasoning_part_id = "re-1"
        sink._open_segment("markdown")
        assert sink._reasoning_part_id is None


class TestQueuedMessages:
    """ "If the chat is idle when a queued message is set, the server SHOULD
    immediately consume it and start a new turn." We did not, so a follow-up
    typed while the agent worked stayed in its chip forever."""

    async def _queue(self, client: FakeClient, chat: str, id_: str, text: str, seq: int) -> None:
        await client.notify(
            "dispatchAction",
            {
                "channel": chat,
                "clientSeq": seq,
                "action": {
                    "type": "chat/pendingMessageSet",
                    "kind": "queued",
                    "id": id_,
                    "message": {"text": text, "origin": {"kind": "user"}},
                },
            },
        )

    async def test_a_message_queued_while_idle_runs_at_once(self, host: Host) -> None:
        client = await _client(host)
        chat = await _session(host, client, "echo:/q-1")

        await self._queue(client, chat, "q1", "hello", 1)
        await client.collect(seconds=0.6)

        state = _state(host, chat)
        assert state.get("queuedMessages") in (None, []), "the queue was never drained"
        assert any("hello" in str(turn) for turn in state["turns"]), "no turn ran it"

    async def test_a_message_queued_mid_turn_runs_after_it(self, tooled: Host) -> None:
        """The case that produced the report: the agent is busy, so the message
        waits -- and then nothing ever came back for it."""
        client = await _client(tooled)
        chat = await _session(tooled, client, "echo:/q-2")

        await _turn(client, chat, text="first")
        await client.collect(seconds=0.4)
        await self._queue(client, chat, "q1", "second", 2)
        await client.collect(seconds=0.3)
        assert _state(tooled, chat)["queuedMessages"], "drained while a turn was running"

        ready = next(a for a in _actions(client, chat) if a["type"] == "chat/toolCallReady")
        await client.notify(
            "dispatchAction",
            {
                "channel": chat,
                "clientSeq": 3,
                "action": {
                    "type": "chat/toolCallConfirmed",
                    "turnId": "t1",
                    "toolCallId": ready["toolCallId"],
                    "approved": True,
                },
            },
        )
        await client.collect(seconds=1.0)

        state = _state(tooled, chat)
        assert state.get("queuedMessages") in (None, [])
        # Running, not finished: this host parks every turn on a confirmation,
        # so the queued message shows up as the ACTIVE turn rather than in the
        # completed list.
        assert "second" in str(state["activeTurn"]), state["activeTurn"]

    async def test_the_host_says_it_consumed_the_message(self, host: Host) -> None:
        """`chat/pendingMessageRemoved` is "dispatched ... by the server when it
        consumes a message". Dropping it silently would leave every client that
        did not originate the message rendering a queue entry that is gone."""
        client = await _client(host)
        chat = await _session(host, client, "echo:/q-3")

        await self._queue(client, chat, "q1", "hello", 1)
        await client.collect(seconds=0.6)

        removals = [a for a in _actions(client, chat) if a["type"] == "chat/pendingMessageRemoved"]
        assert [a["id"] for a in removals] == ["q1"]

    async def test_two_queued_messages_run_in_order(self, host: Host) -> None:
        client = await _client(host)
        chat = await _session(host, client, "echo:/q-4")

        await self._queue(client, chat, "q1", "alpha", 1)
        await self._queue(client, chat, "q2", "bravo", 2)
        await client.collect(seconds=1.2)

        state = _state(host, chat)
        assert state.get("queuedMessages") in (None, [])
        ran = [t for t in state["turns"] if "alpha" in str(t) or "bravo" in str(t)]
        assert len(ran) == 2, f"both should have run: {state['turns']}"
        assert "alpha" in str(ran[0])
        assert "bravo" in str(ran[1])

    async def test_a_steering_message_is_left_alone(self, host: Host) -> None:
        """Steering is injected into the RUNNING turn, which needs a provider
        that can take it mid-flight. Consuming it as if it were queued would
        run it as its own turn, which is not what the user asked for."""
        client = await _client(host)
        chat = await _session(host, client, "echo:/q-5")

        await client.notify(
            "dispatchAction",
            {
                "channel": chat,
                "clientSeq": 1,
                "action": {
                    "type": "chat/pendingMessageSet",
                    "kind": "steering",
                    "id": "s1",
                    "message": {"text": "actually, stop", "origin": {"kind": "user"}},
                },
            },
        )
        await client.collect(seconds=0.5)

        assert _state(host, chat)["steeringMessage"]["id"] == "s1"


class TestUsage:
    """The client's rule is "no usage, no gauge" -- it renders nothing at all
    rather than a zero."""

    async def test_a_turn_reports_usage(self, host: Host) -> None:
        client = await _client(host)
        chat = await _session(host, client, "echo:/u-1")

        await _turn(client, chat, text="hello there")
        await client.collect(seconds=0.5)

        usage = [a for a in _actions(client, chat) if a["type"] == "chat/usage"]
        assert len(usage) == 1
        assert usage[0]["usage"]["inputTokens"] > 0
        assert usage[0]["turnId"] == "t1"

    async def test_it_lands_before_the_turn_completes(self, host: Host) -> None:
        """A usage report for a turn the client has already settled is a report
        it may not apply."""
        client = await _client(host)
        chat = await _session(host, client, "echo:/u-2")

        await _turn(client, chat, text="hello")
        await client.collect(seconds=0.5)

        types = [a["type"] for a in _actions(client, chat)]
        assert types.index("chat/usage") < types.index("chat/turnComplete")


class TestProgressiveToolOutput:
    async def test_a_running_tool_streams(self, tooled: Host) -> None:
        client = await _client(tooled)
        chat = await _session(tooled, client, "echo:/p-1")

        await _turn(client, chat, text="one two three")
        await client.collect(seconds=0.4)
        ready = next(a for a in _actions(client, chat) if a["type"] == "chat/toolCallReady")
        await client.notify(
            "dispatchAction",
            {
                "channel": chat,
                "clientSeq": 2,
                "action": {
                    "type": "chat/toolCallConfirmed",
                    "turnId": "t1",
                    "toolCallId": ready["toolCallId"],
                    "approved": True,
                },
            },
        )
        await client.collect(seconds=0.8)

        actions = _actions(client, chat)
        deltas = [a for a in actions if a["type"] == "chat/toolCallDelta"]
        content = [a for a in actions if a["type"] == "chat/toolCallContentChanged"]
        assert len(deltas) == 3, "one per word"
        assert [a["invocationMessage"] for a in deltas] == [
            "Echoing word 1",
            "Echoing word 2",
            "Echoing word 3",
        ]
        # REPLACES rather than appends, so each carries everything so far.
        assert [a["content"][0]["text"] for a in content] == ["one", "one two", "one two three"]

    async def test_the_meta_key_reaches_the_wire(self, host: Host) -> None:
        """ "a `ptyTerminal` key with `{input, output}` indicates the tool
        operated on a terminal" -- which is what makes a client render the
        terminal widget instead of a plain row."""
        client = await _client(host)
        chat = await _session(host, client, "echo:/p-2")
        sink = ActionTurnSink(host.sequencer, chat, "t-meta")

        await sink.tool_call_started(
            "c1",
            "run",
            {"command": "ls"},
            display_name="ls",
            meta={"ptyTerminal": {"input": "ls\r", "output": "a\r\nb\r\n"}},
        )
        await client.collect(seconds=0.3)

        start = next(a for a in _actions(client, chat) if a["type"] == "chat/toolCallStart")
        assert start["_meta"]["ptyTerminal"]["input"] == "ls\r"


class TestToolInputEncoding:
    """`ToolInput = string | ContentRef`, and the client runs `JSON.parse` on
    it. A bare object renders as an opaque blob on whichever path missed the
    encoding -- and a partial fix is worse than none, because the surface that
    works hides the one that does not."""

    async def test_every_publication_path_encodes(self, tooled: Host) -> None:
        client = await _client(tooled)
        chat = await _session(tooled, client, "echo:/e-1")

        await _turn(client, chat, text="hello")
        await client.collect(seconds=0.5)

        carrying = [a for a in _actions(client, chat) if "toolInput" in a]
        assert {a["type"] for a in carrying} >= {"chat/toolCallStart", "chat/toolCallReady"}
        for action in carrying:
            assert isinstance(action["toolInput"], str), action["type"]
