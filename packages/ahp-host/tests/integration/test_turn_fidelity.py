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
from ahp_protocol.channels import ROOT_URI
from ahp_protocol.transport import memory_pair

from ahp_host.core import Host, LoopbackSingleUserPolicy
from ahp_host.core.turn import ActionTurnSink
from ahp_host.provider import EchoProvider

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
async def slow() -> AsyncIterator[Host]:
    """Replies slowly enough that a client can steer mid-turn."""
    made = Host(EchoProvider(delay=0.3), LoopbackSingleUserPolicy())
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
    # The default chat is what the next two lines read; waiting for it by name
    # turns "the session had not published yet" from an IndexError into a wait.
    await client.collect_until(
        lambda: bool((host.sequencer.state_of(uri) or {}).get("chats")), timeout=10.0
    )
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


async def _completed(client: FakeClient, chat: str, *, timeout: float = 10.0) -> None:
    """Wait for the turn on *chat* to publish `chat/turnComplete`.

    The terminal FRAME rather than any one part or count an assertion happens to
    name: the tests below assert on exact lists ("one usage action", "tool then
    markdown"), and stopping at the first part that matched would let a stray
    later one through. `chat/turnComplete` is the last frame a turn publishes,
    so waiting for it bounds the same window a fixed wait did -- everything the
    turn said, and nothing after.
    """
    await client.collect_until(
        lambda: any(a["type"] == "chat/turnComplete" for a in _actions(client, chat)),
        timeout=timeout,
    )


async def _parked(client: FakeClient, chat: str, *, timeout: float = 10.0) -> None:
    """Wait for a tool call to reach `chat/toolCallReady`.

    Where a confirming host stops until someone answers, and where every caller
    below reads the `toolCallId` off it.
    """
    await client.collect_until(
        lambda: any(a["type"] == "chat/toolCallReady" for a in _actions(client, chat)),
        timeout=timeout,
    )


class TestResponsePartOrdering:
    """The client renders parts in creation order and appends a delta to
    whichever part its id names, so one markdown part per turn puts prose
    written AFTER a tool call above it."""

    async def test_prose_after_a_tool_call_is_its_own_part(self, tooled: Host) -> None:
        client = await _client(tooled)
        chat = await _session(tooled, client, "echo:/f-1")

        await _turn(client, chat, text="hello")
        await _parked(client, chat)
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
                    "confirmed": "user-action",
                },
            },
        )
        await _completed(client, chat)

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
        await _completed(client, chat)

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
        await _completed(client, chat)

        state = _state(host, chat)
        assert state.get("queuedMessages") in (None, []), "the queue was never drained"
        assert any("hello" in str(turn) for turn in state["turns"]), "no turn ran it"

    async def test_a_message_queued_mid_turn_runs_after_it(self, tooled: Host) -> None:
        """The case that produced the report: the agent is busy, so the message
        waits -- and then nothing ever came back for it."""
        client = await _client(tooled)
        chat = await _session(tooled, client, "echo:/q-2")

        await _turn(client, chat, text="first")
        await _parked(client, chat)
        await self._queue(client, chat, "q1", "second", 2)
        # Fixed on purpose: the claim is that the queue was NOT drained while
        # the turn ran, and a wait that returns the moment the message lands
        # gives the host no chance to wrongly drain it a beat later.
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
                    "confirmed": "user-action",
                },
            },
        )
        await client.collect_until(
            lambda: "second" in str((tooled.sequencer.state_of(chat) or {}).get("activeTurn")),
            timeout=10.0,
        )

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
        await _completed(client, chat)

        removals = [a for a in _actions(client, chat) if a["type"] == "chat/pendingMessageRemoved"]
        assert [a["id"] for a in removals] == ["q1"]

    async def test_two_queued_messages_run_in_order(self, host: Host) -> None:
        client = await _client(host)
        chat = await _session(host, client, "echo:/q-4")

        await self._queue(client, chat, "q1", "alpha", 1)
        await self._queue(client, chat, "q2", "bravo", 2)
        # Both turns, and the chat back at rest: an exact count of two only
        # means anything once nothing is still running.
        await client.collect_until(
            lambda: (
                (host.sequencer.state_of(chat) or {}).get("activeTurn") is None
                and len((host.sequencer.state_of(chat) or {}).get("turns", [])) >= 2
            ),
            timeout=10.0,
        )

        state = _state(host, chat)
        assert state.get("queuedMessages") in (None, [])
        ran = [t for t in state["turns"] if "alpha" in str(t) or "bravo" in str(t)]
        assert len(ran) == 2, f"both should have run: {state['turns']}"
        assert "alpha" in str(ran[0])
        assert "bravo" in str(ran[1])

    async def _steer(self, client: FakeClient, chat: str, id_: str, text: str, seq: int) -> None:
        await client.notify(
            "dispatchAction",
            {
                "channel": chat,
                "clientSeq": seq,
                "action": {
                    "type": "chat/pendingMessageSet",
                    "kind": "steering",
                    "id": id_,
                    "message": {"text": text, "origin": {"kind": "user"}},
                },
            },
        )

    async def test_a_steering_message_joins_the_running_turn(self, slow: Host) -> None:
        """Steering is injected into the RUNNING turn: answered there, noted in
        the transcript, and removed from the chat's pending slot -- not run as
        a turn of its own."""
        client = await _client(slow)
        chat = await _session(slow, client, "echo:/q-5")

        await _turn(client, chat, text="first")
        await client.collect(seconds=0.05)
        await self._steer(client, chat, "s1", "and bananas", 2)
        await _completed(client, chat)

        state = _state(slow, chat)
        assert state.get("steeringMessage") is None, "the steering message was never consumed"
        assert len(state["turns"]) == 1, "steering ran as a turn of its own"
        parts = state["turns"][0]["responseParts"]
        notes = [p for p in parts if p.get("kind") == "systemNotification"]
        assert notes, parts
        assert notes[0]["content"] == "and bananas"
        assert notes[0]["_meta"] == {"steering": True}
        assert "You also said: and bananas" in str(parts)
        removals = [a for a in _actions(client, chat) if a["type"] == "chat/pendingMessageRemoved"]
        assert [(a["kind"], a["id"]) for a in removals] == [("steering", "s1")]

    async def test_a_steering_message_on_an_idle_chat_runs_next(self, host: Host) -> None:
        """No turn to join (it ended first, or the agent cannot be steered):
        it is still the user's message, so it runs rather than sitting in the
        chat forever."""
        client = await _client(host)
        chat = await _session(host, client, "echo:/q-6")

        await self._steer(client, chat, "s1", "actually, this", 1)
        await _completed(client, chat)

        state = _state(host, chat)
        assert state.get("steeringMessage") is None
        assert any("actually, this" in str(turn) for turn in state["turns"])

    async def test_two_steering_messages_both_join_the_running_turn(self, slow: Host) -> None:
        """1.1.0 keeps every steering message, not only the latest: each is
        offered to the turn in the order it was typed and removed by its own
        id -- "a later submission MUST NOT implicitly consume an earlier one"."""
        client = await _client(slow)
        chat = await _session(slow, client, "echo:/q-7")

        await _turn(client, chat, text="first")
        await client.collect(seconds=0.05)
        await self._steer(client, chat, "s1", "and bananas", 2)
        await self._steer(client, chat, "s2", "and cherries", 3)
        await _completed(client, chat)

        state = _state(slow, chat)
        assert state.get("queuedMessages") in (None, []), state.get("queuedMessages")
        assert state.get("steeringMessage") is None
        assert len(state["turns"]) == 1, "steering ran as a turn of its own"
        removals = [a for a in _actions(client, chat) if a["type"] == "chat/pendingMessageRemoved"]
        assert [(a["kind"], a["id"]) for a in removals] == [("steering", "s1"), ("steering", "s2")]

    async def test_mixed_pending_input_runs_each_entry_once(self, host: Host) -> None:
        """Both kinds share one list now. Taking `queuedMessages[0]` and
        removing it as `queued` misses a steering entry -- the reducer matches
        id AND kind -- so the entry stayed, and ran again after every turn."""
        client = await _client(host)
        chat = await _session(host, client, "echo:/q-10")
        # Two steering messages no turn took, as a turn that ended first leaves
        # them. Published straight to state: dispatched to an idle chat, the
        # first would run at once and the list would never hold both.
        for id_, text in (("s1", "one"), ("s2", "two")):
            await host.sequencer.publish(
                chat,
                {
                    "type": "chat/pendingMessageSet",
                    "kind": "steering",
                    "id": id_,
                    "message": {"text": text, "origin": {"kind": "user"}},
                },
            )
        await self._queue(client, chat, "q1", "three", 1)
        await client.collect_until(
            lambda: (
                (host.sequencer.state_of(chat) or {}).get("activeTurn") is None
                and len((host.sequencer.state_of(chat) or {}).get("turns", [])) >= 3
            ),
            timeout=10.0,
        )
        # Long enough for a wrongly repeated entry to start another turn.
        await client.collect(seconds=0.3)

        state = _state(host, chat)
        assert state.get("queuedMessages") in (None, []), state.get("queuedMessages")
        assert state.get("steeringMessage") is None
        assert [t["message"]["text"] for t in state["turns"]] == ["one", "two", "three"]

    async def test_an_object_id_runs_once_without_breaking_the_drain(self, host: Host) -> None:
        """A pending id is peer-chosen and the schema's `string` is not
        enforced, so an object can arrive. Asking a Python set whether it holds
        one raises, which killed the drain and stranded the message."""
        client = await _client(host)
        chat = await _session(host, client, "echo:/q-12")

        await client.notify(
            "dispatchAction",
            {
                "channel": chat,
                "clientSeq": 1,
                "action": {
                    "type": "chat/pendingMessageSet",
                    "kind": "queued",
                    "id": {"n": 1},
                    "message": {"text": "hello", "origin": {"kind": "user"}},
                },
            },
        )
        await _completed(client, chat)
        await client.collect(seconds=0.3)

        state = _state(host, chat)
        assert state.get("queuedMessages") in (None, []), state.get("queuedMessages")
        assert [t["message"]["text"] for t in state["turns"]] == ["hello"]

    async def test_a_consumed_id_cannot_be_set_again(self, host: Host) -> None:
        """ "Hosts MUST reject edits or client removals that they can no longer
        honor after handoff." Re-setting a consumed id would run it again."""
        client = await _client(host)
        chat = await _session(host, client, "echo:/q-8")

        await self._queue(client, chat, "q1", "hello", 1)
        await _completed(client, chat)
        await self._queue(client, chat, "q1", "hello again", 2)
        await client.collect(seconds=0.3)

        sets = [e for e in client.actions(chat) if e["action"]["type"] == "chat/pendingMessageSet"]
        assert "rejectionReason" not in sets[0]
        assert "already delivered" in sets[-1].get("rejectionReason", "")
        assert len(_state(host, chat)["turns"]) == 1

    async def test_removing_what_is_not_pending_is_refused(self, host: Host) -> None:
        """ "Server SHOULD reject the action; the reducer is a no-op" -- and a
        steering removal must not take a queued entry of the same id."""
        client = await _client(host)
        chat = await _session(host, client, "echo:/q-9")

        await client.notify(
            "dispatchAction",
            {
                "channel": chat,
                "clientSeq": 1,
                "action": {"type": "chat/pendingMessageRemoved", "kind": "queued", "id": "nope"},
            },
        )
        await client.collect(seconds=0.3)

        echoes = [
            e for e in client.actions(chat) if e["action"]["type"] == "chat/pendingMessageRemoved"
        ]
        assert echoes, "the removal was dropped rather than echoed"
        assert "rejectionReason" in echoes[0]

    async def test_a_pending_message_of_an_unknown_kind_is_refused(self, host: Host) -> None:
        """Nothing consumes a kind the host does not know, so it would sit in
        the list forever."""
        client = await _client(host)
        chat = await _session(host, client, "echo:/q-11")

        await client.notify(
            "dispatchAction",
            {
                "channel": chat,
                "clientSeq": 1,
                "action": {
                    "type": "chat/pendingMessageSet",
                    "kind": "later",
                    "id": "x1",
                    "message": {"text": "hi", "origin": {"kind": "user"}},
                },
            },
        )
        await client.collect(seconds=0.3)

        echoes = [
            e for e in client.actions(chat) if e["action"]["type"] == "chat/pendingMessageSet"
        ]
        assert "rejectionReason" in echoes[0]
        assert _state(host, chat).get("queuedMessages") in (None, [])


class TestUsage:
    """The client's rule is "no usage, no gauge" -- it renders nothing at all
    rather than a zero."""

    async def test_a_turn_reports_usage(self, host: Host) -> None:
        client = await _client(host)
        chat = await _session(host, client, "echo:/u-1")

        await _turn(client, chat, text="hello there")
        await _completed(client, chat)

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
        await _completed(client, chat)

        types = [a["type"] for a in _actions(client, chat)]
        assert types.index("chat/usage") < types.index("chat/turnComplete")


class TestProgressiveToolOutput:
    async def test_a_running_tool_streams(self, tooled: Host) -> None:
        client = await _client(tooled)
        chat = await _session(tooled, client, "echo:/p-1")

        await _turn(client, chat, text="one two three")
        await _parked(client, chat)
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
                    "confirmed": "user-action",
                },
            },
        )
        await _completed(client, chat)

        actions = _actions(client, chat)
        content = [
            a["content"][0]["text"] for a in actions if a["type"] == "chat/toolCallContentChanged"
        ]
        # REPLACES rather than appends, so each carries everything so far. The
        # consecutive repeats are the progress updates below putting the content
        # back: a second `chat/toolCallReady` rebuilds the tool call from its
        # base fields, which do not include `content`, so live output would
        # otherwise blink out every time the progress line moved.
        assert [text for i, text in enumerate(content) if i == 0 or text != content[i - 1]] == [
            "one",
            "one two",
            "one two three",
        ]

        # Progress on a call that is already RUNNING, which is the only mode
        # this provider has. `chat/toolCallDelta` reaches a `streaming` call and
        # nothing else -- the reducer's updater returns a running call untouched
        # -- so this used to publish three deltas that every mirror discarded,
        # and the demo's own progress line never moved. The frame that lands is
        # a second `chat/toolCallReady` carrying the confirmation forward.
        progress = [
            a
            for a in actions
            if a["type"] == "chat/toolCallReady" and a.get("confirmed") == "user-action"
        ]
        assert [a["invocationMessage"] for a in progress] == [
            "Echoing word 1",
            "Echoing word 2",
            "Echoing word 3",
        ]
        assert not [a for a in actions if a["type"] == "chat/toolCallDelta"], (
            "a delta on a running call is a frame the reducer drops"
        )

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
        await client.collect_until(
            lambda: any(a["type"] == "chat/toolCallStart" for a in _actions(client, chat)),
            timeout=10.0,
        )

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
        # Fixed on purpose, and the set-equality below is why: the claim is that
        # `chat/toolCallReady` is the ONLY frame carrying a `toolInput`. This
        # host parks the turn on a confirmation nobody answers, so there is no
        # terminal frame to wait for, and stopping at the first `toolInput`
        # would stop checking exactly where the check starts being interesting.
        await client.collect(seconds=0.5)

        carrying = [a for a in _actions(client, chat) if "toolInput" in a]
        assert {a["type"] for a in carrying} == {"chat/toolCallReady"}, (
            "`chat/toolCallStart` declares no `toolInput`; the reducer drops one"
        )
        for action in carrying:
            assert isinstance(action["toolInput"], str), action["type"]
