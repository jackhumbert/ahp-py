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
import json
import os
import subprocess
from collections.abc import AsyncIterator
from typing import Any

import pytest
from ahp_protocol.channels import ROOT_URI
from ahp_protocol.transport import memory_pair
from ahp_protocol.types import AHP_ERROR_CODES

from ahp_host.core import Host, LoopbackSingleUserPolicy
from ahp_host.core.terminals import (
    STRICT_CLAIM_GATED_ACTIONS,
    OutputSink,
    TerminalRequest,
)
from ahp_host.provider import EchoProvider

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
    # `null`, not `{}`: the CommandMap declares `result: null` for every
    # create/dispose lifecycle command.
    assert result["result"] is None
    await client.request("subscribe", {"channel": channel})
    return channel


def _content(client: FakeClient, channel: str) -> str:
    text = ""
    for envelope in client.actions(channel):
        if envelope["action"]["type"] == "terminal/data":
            text += envelope["action"]["data"]
    return text


def _catalogue(host: Host) -> list[Any]:
    state = host.sequencer.state_of(ROOT_URI) or {}
    terminals = state.get("terminals")
    return list(terminals) if isinstance(terminals, list) else []


async def _session_ready(
    host: Host, client: FakeClient, uri: str, *, timeout: float = 10.0
) -> None:
    """Wait for *uri*'s bring-up to finish.

    `createSession` answers BEFORE `_bring_up` runs, and the default chat is
    published from inside it (`session/chatAdded`) -- so a caller that reads
    `chats[0]` after a fixed sleep is racing a background task rather than
    merely being slow. `lifecycle` is the end of that task: `session/ready` is
    its last publish.
    """

    def ready() -> bool:
        state = host.sequencer.state_of(uri) or {}
        return state.get("lifecycle") == "ready" and bool(state.get("chats"))

    await client.collect_until(ready, timeout=timeout)


async def _data(client: FakeClient, channel: str, text: str, *, timeout: float = 10.0) -> None:
    """Wait for *text* to arrive in *channel*'s `terminal/data`.

    One read is mapped to actions in stream order inside a single coroutine, so
    waiting for the LAST expected fragment means every earlier one -- and any
    escape wrongly emitted as text among them -- has already been published.
    """
    await client.collect_until(lambda: text in _content(client, channel), timeout=timeout)


async def _acted(
    client: FakeClient, channel: str, action_type: str, *, timeout: float = 10.0
) -> None:
    """Wait for *action_type* to be published on *channel*."""
    await client.collect_until(
        lambda: any(e["action"]["type"] == action_type for e in client.actions(channel)),
        timeout=timeout,
    )


async def _rejected(
    client: FakeClient, channel: str, action_type: str, *, timeout: float = 10.0
) -> None:
    """Wait for the echo of *action_type* to come back carrying a rejection.

    The rejection IS the terminal outcome of that dispatch: the gate and the
    hand-off to the backend live in the same handler. So "the backend was never
    touched", asserted after this, is checked against a handler that has
    demonstrably run rather than against a clock that has merely ticked.
    """
    await client.collect_until(
        lambda: any(
            e["action"]["type"] == action_type and "rejectionReason" in e
            for e in client.actions(channel)
        ),
        timeout=timeout,
    )


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
        disposed = await client.request("disposeTerminal", {"channel": channel})
        # `result: null` per the CommandMap, like `_open`'s create.
        assert disposed["result"] is None

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
        await _data(client, channel, "hello world")
        assert "hello world" in _content(client, channel)

    async def test_shell_integration_sequences_are_stripped(
        self, wired: tuple[Host, FakeBackend]
    ) -> None:
        """`terminal-channel.md:112` makes stripping these a MUST."""
        host, backend = wired
        client = await _client(host)
        channel = await _open(client)
        await backend.emit(b"\x1b]633;A\x07before\x1b]633;B\x07after")
        await _data(client, channel, "after")
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
        await _data(client, channel, "end")
        text = _content(client, channel)
        assert "start" in text
        assert "end" in text
        assert "633" not in text

    async def test_command_detection_maps_to_actions(self, wired: tuple[Host, FakeBackend]) -> None:
        host, backend = wired
        client = await _client(host)
        channel = await _open(client)
        await backend.emit(b"\x1b]633;E;ls -la\x07\x1b]633;C\x07total 0\r\n\x1b]633;D;0\x07")
        # The last of the three, so the two before it have already landed.
        await _acted(client, channel, "terminal/commandFinished")

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
        # `second` is the tail of the chunk: once it is here the whole read has
        # been mapped, which is what the ordering below is asserted over.
        await _data(client, channel, "second")

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
        await client.collect_until(lambda: backend.process.written == [b"ls\r"], timeout=10.0)
        # And then a real elapsed wait, kept deliberately: the second assertion
        # is NEGATIVE -- no `terminal/data` was manufactured from the keystroke
        # -- and a condition wait would return before any such frame could
        # arrive and prove nothing. Now it is a quiet window that begins after
        # the write has landed, rather than one the dispatch had to fit inside.
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
        await client.collect_until(lambda: backend.process.size == (120, 40), timeout=10.0)
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
        await _rejected(intruder, channel, "terminal/input")

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
                {
                    "channel": "agenthost-terminal:/x",
                    "claim": {"kind": "session", "session": "s", "chat": "ahp-chat:/s"},
                },
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
    async def pty_host(self) -> AsyncIterator[Host]:
        from ahp_host.core.pty_backend import PtyTerminalBackend

        host = Host(EchoProvider(), LoopbackSingleUserPolicy(), terminals=PtyTerminalBackend())
        try:
            yield host
        finally:
            # Not decoration: a terminal these tests leave open is a real shell,
            # and without `aclose` it outlives pytest itself.
            await host.aclose()

    async def test_exiting_publishes_terminal_exited_with_the_code(self, pty_host: Host) -> None:
        client = await _client(pty_host)
        await client.request("createSession", {"channel": "echo:/t-exit"})
        await _session_ready(pty_host, client, "echo:/t-exit")
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
        await _acted(client, channel, "terminal/exited")
        # A real elapsed wait AFTER the exit, kept on purpose: the second half
        # of this test is the NEGATIVE claim that nothing follows the exit
        # frame, and stopping the moment it arrives would make that claim true
        # by construction.
        #
        # One second, not the 0.4 this was first converted to. The failure it
        # exists to catch is buffered pty output arriving LATE on a loaded
        # runner -- which is the same load case this whole conversion is about,
        # so the one window that must stay generous is this one. The window it
        # replaced was ~2.2s of real post-exit time; 0.4s was a narrowing, and
        # the module's next-slowest test is 0.68s, so the second is free.
        await client.collect(seconds=1.0)

        exits = [
            a["action"] for a in client.actions(channel) if a["action"]["type"] == "terminal/exited"
        ]
        assert exits, "the shell exited and nothing said so"
        assert exits[-1]["exitCode"] == 7

        # And the exit is LAST: the parser is flushed first, so the tail of the
        # output arrives before the frame that says there is no more coming.
        types = [a["action"]["type"] for a in client.actions(channel)]
        assert types[-1] == "terminal/exited", types[-4:]

        # Since 0.9.0 the exit is state, not a stray top-level field: the
        # terminal and its catalogue row both say `exited`, code included.
        state = (await client.request("subscribe", {"channel": channel}))["result"]["snapshot"][
            "state"
        ]
        assert state["lifecycle"] == {"status": "exited", "exitCode": 7}

    async def test_the_catalogue_carries_the_required_fields(self, pty_host: Host) -> None:
        """`TerminalInfo` requires `resource`, `title` and `claim`. We sent
        `{resource, isPty}` -- and `isPty` is not even a TerminalInfo field."""
        client = await _client(pty_host)
        await client.request("createSession", {"channel": "echo:/t-cat"})
        await _session_ready(pty_host, client, "echo:/t-cat")
        await client.request(
            "createTerminal",
            {
                "channel": "ahp-terminal:/cat-1",
                "claim": {"kind": "client", "clientId": "c1"},
                "cwd": os.getcwd(),
                "name": "named",
            },
        )
        # The catalogue entry is published as one dict, so its arrival is the
        # end state for all three fields asserted below.
        await client.collect_until(
            lambda: any(t.get("resource") == "ahp-terminal:/cat-1" for t in _catalogue(pty_host)),
            timeout=10.0,
        )

        root = (await client.request("subscribe", {"channel": ROOT_URI}))["result"]["snapshot"][
            "state"
        ]
        entry = root["terminals"][0]
        assert entry["resource"] == "ahp-terminal:/cat-1"
        assert entry["title"] == "named"
        assert entry["claim"] == {"kind": "client", "clientId": "c1"}


class TestTheCatalogueKeepsUp:
    """`RootState.terminals` was republished on create, exit and dispose only.

    `TerminalInfo` carries `title` and `claim`, and both of them change through
    ordinary client actions -- so the root list went on reporting the previous
    owner. That is the field a client reads to decide whether to offer an input
    box, so after a handover the terminal looked typeable in the window that had
    just given it away.
    """

    async def test_a_rename_reaches_the_root_list(self, wired: tuple[Host, FakeBackend]) -> None:
        host, _ = wired
        client = await _client(host)
        channel = await _open(client)
        await client.notify(
            "dispatchAction",
            {
                "channel": channel,
                "clientSeq": 1,
                "action": {"type": "terminal/titleChanged", "title": "renamed"},
            },
        )
        await client.collect_until(
            lambda: [t.get("title") for t in _catalogue(host)] == ["renamed"], timeout=10.0
        )

        root = (await client.request("subscribe", {"channel": ROOT_URI}))["result"]["snapshot"][
            "state"
        ]
        assert [t["title"] for t in root["terminals"]] == ["renamed"]

    async def test_a_handover_reaches_the_root_list(self, wired: tuple[Host, FakeBackend]) -> None:
        host, _ = wired
        client = await _client(host)
        channel = await _open(client)
        handed = {"kind": "session", "session": "echo:/s", "chat": "ahp-chat:/s"}
        await client.notify(
            "dispatchAction",
            {
                "channel": channel,
                "clientSeq": 1,
                "action": {"type": "terminal/claimed", "claim": handed},
            },
        )
        await client.collect_until(
            lambda: [t.get("claim") for t in _catalogue(host)] == [handed], timeout=10.0
        )

        root = (await client.request("subscribe", {"channel": ROOT_URI}))["result"]["snapshot"][
            "state"
        ]
        assert [t["claim"] for t in root["terminals"]] == [handed]
        # And the two states agree: the catalogue entry is projected from the
        # channel's own state rather than remembered separately.
        assert host.sequencer.state_of(channel)["claim"] == handed

    async def test_the_same_action_at_a_chat_does_not_touch_the_catalogue(
        self, wired: tuple[Host, FakeBackend]
    ) -> None:
        """Both actions are client-dispatchable at any channel. Aimed at a chat
        they no-op in its reducer, and must not drag the root channel along."""
        host, _ = wired
        client = await _client(host)
        channel = await _open(client)
        await client.request("createSession", {"channel": "echo:/cat-noise"})
        # Bring-up must be COMPLETE before `before` is read below, or one of its
        # own publishes lands inside the window this test counts envelopes over.
        await _session_ready(host, client, "echo:/cat-noise")
        chat = (await client.request("subscribe", {"channel": "echo:/cat-noise"}))["result"][
            "snapshot"
        ]["state"]["chats"][0]["resource"]

        before = host.sequencer.server_seq
        await client.notify(
            "dispatchAction",
            {
                "channel": chat,
                "clientSeq": 1,
                "action": {"type": "terminal/titleChanged", "title": "nowhere"},
            },
        )
        # A real elapsed wait, kept: the assertion is that a SECOND envelope
        # never arrives, and a condition wait on the first would return before
        # the second had a chance to and prove nothing.
        await client.collect(seconds=0.3)
        # One envelope, the echo of the action itself -- not two.
        assert host.sequencer.server_seq == before + 1
        assert host.sequencer.state_of(channel)["title"] == "Test Terminal"


class TestAnUnnamedTerminalStillHasATitle:
    """`title` is REQUIRED on `TerminalState` and `name` is optional on
    `CreateTerminalParams`, so omitting it published a state that fails the
    whole `Snapshot.state` union -- while the root catalogue substituted
    "Terminal", leaving one terminal with two different titles."""

    async def test_the_channel_and_the_catalogue_agree(
        self, wired: tuple[Host, FakeBackend]
    ) -> None:
        host, _ = wired
        client = await _client(host)
        channel = "agenthost-terminal:/unnamed"
        await client.request(
            "createTerminal",
            {"channel": channel, "claim": {"kind": "client", "clientId": "c1"}},
        )
        state = (await client.request("subscribe", {"channel": channel}))["result"]["snapshot"][
            "state"
        ]
        assert state["title"] == "Terminal"

        root = (await client.request("subscribe", {"channel": ROOT_URI}))["result"]["snapshot"][
            "state"
        ]
        assert [t["title"] for t in root["terminals"]] == [state["title"]]

    async def test_an_empty_name_is_not_an_empty_tab(self, wired: tuple[Host, FakeBackend]) -> None:
        """`name: ""` is schema-valid and renders as a blank tab, which is the
        same unusable row by another route."""
        host, _ = wired
        client = await _client(host)
        channel = "agenthost-terminal:/blank"
        await client.request(
            "createTerminal",
            {"channel": channel, "claim": {"kind": "client", "clientId": "c1"}, "name": ""},
        )
        state = (await client.request("subscribe", {"channel": channel}))["result"]["snapshot"][
            "state"
        ]
        assert state["title"] == "Terminal"


class TestADuplicateChannelIsNotASession:
    """-32003 is defined as "a session with the given URI already exists", and
    the shared helper's message says "Session" -- so a client re-creating a
    terminal was told a session collided, on a URI naming no session."""

    async def test_the_code_and_the_message_name_a_channel(
        self, wired: tuple[Host, FakeBackend]
    ) -> None:
        host, _ = wired
        client = await _client(host)
        channel = await _open(client)
        again = await client.request(
            "createTerminal",
            {"channel": channel, "claim": {"kind": "client", "clientId": "c1"}},
        )
        assert again["error"]["code"] == AHP_ERROR_CODES["AlreadyExists"]
        assert "Session" not in again["error"]["message"]
        assert channel in again["error"]["message"]


class TestDisposalIsNotClaimGated:
    """Deliberate, and pinned so nobody "fixes" it into an immortal terminal.

    A peer refused `terminal/input` can still dispose the terminal. The spec
    attaches no ownership rule to the command -- `DisposeTerminalParams` carries
    a channel and nothing else -- and gating it on the claim would mean that a
    terminal handed to a SESSION could never be disposed by anybody, because a
    session claim is held by no client. The shell would then run until the host
    stopped. Disposal is a command, so it is gated where commands are: `Policy`.
    """

    async def test_a_session_claimed_terminal_can_still_be_disposed(
        self, wired: tuple[Host, FakeBackend]
    ) -> None:
        host, backend = wired
        client = await _client(host)
        channel = "agenthost-terminal:/handed"
        await client.request(
            "createTerminal",
            {
                "channel": channel,
                "claim": {"kind": "session", "session": "echo:/s", "chat": "ahp-chat:/s"},
                "name": "t",
            },
        )
        # Nobody holds a session claim, so this is the case a claim gate would
        # make unkillable.
        response = await client.request("disposeTerminal", {"channel": channel})
        assert "error" not in response, response
        assert backend.process.killed
        assert not host.sequencer.has_channel(channel)

    async def test_the_policy_is_where_disposal_is_refused(self) -> None:
        """The gate that does exist, exercised -- so "gated at the policy layer"
        is a claim with a test behind it rather than a docstring."""

        class NoDisposal(LoopbackSingleUserPolicy):
            def may_see_channel(self, info: Any, channel: str) -> bool:
                return not channel.startswith("agenthost-terminal:")

        backend = FakeBackend()
        host = Host(EchoProvider(), NoDisposal(), terminals=backend)
        try:
            client = await _client(host)
            channel = "agenthost-terminal:/protected"
            await client.request(
                "createTerminal",
                {"channel": channel, "claim": {"kind": "client", "clientId": "c1"}, "name": "t"},
            )
            response = await client.request("disposeTerminal", {"channel": channel})
            assert response["error"]["code"] == -32009
            assert not backend.process.killed
        finally:
            await host.aclose()


class TestTheBangCommandShellDiesWithItsTurn:
    """Cancelling a `!command` turn leaked the child shell.

    `_run_terminal_command` parks on `process.wait()`; `chat/turnCancelled`
    cancels that task, and the kill was on the line *after* the await. The
    one-shot has no channel and no `_Terminal`, so `aclose`'s sweep of
    `_live_terminals` could not see it either: measured, the child outlived the
    turn, the host, and the host's process.
    """

    @pytest.fixture
    async def spied(self) -> AsyncIterator[tuple[Host, list[Any]]]:
        from ahp_host.core.pty_backend import PtyTerminalBackend

        started: list[Any] = []

        class SpyBackend(PtyTerminalBackend):
            async def create(self, request: TerminalRequest, output: OutputSink) -> Any:
                process = await super().create(request, output)
                started.append(process)
                return process

        host = Host(EchoProvider(), LoopbackSingleUserPolicy(), terminals=SpyBackend())
        try:
            yield host, started
        finally:
            await host.aclose()

    @staticmethod
    def _alive(pid: int) -> bool:
        return subprocess.run(["ps", "-p", str(pid)], capture_output=True).returncode == 0

    async def test_cancelling_the_turn_takes_the_child_with_it(
        self, spied: tuple[Host, list[Any]]
    ) -> None:
        host, started = spied
        client = await _client(host)
        await client.request("createSession", {"channel": "echo:/bang"})
        await _session_ready(host, client, "echo:/bang")
        chat = (await client.request("subscribe", {"channel": "echo:/bang"}))["result"]["snapshot"][
            "state"
        ]["chats"][0]["resource"]
        await client.request("subscribe", {"channel": chat})

        await client.notify(
            "dispatchAction",
            {
                "channel": chat,
                "clientSeq": 1,
                "action": {
                    "type": "chat/turnStarted",
                    "turnId": "t1",
                    "startedAt": "1970-01-01T00:00:01.000Z",
                    # Long enough that only the cancel can end it.
                    "message": {"text": "!sleep 400", "origin": {"kind": "user"}},
                },
            },
        )
        # POLLED, not `collect_until`: the one-shot has no channel of its own,
        # so nothing on the wire marks the moment the backend was asked for a
        # shell -- and a predicate that is only re-checked when a notification
        # arrives would sit out its whole timeout waiting for a frame that is
        # never sent. Same shape as the liveness loop below.
        deadline = asyncio.get_running_loop().time() + 15
        while not started and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.05)
        assert started, "the `!` prefix never reached the backend"
        pid = started[0]._process.pid
        assert self._alive(pid), "the shell never started"

        await client.notify(
            "dispatchAction",
            {
                "channel": chat,
                "clientSeq": 2,
                "action": {"type": "chat/turnCancelled", "turnId": "t1", "duration": 0},
            },
        )
        deadline = asyncio.get_running_loop().time() + 5
        while self._alive(pid) and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.1)
        assert not self._alive(pid), "the shell outlived the turn that started it"
        assert not host._oneshot_terminals, "the one-shot registry still holds a dead child"


class TestCommandFinishedWithoutAnExitCode:
    async def test_the_exit_code_member_is_omitted_not_null(
        self, wired: tuple[Host, FakeBackend]
    ) -> None:
        """OSC 633 `D` may carry no code (an interrupted command). The schema
        declares `exitCode` an optional NUMBER -- "`undefined` if the shell did
        not report one" -- and the reducer writes the value through into
        `TerminalCommandPart`, so an explicit null failed the action schema and
        every later snapshot of the channel. Omission, as `terminal/exited`
        already does."""
        host, backend = wired
        client = await _client(host)
        channel = await _open(client)
        await backend.emit(b"\x1b]633;E;make\x07\x1b]633;C\x07building\r\n\x1b]633;D\x07")
        await _acted(client, channel, "terminal/commandFinished")

        finished = next(
            e["action"]
            for e in client.actions(channel)
            if e["action"]["type"] == "terminal/commandFinished"
        )
        assert "exitCode" not in finished


class EagerBackend(FakeBackend):
    """Delivers output from INSIDE `create`, before it returns.

    The pty backend's shape: `add_reader` is armed in the process constructor,
    so the child's first bytes -- a fast prompt, an immediate error -- can
    reach the sink while `_create_terminal` is still awaiting registration."""

    def __init__(self, early: bytes) -> None:
        super().__init__()
        self.early = early

    async def create(self, request: TerminalRequest, output: OutputSink) -> FakeProcess:
        process = await super().create(request, output)
        output(self.early)
        return process


class TestOutputDuringSpawn:
    async def test_bytes_written_before_registration_still_arrive(self) -> None:
        """Chunks for a channel not yet in `_live_terminals` were returned to
        nobody: the first bytes of a fast shell never reached `terminal/data`."""
        backend = EagerBackend(b"early prompt$ ")
        host = Host(EchoProvider(), LoopbackSingleUserPolicy(), terminals=backend)
        try:
            client = await _client(host)
            channel = await _open(client)
            await backend.emit(b"and then output\r\n")

            state = (await client.request("subscribe", {"channel": channel}))["result"]["snapshot"][
                "state"
            ]
            # `content` parts keep arrival order, so the serialized state does
            # too -- shape-independent, since parts store text under different
            # keys (`value`, `output`) depending on classification.
            text = json.dumps(state)
            assert "early prompt$" in text
            # In arrival order: spawn-window bytes precede post-registration ones.
            assert text.index("early prompt$") < text.index("and then output")
        finally:
            await host.aclose()


class TestStrictClaimGating:
    """The knob `core/terminals.py` documents: a multi-trust-domain host passes
    `STRICT_CLAIM_GATED_ACTIONS` via `Host(claim_gated_actions=...)` and the
    contested actions -- `terminal/cleared`, `terminal/resized`,
    `terminal/titleChanged` -- become claim-gated too."""

    async def test_a_non_holder_cannot_resize_under_the_strict_set(self) -> None:
        backend = FakeBackend()
        host = Host(
            EchoProvider(),
            LoopbackSingleUserPolicy(),
            terminals=backend,
            claim_gated_actions=STRICT_CLAIM_GATED_ACTIONS,
        )
        try:
            owner = await _client(host)
            channel = await _open(owner)

            other_transport, other_server = memory_pair()
            task = asyncio.create_task(host.serve(other_server))
            assert task is not None
            viewer = FakeClient(other_transport)
            await viewer.request(
                "initialize",
                {
                    "channel": ROOT_URI,
                    "clientId": "viewer",
                    "protocolVersions": ["0.7.0"],
                    "initialSubscriptions": [ROOT_URI],
                },
            )
            await viewer.request("subscribe", {"channel": channel})
            await viewer.notify(
                "dispatchAction",
                {
                    "channel": channel,
                    "clientSeq": 1,
                    "action": {"type": "terminal/resized", "cols": 10, "rows": 5},
                },
            )
            await _rejected(viewer, channel, "terminal/resized")

            assert backend.process.size is None, "a non-holder reflowed somebody else's pty"
            echoes = [
                e for e in viewer.actions(channel) if e["action"]["type"] == "terminal/resized"
            ]
            assert echoes
            assert "rejectionReason" in echoes[-1]

            # The claim holder is not collateral damage.
            await owner.notify(
                "dispatchAction",
                {
                    "channel": channel,
                    "clientSeq": 1,
                    "action": {"type": "terminal/resized", "cols": 120, "rows": 40},
                },
            )
            await owner.collect_until(lambda: backend.process.size == (120, 40), timeout=10.0)
            assert backend.process.size == (120, 40)
        finally:
            await host.aclose()
