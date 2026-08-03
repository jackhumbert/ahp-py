"""The last of the parity diff: one latent bug, one advertised-and-absent
feature, and two findings that turned out to be wrong.

The two refutations are here on purpose. A parity list is a set of claims, and
a claim that is checked and found false is worth as much as one that is fixed --
"fixed" a correct behaviour is how a conformant host stops being one.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from agent_host_protocol.channels import ROOT_URI
from agent_host_protocol.transport import memory_pair

from agent_host_server.core import Host, LoopbackSingleUserPolicy
from agent_host_server.core.pty_backend import PtyTerminalBackend
from agent_host_server.provider import EchoProvider
from agent_host_server.provider.base import AgentSessionContext, TurnSink, UserMessage

from .test_host_end_to_end import FakeClient

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class BareSession:
    """The simplest provider anyone writes: announce a call, then finish it.

    No confirmation, no client tool, no elicitation. This is the shape a first
    adapter has, and it is the one that was broken.
    """

    def __init__(self, context: AgentSessionContext) -> None:
        self.context = context

    async def send_user_message(self, message: UserMessage, sink: TurnSink) -> None:
        await sink.tool_call_started("c1", "look", {"q": message.text}, display_name="Look")
        await sink.tool_call_completed(
            "c1",
            {"content": [{"type": "text", "text": "found it"}]},
            past_tense_message="Looked",
        )
        await sink.text_delta("done")

    async def cancel(self, reason: str | None = None) -> None: ...

    async def aclose(self) -> None: ...


class BareProvider(EchoProvider):
    async def create_session(self, context: AgentSessionContext) -> Any:
        return BareSession(context)


@pytest.fixture
async def bare() -> AsyncIterator[Host]:
    made = Host(BareProvider(), LoopbackSingleUserPolicy())
    try:
        yield made
    finally:
        await made.aclose()


@pytest.fixture
async def shell() -> AsyncIterator[Host]:
    made = Host(EchoProvider(), LoopbackSingleUserPolicy(), terminals=PtyTerminalBackend())
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


def _state(host: Host, uri: str) -> dict[str, Any]:
    state = host.sequencer.state_of(uri)
    assert isinstance(state, dict)
    return state


async def _completed(host: Host, client: FakeClient, chat: str, *, timeout: float = 15.0) -> None:
    """Wait for *chat* to have a completed turn carrying a tool call.

    This replaced a fixed `collect(seconds=2.0)` that failed in a full-suite
    run and passed in isolation. I first read that as a timing budget -- two
    seconds being comfortable on an idle laptop and not under load -- and that
    diagnosis was WRONG. The cause was data loss in the pty backend: the parent
    closed its slave fd immediately, so a macOS pty master reported EOF and
    discarded whatever the child had just written. Measured at 48/48 concurrent
    commands losing their output entirely; the fix is in `pty_backend.py` and
    the load test is `TestTheTailIsNotLost`.

    The condition wait is still the right shape -- it is faster and it does not
    encode a guess about how slow a runner might be -- but it was not what made
    this test honest.
    """
    # The CALL's terminal status, not merely its existence: a `!command` lands
    # in `turns` as soon as the turn settles, and the completion that carries
    # the output is a separate frame. Waiting only for the call to appear
    # reintroduced the race in a slower form.
    await client.collect_until(
        lambda: any(c.get("status") in {"completed", "cancelled"} for c in _tool_calls(host, chat)),
        timeout=timeout,
    )


def _tool_calls(host: Host, chat: str) -> list[dict[str, Any]]:
    return [
        part["toolCall"]
        for turn in _state(host, chat)["turns"]
        for part in turn.get("responseParts", [])
        if part.get("kind") == "toolCall"
    ]


class TestStartToCompleteIsNotSwallowed:
    """`chat/toolCallStart` leaves a call in `streaming`, and the validation
    table only accepts `chat/toolCallComplete` from `running`,
    `pendingConfirmation` or `authRequired`. The reducer is right; the missing
    piece was the transition, which the confirm and client-tool paths happened
    to publish for their own reasons and nothing else did."""

    async def test_the_simplest_provider_gets_a_completed_call(self, bare: Host) -> None:
        client = await _client(bare)
        chat = await _session(bare, client, "echo:/s-1")

        await _turn(client, chat, text="hello")
        await client.collect(seconds=0.6)

        calls = _tool_calls(bare, chat)
        assert len(calls) == 1
        assert calls[0]["status"] == "completed", "the completion was dropped"
        assert calls[0]["content"] == [{"type": "text", "text": "found it"}]

    async def test_the_transition_says_no_confirmation_was_needed(self, bare: Host) -> None:
        """ "The server typically sets `confirmed` to `'not-needed'` so the tool
        transitions directly to `running`.\""""
        client = await _client(bare)
        chat = await _session(bare, client, "echo:/s-2")

        await _turn(client, chat, text="hello")
        await client.collect(seconds=0.6)

        assert _tool_calls(bare, chat)[0]["confirmed"] == "not-needed"

    async def test_a_confirmed_call_does_not_get_a_second_ready(self) -> None:
        """The confirm path publishes its own, and two would move the call
        straight past the confirmation the user was being asked for."""
        host = Host(EchoProvider(confirm_tools=True), LoopbackSingleUserPolicy())
        try:
            client = await _client(host)
            chat = await _session(host, client, "echo:/s-3")
            await _turn(client, chat, text="hello")
            await client.collect(seconds=0.5)

            readies = [
                note["params"]["action"]
                for note in client.notifications
                if note.get("method") == "action"
                and note["params"].get("channel") == chat
                and note["params"]["action"]["type"] == "chat/toolCallReady"
            ]
            assert len(readies) == 1
            assert readies[0].get("confirmed") != "not-needed"
        finally:
            await host.aclose()


class TestTheTerminalCommandPrefix:
    """Advertised and unimplemented is worse than absent: the input box
    promised a shortcut that silently went to the agent instead."""

    async def test_it_is_only_advertised_behind_a_real_backend(self) -> None:
        """ "Absence means the host does not support command prefixes." With
        the refusing default, `!ls` would promise a command this host then
        declines -- a working input turned into a dead end."""
        plain = Host(EchoProvider(), LoopbackSingleUserPolicy())
        try:
            client_transport, server_transport = memory_pair()
            task = asyncio.create_task(plain.serve(server_transport))
            bare_client = FakeClient(client_transport)
            response = await bare_client.request(
                "initialize",
                {
                    "channel": ROOT_URI,
                    "clientId": "c1",
                    "protocolVersions": ["0.7.0"],
                    "initialSubscriptions": [],
                },
            )
            assert "terminalCommandPrefix" not in response["result"]
            task.cancel()
        finally:
            await plain.aclose()

    async def test_a_real_backend_advertises_it(self, shell: Host) -> None:
        client_transport, server_transport = memory_pair()
        task = asyncio.create_task(shell.serve(server_transport))
        client = FakeClient(client_transport)
        response = await client.request(
            "initialize",
            {
                "channel": ROOT_URI,
                "clientId": "c1",
                "protocolVersions": ["0.7.0"],
                "initialSubscriptions": [],
            },
        )
        assert response["result"]["terminalCommandPrefix"] == "!"
        task.cancel()

    async def test_the_command_actually_runs(self, shell: Host) -> None:
        client = await _client(shell)
        chat = await _session(shell, client, "echo:/t-1")

        await _turn(client, chat, text="!echo hello-from-the-shell")
        await _completed(shell, client, chat)

        calls = _tool_calls(shell, chat)
        assert len(calls) == 1, "no tool call was published for the command"
        assert calls[0]["status"] == "completed"
        assert calls[0]["success"] is True
        assert "hello-from-the-shell" in json.dumps(calls[0]["content"])

    async def test_a_failing_command_is_reported_as_a_failure(self, shell: Host) -> None:
        client = await _client(shell)
        chat = await _session(shell, client, "echo:/t-2")

        await _turn(client, chat, text="!exit 3")
        await _completed(shell, client, chat)

        call = _tool_calls(shell, chat)[0]
        assert call["success"] is False
        assert "exit 3" in call["pastTenseMessage"]

    async def test_it_never_reaches_the_agent(self, shell: Host) -> None:
        """The user asked the HOST to run a command. Handing it to an agent as
        a message beginning with `!` is what the prefix exists to stop."""
        client = await _client(shell)
        chat = await _session(shell, client, "echo:/t-3")

        await _turn(client, chat, text="!true")
        await client.collect_until(
            lambda: (
                (shell.sequencer.state_of(chat) or {}).get("activeTurn") is None
                and bool((shell.sequencer.state_of(chat) or {}).get("turns"))
            ),
            timeout=15.0,
        )

        turn = _state(shell, chat)["turns"][0]
        kinds = [part.get("kind") for part in turn["responseParts"]]
        assert "markdown" not in kinds, "the echo agent answered a terminal command"
        assert _state(shell, chat)["activeTurn"] is None, "the turn never settled"

    async def test_a_bare_prefix_is_just_a_message(self, shell: Host) -> None:
        """`!` alone names no command, so it goes to the agent like any text."""
        client = await _client(shell)
        chat = await _session(shell, client, "echo:/t-4")

        await _turn(client, chat, text="!   ")
        await client.collect(seconds=1.0)

        kinds = [p.get("kind") for p in _state(shell, chat)["turns"][0]["responseParts"]]
        assert "markdown" in kinds

    async def test_a_host_without_a_backend_leaves_the_message_alone(self, bare: Host) -> None:
        """It does not advertise the prefix, so `!ls` is an ordinary message and
        must reach the agent rather than being swallowed."""
        client = await _client(bare)
        chat = await _session(bare, client, "echo:/t-5")

        await _turn(client, chat, text="!ls")
        await client.collect(seconds=0.6)

        calls = _tool_calls(bare, chat)
        assert calls, "the message never reached the agent"
        assert calls[0]["toolName"] == "look"


class TestTwoFindingsThatWereWrong:
    """Both were on the parity list. Both are checked here so nobody 'fixes'
    them later."""

    async def test_active_sessions_counts_every_live_session(self, bare: Host) -> None:
        """The list said this should count sessions with an ACTIVE TURN. The
        schema says `activeSessions` is the "number of active (non-disposed)
        sessions on the server" -- which is what we already publish."""
        client = await _client(bare)
        await _session(bare, client, "echo:/w-1")
        await _session(bare, client, "echo:/w-2")
        await client.collect(seconds=0.4)

        root = _state(bare, ROOT_URI)
        assert root["activeSessions"] == 2, "idle sessions stopped being counted"

    async def test_an_action_on_an_unknown_channel_is_not_echoed(self, bare: Host) -> None:
        """The list said the missing echo pins the client's optimistic overlay.
        It does -- and the spec is explicit that it MUST: "actions on a
        non-existent channel MUST be silently ignored with no echo". Echoing
        would break the rule to fix the symptom."""
        client = await _client(bare)
        await _session(bare, client, "echo:/w-3")
        client.notifications.clear()

        await client.notify(
            "dispatchAction",
            {
                "channel": "ahp-chat:/does-not-exist",
                "clientSeq": 9,
                "action": {"type": "chat/draftChanged", "draft": {"text": "hi"}},
            },
        )
        await client.collect(seconds=0.4)

        assert not [
            note
            for note in client.notifications
            if note.get("method") == "action"
            and note["params"].get("channel") == "ahp-chat:/does-not-exist"
        ]
