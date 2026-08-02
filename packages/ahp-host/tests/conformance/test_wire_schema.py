"""Every frame this host emits, validated against the vendored schemas.

Drives real flows over the in-memory transport, captures what a client would
actually receive, and checks each frame against the shape the spec declares.
Not a unit test of a serialiser: the frames come from a running host doing the
thing, because every defect this file exists for looked correct in isolation.

If this fails, the fix is the HOST, not the assertion. The schemas are vendored
from a pinned upstream revision (see UPSTREAM.md); if the pin moved, re-vendor
rather than relaxing a check.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from agent_host_protocol.channels import ROOT_URI
from agent_host_protocol.transport import memory_pair

from agent_host_server.core import Host, LoopbackSingleUserPolicy
from agent_host_server.core.pty_backend import PtyTerminalBackend
from agent_host_server.core.resources import RootedFilesystemResourceProvider
from agent_host_server.provider import EchoProvider

from .schemas import assert_valid_action, assert_valid_result, assert_valid_state

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class Recorder:
    """A client that only records. Every frame the host sent, in order."""

    def __init__(self, transport: Any) -> None:
        self.transport = transport
        self.frames: list[dict[str, Any]] = []
        self._next_id = 0
        #: request id -> method, so a captured result can be matched to the
        #: definition that describes it.
        self._methods: dict[int, str] = {}

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._next_id += 1
        self._methods[self._next_id] = method
        request_id = self._next_id
        await self.transport.send(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        )
        while True:
            message = await asyncio.wait_for(self.transport.receive(), timeout=5)
            assert message is not None
            self.frames.append(message)
            if message.get("id") == request_id:
                found: dict[str, Any] = message
                return found

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        await self.transport.send({"jsonrpc": "2.0", "method": method, "params": params})

    async def drain(self, seconds: float = 0.8) -> None:
        deadline = asyncio.get_running_loop().time() + seconds
        while asyncio.get_running_loop().time() < deadline:
            try:
                message = await asyncio.wait_for(self.transport.receive(), timeout=0.1)
            except TimeoutError:
                continue
            if message is not None:
                self.frames.append(message)

    # ─── what the schemas get pointed at ─────────────────────────────────

    @property
    def actions(self) -> list[dict[str, Any]]:
        """Actions the HOST originated.

        Echoes of client-dispatched actions are excluded: a host MUST echo one
        even when it is malformed -- that is how a client learns its optimistic
        prediction was rejected -- so validating them would fail this host for
        the client's mistakes. An echo is identifiable by its `origin`, which
        the host stamps from the connection and never sets on its own actions.
        """
        return [
            frame["params"]["action"]
            for frame in self.frames
            if frame.get("method") == "action"
            and "action" in (frame.get("params") or {})
            and "origin" not in frame["params"]
        ]

    def results(self) -> list[tuple[str, Any]]:
        """`(method, result)` for every command that returned one."""
        return [
            (self._methods[frame["id"]], frame["result"])
            for frame in self.frames
            if isinstance(frame.get("result"), dict) and frame.get("id") in self._methods
        ]

    def snapshots(self) -> list[tuple[str, Any]]:
        """`(channel, state)` for every snapshot the host handed back."""
        found: list[tuple[str, Any]] = []
        for frame in self.frames:
            result = frame.get("result")
            if not isinstance(result, dict):
                continue
            snapshot = result.get("snapshot")
            if isinstance(snapshot, dict) and "state" in snapshot:
                found.append((snapshot.get("resource", ""), snapshot["state"]))
            for entry in result.get("snapshots") or []:
                if isinstance(entry, dict) and "state" in entry:
                    found.append((entry.get("resource", ""), entry["state"]))
        return found


def _kind_of(channel: str) -> str | None:
    if channel == ROOT_URI:
        return "root"
    if channel.endswith("/annotations"):
        return "annotations"
    if channel.startswith("ahp-chat"):
        return "chat"
    if channel.startswith("ahp-terminal"):
        return "terminal"
    if channel.startswith("ahp-changeset"):
        return "changeset"
    if channel.startswith("echo:"):
        return "session"
    return None


def _check(recorder: Recorder) -> int:
    """Validate everything captured. Returns how many actions were checked."""
    for action in recorder.actions:
        assert_valid_action(action)
    for channel, state in recorder.snapshots():
        kind = _kind_of(channel)
        if kind is not None:
            assert_valid_state(kind, state)
    for method, result in recorder.results():
        assert_valid_result(method, result)
    return len(recorder.actions)


@pytest.fixture
async def recorder() -> AsyncIterator[tuple[Host, Recorder]]:
    host = Host(
        EchoProvider(customizations=True, delay=0.01),
        LoopbackSingleUserPolicy(),
        resources=RootedFilesystemResourceProvider(Path.cwd()),
        terminals=PtyTerminalBackend(),
        default_directory="file://" + os.getcwd(),
        completion_trigger_characters=("#",),
    )
    client_transport, server_transport = memory_pair()
    serve = asyncio.create_task(host.serve(server_transport))
    client = Recorder(client_transport)
    await client.request(
        "initialize",
        {
            "channel": ROOT_URI,
            "clientId": "conformance",
            "protocolVersions": ["0.7.0"],
            "initialSubscriptions": [ROOT_URI],
        },
    )
    try:
        yield host, client
    finally:
        serve.cancel()
        await host.aclose()


class TestTheWireMatchesTheSpec:
    async def test_a_whole_turn(self, recorder: tuple[Host, Recorder]) -> None:
        """The path every defect in `chat/*` lived on."""
        host, client = recorder
        await client.request("createSession", {"channel": "echo:/c1", "provider": "echo"})
        await client.drain(0.4)
        state = (await client.request("subscribe", {"channel": "echo:/c1"}))["result"]["snapshot"][
            "state"
        ]
        chat = state["chats"][0]["resource"]
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
                    "message": {"text": "hello", "origin": {"kind": "user"}},
                },
            },
        )
        await client.drain(1.0)
        assert _check(client) > 3, "the turn published almost nothing; check the flow"

    async def test_a_failed_turn(self) -> None:
        """`chat/error` is where the missing `errorType` lived."""

        class Exploding(EchoProvider):
            async def create_session(self, context: Any) -> Any:
                session = await super().create_session(context)

                async def boom(message: Any, sink: Any) -> None:
                    raise RuntimeError("boom")

                session.send_user_message = boom  # type: ignore[method-assign]
                return session

        host = Host(Exploding(), LoopbackSingleUserPolicy())
        client_transport, server_transport = memory_pair()
        serve = asyncio.create_task(host.serve(server_transport))
        client = Recorder(client_transport)
        try:
            await client.request(
                "initialize",
                {
                    "channel": ROOT_URI,
                    "clientId": "c",
                    "protocolVersions": ["0.7.0"],
                    "initialSubscriptions": [ROOT_URI],
                },
            )
            await client.request("createSession", {"channel": "echo:/err", "provider": "echo"})
            await client.drain(0.4)
            state = (await client.request("subscribe", {"channel": "echo:/err"}))["result"][
                "snapshot"
            ]["state"]
            chat = state["chats"][0]["resource"]
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
                        "message": {"text": "go", "origin": {"kind": "user"}},
                    },
                },
            )
            await client.drain(0.8)
            assert any(a["type"] == "chat/error" for a in client.actions)
            _check(client)
        finally:
            serve.cancel()
            await host.aclose()

    async def test_a_terminal(self, recorder: tuple[Host, Recorder]) -> None:
        """`root/terminalsChanged` and `terminal/*` -- where the missing
        required `title`/`claim` and the ISO-string timestamp lived."""
        host, client = recorder
        await client.request("createSession", {"channel": "echo:/t1", "provider": "echo"})
        await client.drain(0.3)
        channel = "ahp-terminal:/conf-1"
        result = await client.request(
            "createTerminal",
            {
                "channel": channel,
                "claim": {"kind": "client", "clientId": "conformance"},
                "cwd": os.getcwd(),
                "cols": 80,
                "rows": 24,
                "name": "conformance",
            },
        )
        assert "error" not in result, result.get("error")
        await client.request("subscribe", {"channel": channel})
        # Emit the OSC 633 shell-integration sequences by hand. Nothing
        # INJECTS them -- that is a deliberate non-goal, since the injection
        # scripts are a VS Code product artifact -- so without this the
        # `terminal/commandExecuted` and `terminal/commandFinished` frames are
        # never produced by the demo and this file cannot check their shape.
        # That is exactly where the ISO-string-where-a-number-is-declared
        # defect lived.
        osc = (
            r"printf '\033]633;E;ls -1\a\033]633;C\a'; "
            r"printf 'output\n'; printf '\033]633;D;0\a'; exit 0"
        )
        await client.notify(
            "dispatchAction",
            {
                "channel": channel,
                "clientSeq": 1,
                "action": {"type": "terminal/input", "data": osc + "\n"},
            },
        )
        await client.drain(2.5)
        emitted = {a["type"] for a in client.actions}
        assert "terminal/commandExecuted" in emitted, (
            f"the shell-integration path was not exercised; saw {sorted(emitted)}"
        )
        _check(client)

    async def test_customizations_and_the_root_channel(
        self, recorder: tuple[Host, Recorder]
    ) -> None:
        host, client = recorder
        await client.request("createSession", {"channel": "echo:/cz", "provider": "echo"})
        # Customizations arrive on the SESSION channel, not on root -- so a
        # subscription is required to see them at all.
        await client.request("subscribe", {"channel": "echo:/cz"})
        await client.drain(0.8)
        _check(client)
        # The tree ships in the session's INITIAL SNAPSHOT rather than as an
        # action -- `describe_session` feeds it into the registered state. So
        # `_check` above has already validated it against `SessionState`; this
        # only proves the flow produced one to validate.
        trees = [
            state.get("customizations")
            for channel, state in client.snapshots()
            if _kind_of(channel) == "session" and isinstance(state, dict)
        ]
        assert any(trees), "the demo published no customizations"

    async def test_completions_and_a_confirmed_tool_call(self) -> None:
        """The surfaces the other flows never reach.

        `CompletionItem` carried an invented `label`/`detail` and an attachment
        keyed `kind` instead of `type`; tool RESULT content had the same
        `kind`/`type` mix-up. Neither is an action, and neither appears in a
        plain turn, so without this the harness would pass while both were
        wrong -- which is precisely the failure mode it exists to end.
        """
        host = Host(EchoProvider(confirm_tools=True, delay=0.01), LoopbackSingleUserPolicy())
        client_transport, server_transport = memory_pair()
        serve = asyncio.create_task(host.serve(server_transport))
        client = Recorder(client_transport)
        try:
            await client.request(
                "initialize",
                {
                    "channel": ROOT_URI,
                    "clientId": "c",
                    "protocolVersions": ["0.7.0"],
                    "initialSubscriptions": [ROOT_URI],
                },
            )
            await client.request("createSession", {"channel": "echo:/ct", "provider": "echo"})
            await client.drain(0.4)
            state = (await client.request("subscribe", {"channel": "echo:/ct"}))["result"][
                "snapshot"
            ]["state"]
            chat = state["chats"][0]["resource"]
            await client.request("subscribe", {"channel": chat})

            # completions -> CompletionsResult, with its attachments
            completions = await client.request(
                "completions",
                {"channel": "echo:/ct", "kind": "file", "text": "#re", "offset": 3},
            )
            assert "error" not in completions, completions.get("error")
            assert completions["result"]["items"], "no completion items to check"

            # a confirmed tool call, so tool RESULT content is emitted
            await client.notify(
                "dispatchAction",
                {
                    "channel": chat,
                    "clientSeq": 1,
                    "action": {
                        "type": "chat/turnStarted",
                        "turnId": "t1",
                        "startedAt": "1970-01-01T00:00:01.000Z",
                        "message": {"text": "run it", "origin": {"kind": "user"}},
                    },
                },
            )
            await client.drain(0.6)
            pending = [a for a in client.actions if a["type"] == "chat/toolCallReady"]
            assert pending, "the confirming path never asked"
            await client.notify(
                "dispatchAction",
                {
                    "channel": chat,
                    "clientSeq": 2,
                    "action": {
                        # Well-formed the way a real client sends it:
                        # `approved` and a `ToolCallConfirmationReason`, not the
                        # invented `confirmed: "confirmed"` an earlier draft of
                        # this test used and our host accepted anyway.
                        "type": "chat/toolCallConfirmed",
                        "turnId": "t1",
                        "toolCallId": pending[-1]["toolCallId"],
                        "approved": True,
                        "confirmed": "user-action",
                    },
                },
            )
            await client.drain(1.0)
            assert any(a["type"] == "chat/toolCallComplete" for a in client.actions), (
                "the tool never completed, so its result content was never checked"
            )
            _check(client)
        finally:
            serve.cancel()
            await host.aclose()
