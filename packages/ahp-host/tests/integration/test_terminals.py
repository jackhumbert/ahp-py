"""Terminals end to end, against a backend that runs nothing.

This library ships **no** working terminal backend, deliberately: a POSIX pty in
the default wheel is arbitrary command execution one import away, in a library
whose `Policy` cannot authenticate a peer at all. So the test supplies its own
fake process — which is also the honest demonstration that the host's half works
without the dangerous half existing.

The properties under test are the ones a naive wiring gets wrong: stream order
(one read can carry output, an escape, and more output), input never echoing
into the buffer, and the claim model.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from typing import Any

import pytest
from agent_host_protocol.channels import ROOT_URI
from agent_host_protocol.transport import memory_pair

from agent_host_server.core import Host, LoopbackSingleUserPolicy
from agent_host_server.core.terminals import OutputSink, TerminalRequest
from agent_host_server.provider import EchoProvider

from .test_host_end_to_end import FakeClient

pytestmark = pytest.mark.anyio


class FakeProcess:
    """A terminal that runs nothing and remembers what it was told."""

    is_pty = True

    def __init__(self) -> None:
        self.written: list[bytes] = []
        self.size: tuple[int, int] | None = None
        self.killed = False

    async def write(self, data: bytes) -> None:
        self.written.append(data)

    async def resize(self, cols: int, rows: int) -> None:
        self.size = (cols, rows)

    async def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int | None:
        return 0


class FakeBackend:
    def __init__(self) -> None:
        self.process = FakeProcess()
        self.sink: OutputSink | None = None
        self.request: TerminalRequest | None = None

    async def create(self, request: TerminalRequest, output: OutputSink) -> FakeProcess:
        self.request = request
        self.sink = output
        return self.process

    async def emit(self, chunk: bytes) -> None:
        assert self.sink is not None
        result = self.sink(chunk)
        if asyncio.iscoroutine(result):
            await result
        await asyncio.sleep(0.15)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def wired() -> AsyncIterator[tuple[Host, FakeBackend]]:
    backend = FakeBackend()
    host = Host(EchoProvider(), LoopbackSingleUserPolicy(), terminals=backend)
    try:
        yield host, backend
    finally:
        await host.aclose()


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


async def _open(client: FakeClient, channel: str = "agenthost-terminal:/t1") -> str:
    result = await client.request(
        "createTerminal",
        {
            "channel": channel,
            "claim": {"kind": "client", "clientId": "c1"},
            "name": "Test Terminal",
            "cols": 80,
            "rows": 24,
        },
    )
    assert "error" not in result, result
    await client.request("subscribe", {"channel": channel})
    return channel


def _content(client: FakeClient, channel: str) -> str:
    text = ""
    for envelope in client.actions(channel):
        if envelope["action"]["type"] == "terminal/data":
            text += envelope["action"]["data"]
    return text


class TestLifecycle:
    async def test_a_terminal_registers_and_lands_in_the_catalogue(
        self, wired: tuple[Host, FakeBackend]
    ) -> None:
        host, _ = wired
        client = await _client(host)
        channel = await _open(client)

        state = (await client.request("subscribe", {"channel": channel}))["result"]["snapshot"][
            "state"
        ]
        assert state["isPty"] is True
        assert state["claim"] == {"kind": "client", "clientId": "c1"}

        root = (await client.request("subscribe", {"channel": ROOT_URI}))["result"]["snapshot"][
            "state"
        ]
        assert [t["resource"] for t in root["terminals"]] == [channel]

    async def test_the_uri_scheme_is_never_routed_on(self, wired: tuple[Host, FakeBackend]) -> None:
        """VS Code uses three `agenthost-terminal:` forms and the spec's
        examples use `ahp-terminal:`. The reducer is bound at registration."""
        host, _ = wired
        client = await _client(host)
        channel = await _open(client, "totally-made-up:/whatever")
        assert host.sequencer.reducer_of(channel) == "terminal"

    async def test_disposing_kills_the_process_and_clears_the_catalogue(
        self, wired: tuple[Host, FakeBackend]
    ) -> None:
        host, backend = wired
        client = await _client(host)
        channel = await _open(client)
        await client.request("disposeTerminal", {"channel": channel})

        assert backend.process.killed
        assert not host.sequencer.has_channel(channel)
        root = (await client.request("subscribe", {"channel": ROOT_URI}))["result"]["snapshot"][
            "state"
        ]
        assert root["terminals"] == []


class TestOutput:
    async def test_output_becomes_terminal_data(self, wired: tuple[Host, FakeBackend]) -> None:
        host, backend = wired
        client = await _client(host)
        channel = await _open(client)
        await backend.emit(b"hello world\r\n")
        await client.collect(seconds=0.3)
        assert "hello world" in _content(client, channel)

    async def test_shell_integration_sequences_are_stripped(
        self, wired: tuple[Host, FakeBackend]
    ) -> None:
        """`terminal-channel.md:112` makes stripping these a MUST."""
        host, backend = wired
        client = await _client(host)
        channel = await _open(client)
        await backend.emit(b"\x1b]633;A\x07before\x1b]633;B\x07after")
        await client.collect(seconds=0.3)
        text = _content(client, channel)
        assert "before" in text
        assert "after" in text
        assert "\x1b" not in text
        assert "633" not in text

    async def test_a_sequence_split_across_reads_survives(
        self, wired: tuple[Host, FakeBackend]
    ) -> None:
        """A chunk can end mid-escape. A stateless parser emits the fragment as
        text and then loses the rest of the sequence."""
        host, backend = wired
        client = await _client(host)
        channel = await _open(client)
        await backend.emit(b"start\x1b]6")
        await backend.emit(b"33;A\x07end")
        await client.collect(seconds=0.4)
        text = _content(client, channel)
        assert "start" in text
        assert "end" in text
        assert "633" not in text

    async def test_command_detection_maps_to_actions(self, wired: tuple[Host, FakeBackend]) -> None:
        host, backend = wired
        client = await _client(host)
        channel = await _open(client)
        await backend.emit(b"\x1b]633;E;ls -la\x07\x1b]633;C\x07total 0\r\n\x1b]633;D;0\x07")
        await client.collect(seconds=0.4)

        kinds = [e["action"]["type"] for e in client.actions(channel)]
        assert "terminal/commandDetectionAvailable" in kinds
        assert "terminal/commandExecuted" in kinds
        assert "terminal/commandFinished" in kinds

        executed = next(
            e["action"]
            for e in client.actions(channel)
            if e["action"]["type"] == "terminal/commandExecuted"
        )
        assert executed["commandLine"] == "ls -la"
        finished = next(
            e["action"]
            for e in client.actions(channel)
            if e["action"]["type"] == "terminal/commandFinished"
        )
        assert finished["exitCode"] == 0
        assert finished["commandId"] == executed["commandId"]

    async def test_output_around_an_escape_keeps_its_order(
        self, wired: tuple[Host, FakeBackend]
    ) -> None:
        """One read can carry `output ESC]633;D ESC]633;C output`. Treating a
        chunk as (all text, then all events) appends the second half to the
        wrong content part."""
        host, backend = wired
        client = await _client(host)
        channel = await _open(client)
        await backend.emit(b"first\x1b]633;D;0\x07\x1b]633;C\x07second")
        await client.collect(seconds=0.4)

        ordered = [
            e["action"]["type"] if e["action"]["type"] != "terminal/data" else e["action"]["data"]
            for e in client.actions(channel)
        ]
        assert ordered.index("first") < ordered.index("terminal/commandExecuted")
        assert ordered.index("terminal/commandExecuted") < ordered.index("second")


class TestClientActions:
    async def test_input_reaches_the_process_and_not_the_buffer(
        self, wired: tuple[Host, FakeBackend]
    ) -> None:
        """Echoing input into `content` would double it against the
        `terminal/data` the pty sends back -- which is why the reducer treats
        `terminal/input` as a no-op."""
        host, backend = wired
        client = await _client(host)
        channel = await _open(client)

        await client.notify(
            "dispatchAction",
            {
                "channel": channel,
                "clientSeq": 1,
                "action": {"type": "terminal/input", "data": "ls\r"},
            },
        )
        await client.collect(seconds=0.3)
        assert backend.process.written == [b"ls\r"]
        assert _content(client, channel) == ""

    async def test_a_resize_reaches_the_process(self, wired: tuple[Host, FakeBackend]) -> None:
        host, backend = wired
        client = await _client(host)
        channel = await _open(client)
        await client.notify(
            "dispatchAction",
            {
                "channel": channel,
                "clientSeq": 1,
                "action": {"type": "terminal/resized", "cols": 120, "rows": 40},
            },
        )
        await client.collect(seconds=0.3)
        assert backend.process.size == (120, 40)

    async def test_a_peer_that_does_not_hold_the_claim_cannot_type(
        self, wired: tuple[Host, FakeBackend]
    ) -> None:
        """`terminal/input` is client-dispatchable, so without a claim check any
        subscriber can type into somebody else's shell."""
        host, backend = wired
        owner = await _client(host)
        channel = await _open(owner)

        other_transport, other_server = memory_pair()
        task = asyncio.create_task(host.serve(other_server))
        assert task is not None
        intruder = FakeClient(other_transport)
        await intruder.request(
            "initialize",
            {
                "channel": ROOT_URI,
                "clientId": "intruder",
                "protocolVersions": ["0.7.0"],
                "initialSubscriptions": [ROOT_URI],
            },
        )
        await intruder.request("subscribe", {"channel": channel})
        await intruder.notify(
            "dispatchAction",
            {
                "channel": channel,
                "clientSeq": 1,
                "action": {"type": "terminal/input", "data": "rm -rf /\r"},
            },
        )
        await intruder.collect(seconds=0.3)

        assert backend.process.written == []
        echoes = [e for e in intruder.actions(channel) if e["action"]["type"] == "terminal/input"]
        assert echoes
        assert "rejectionReason" in echoes[-1]


class TestRefusal:
    async def test_the_policy_gate_runs_before_the_backend(self) -> None:
        """A host that installs a real backend without narrowing this has
        granted a shell to anyone who can reach the socket."""

        class NoTerminals(LoopbackSingleUserPolicy):
            def may_create_terminal(self, info: Any, params: Any) -> bool:
                return False

        backend = FakeBackend()
        host = Host(EchoProvider(), NoTerminals(), terminals=backend)
        try:
            client = await _client(host)
            response = await client.request(
                "createTerminal",
                {"channel": "agenthost-terminal:/x", "claim": {"kind": "session", "session": "s"}},
            )
            assert response["error"]["code"] == -32009
            assert backend.request is None, "the backend was reached despite the policy"
        finally:
            await host.aclose()

    async def test_a_malformed_claim_is_refused(self, wired: tuple[Host, FakeBackend]) -> None:
        """A `clientId` of `123` must not become the claim of a client named
        `"123"`."""
        host, _ = wired
        client = await _client(host)
        response = await client.request(
            "createTerminal",
            {"channel": "agenthost-terminal:/bad", "claim": {"kind": "client", "clientId": 123}},
        )
        assert response["error"]["code"] == -32602


class TestTheExitIsAnnounced:
    """Nothing published `terminal/exited`, so a shell that ended left the
    channel looking live forever and the client's tab never closed. Confirmed
    on the wire: after `exit 7` the last frame was the input echo, then
    silence.

    Needs a real pty -- a fake backend cannot exit.
    """

    @pytest.fixture
    def pty_host(self) -> Host:
        from agent_host_server.core.pty_backend import PtyTerminalBackend

        return Host(EchoProvider(), LoopbackSingleUserPolicy(), terminals=PtyTerminalBackend())

    async def test_exiting_publishes_terminal_exited_with_the_code(self, pty_host: Host) -> None:
        client = await _client(pty_host)
        await client.request("createSession", {"channel": "echo:/t-exit"})
        await client.collect(seconds=0.3)
        channel = "ahp-terminal:/exit-1"
        result = await client.request(
            "createTerminal",
            {
                "channel": channel,
                # A CLIENT claim, not a session one: `terminal/input` is
                # claim-gated, so a terminal claimed by the session refuses
                # keystrokes from every client -- which is correct, and is how
                # a user-opened terminal differs from a tool-call one.
                "claim": {"kind": "client", "clientId": "c1"},
                "cwd": os.getcwd(),
                "cols": 80,
                "rows": 24,
            },
        )
        assert "error" not in result, result.get("error")
        await client.request("subscribe", {"channel": channel})

        await client.notify(
            "dispatchAction",
            {
                "channel": channel,
                "clientSeq": 1,
                "action": {"type": "terminal/input", "data": "exit 7\n"},
            },
        )
        await client.collect(seconds=2.5)

        exits = [
            a["action"] for a in client.actions(channel) if a["action"]["type"] == "terminal/exited"
        ]
        assert exits, "the shell exited and nothing said so"
        assert exits[-1]["exitCode"] == 7

        # And the exit is LAST: the parser is flushed first, so the tail of the
        # output arrives before the frame that says there is no more coming.
        types = [a["action"]["type"] for a in client.actions(channel)]
        assert types[-1] == "terminal/exited", types[-4:]

    async def test_the_catalogue_carries_the_required_fields(self, pty_host: Host) -> None:
        """`TerminalInfo` requires `resource`, `title` and `claim`. We sent
        `{resource, isPty}` -- and `isPty` is not even a TerminalInfo field."""
        client = await _client(pty_host)
        await client.request("createSession", {"channel": "echo:/t-cat"})
        await client.collect(seconds=0.3)
        await client.request(
            "createTerminal",
            {
                "channel": "ahp-terminal:/cat-1",
                "claim": {"kind": "client", "clientId": "c1"},
                "cwd": os.getcwd(),
                "name": "named",
            },
        )
        await client.collect(seconds=0.4)

        root = (await client.request("subscribe", {"channel": ROOT_URI}))["result"]["snapshot"][
            "state"
        ]
        entry = root["terminals"][0]
        assert entry["resource"] == "ahp-terminal:/cat-1"
        assert entry["title"] == "named"
        assert entry["claim"] == {"kind": "client", "clientId": "c1"}
