"""Replay the request shapes a real VS Code 1.131 sent, against a fresh host.

The fixture beside this file was captured from a live connection, not written by
hand. It is the only regression guarding three defects that neither the 200
reducer fixtures nor the npm-client interop test caught, because all three come
from VS Code doing something the spec's *examples* do not:

* it opens with ``reconnect``, not ``initialize``, and does not fall back when
  refused -- it retries forever, so the connection simply never establishes;
* its session URIs are ``<provider>:/<uuid>``, not ``ahp-session:/<uuid>``, so a
  host routing reducers on the URI scheme applies none at all and its state
  silently freezes at the snapshot;
* it prefers protocol ``0.7.0``. (This capture recorded ``["0.7.0"]`` alone,
  but the reference client sends its full four-version ladder --
  ``registry.ts`` `SUPPORTED_PROTOCOL_VERSIONS` -- so only the most-preferred
  entry is guarded, and a faithful re-capture still passes.)

Replaying shapes rather than the raw log on purpose: ids, UUIDs and VS Code's
polling volume are noise, and a byte-exact replay would be brittle without
testing anything more.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agent_host_server.core import Host, LoopbackSingleUserPolicy
from agent_host_server.provider import EchoProvider

from .test_host_end_to_end import FakeClient

pytestmark = pytest.mark.anyio

FIXTURE = Path(__file__).parent / "fixtures" / "vscode-1.131-client-requests.json"
TRACE: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
REQUESTS: list[dict[str, Any]] = TRACE["requests"]

#: Nothing answers `MethodNotFound` any more: every method VS Code probes is
#: implemented. What is left are *specific* refusals, which tell a client more
#: than -32601 does.
EXPECTED_REFUSALS: set[str] = set()

#: Implemented, and answered `InvalidParams` because the demo agent advertises
#: no protected resources and installs no session config beyond what it
#: declares. Distinct from "not implemented" for the same reason as the others.
EXPECTED_INVALID = {"authenticate"}

#: Implemented, but answered `NotFound` (-32008) by this host, because the
#: default resource provider exposes nothing. That is the distinction the
#: `resource*` family is meant to make: "this host has no such file" is a
#: different answer from "this host does not do files", and a host does not
#: acquire a filesystem by being upgraded.
#: `reconnect` is here because the capture OPENS with one, carrying a clientId
#: from a previous window that this fresh host has never admitted. `NotFound`
#: is the designed answer, not a failure: the client catches exactly that code
#: and issues a fresh `initialize` ("Server forgot client ...; initializing a
#: fresh connection" -- present in the shipping 1.131.0 bundles). Resuming a
#: stranger instead looked successful and left the client without a
#: `defaultDirectory`, because `initialize` is the only place it assigns one.
#:
#: `disposeTerminal` no longer errors at all -- disposal is idempotent -- so it
#: is tolerated here rather than asserted.
EXPECTED_ABSENT = {"resourceList", "resourceRead", "resourceResolve", "reconnect"}

#: Implemented, but declined because this host installs no watcher. Same
#: distinction as EXPECTED_ABSENT: "nothing to watch here" is not "this host
#: cannot watch".
#: Declined for a stated reason rather than unimplemented: no watcher and no
#: terminal backend are installed, and a terminal backend deliberately is not
#: shipped in this distribution at all.
EXPECTED_DENIED = {"createResourceWatch", "createTerminal"}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _request(method: str, action_type: str | None = None) -> dict[str, Any]:
    for entry in REQUESTS:
        if entry["method"] != method:
            continue
        if action_type is None or (entry["params"].get("action") or {}).get("type") == action_type:
            return entry
    raise AssertionError(f"fixture has no {method} {action_type or ''}")


class TestCapturedShapes:
    """Assertions about the capture itself, so a re-capture that loses the
    interesting shapes fails loudly rather than quietly weakening the test."""

    def test_vscode_opens_with_reconnect(self) -> None:
        assert REQUESTS[0]["method"] == "reconnect"
        assert REQUESTS[1]["method"] == "initialize"

    def test_vscode_prefers_protocol_0_7_0(self) -> None:
        """First entry only, not the whole array: the reference client
        advertises four versions (`registry.ts` SUPPORTED_PROTOCOL_VERSIONS,
        with an explicit negotiate-down comment), so pinning `== ["0.7.0"]`
        would fail a faithful re-capture. This capture happened to record a
        single entry; what the replay depends on is which version is
        most-preferred."""
        versions = _request("initialize")["params"]["protocolVersions"]
        assert versions[0] == "0.7.0"

    def test_session_uri_uses_the_provider_scheme(self) -> None:
        channel = _request("createSession")["params"]["channel"]
        assert channel.startswith("echo:/"), channel
        assert not channel.startswith("ahp-session:"), "capture lost the provider-scheme URI"

    def test_create_session_carries_plural_working_directories(self) -> None:
        """`workingDirectories` is the 0.7.0 shape; the singular was removed."""
        params = _request("createSession")["params"]
        assert isinstance(params["workingDirectories"], list)
        assert "workingDirectory" not in params

    def test_create_session_contributes_client_tools(self) -> None:
        """VS Code offers its own tools for the agent to invoke -- the
        active-client tool routing v0.1 does not implement but must not choke on."""
        active = _request("createSession")["params"]["activeClient"]
        assert active["clientId"]
        assert isinstance(active["tools"], list)
        assert active["tools"]

    def test_no_home_paths_leaked_into_the_fixture(self) -> None:
        assert "/Users/" not in FIXTURE.read_text(encoding="utf-8")


class TestReplay:
    async def test_the_whole_captured_sequence_runs(self) -> None:
        """Replay every shape in order; nothing may fail except the known refusals."""
        host = Host(EchoProvider(), LoopbackSingleUserPolicy())
        import asyncio

        from agent_host_protocol.transport import memory_pair

        client_transport, server_transport = memory_pair()
        serve = asyncio.create_task(host.serve(server_transport))
        client = FakeClient(client_transport)
        try:
            unexpected: list[tuple[str, Any]] = []
            for entry in REQUESTS:
                method, params = entry["method"], dict(entry["params"])
                if "id" not in entry:
                    await client.notify(method, params)
                    continue
                response = await client.request(method, params)
                if "error" not in response:
                    continue
                if method in EXPECTED_REFUSALS:
                    assert response["error"]["code"] == -32601, (method, response["error"])
                    continue
                if method in EXPECTED_ABSENT:
                    assert response["error"]["code"] == -32008, (method, response["error"])
                    continue
                if method in EXPECTED_DENIED:
                    assert response["error"]["code"] == -32009, (method, response["error"])
                    continue
                if method in EXPECTED_INVALID:
                    assert response["error"]["code"] == -32602, (method, response["error"])
                    continue
                unexpected.append((method, response["error"]))
            assert not unexpected, f"unexpected failures: {unexpected}"
        finally:
            serve.cancel()
            await host.aclose()

    async def test_a_turn_completes_over_the_captured_session_uri(self) -> None:
        """The end the user sees: create the session VS Code's way, run its turn."""
        import asyncio

        from agent_host_protocol.transport import memory_pair

        host = Host(EchoProvider(), LoopbackSingleUserPolicy())
        client_transport, server_transport = memory_pair()
        serve = asyncio.create_task(host.serve(server_transport))
        client = FakeClient(client_transport)
        try:
            await client.request("initialize", _request("initialize")["params"])

            session_uri = _request("createSession")["params"]["channel"]
            await client.request("createSession", _request("createSession")["params"])
            await client.collect(seconds=0.3)

            session = (await client.request("subscribe", {"channel": session_uri}))["result"]
            state = session["snapshot"]["state"]
            assert state["lifecycle"] == "ready", "bring-up did not complete"
            chat_uri = state["chats"][0]["resource"]

            await client.request("subscribe", {"channel": chat_uri})
            turn = dict(_request("dispatchAction", "chat/turnStarted")["params"])
            turn["channel"] = chat_uri
            await client.notify("dispatchAction", turn)
            await client.collect(seconds=0.5)

            fresh = (await client.request("subscribe", {"channel": chat_uri}))["result"]
            turns = fresh["snapshot"]["state"]["turns"]
            assert len(turns) == 1, turns
            assert turns[0]["state"] == "complete"
            text = "".join(
                part.get("content", "")
                for part in turns[0]["responseParts"]
                if part.get("kind") == "markdown"
            )
            assert text.startswith("You said: "), text
        finally:
            serve.cancel()
            await host.aclose()


class TestMeasuredCallCounts:
    """The evidence a prioritisation argument rests on, in the repository.

    Two of the seven scoping areas were originally ranked on per-method refusal
    frequencies that lived only in an uncommitted wire log. A reviewer could not
    check them, so they are counted here instead -- method names and counts
    only, never params.
    """

    @staticmethod
    def _measured() -> dict[str, Any]:
        measured: dict[str, Any] = TRACE["measured"]
        return measured

    def test_the_counts_carry_no_message_content(self) -> None:
        """The capture is a full transcript of a real conversation, so only the
        aggregate crosses into the repository."""
        measured = self._measured()
        assert set(measured) == {
            "note",
            "sessionsCreated",
            "clientToServer",
            "serverToClient",
        }
        for direction in ("clientToServer", "serverToClient"):
            assert all(isinstance(v, int) for v in measured[direction].values())

    def test_the_resource_family_dominates_the_refusals(self) -> None:
        """This is why `resource*` is the next release and terminals are not:
        it is not a judgement about which feature is nicer."""
        counts = self._measured()["clientToServer"]
        resource_calls = sum(v for k, v in counts.items() if k.startswith("resource"))
        assert resource_calls > 100
        assert resource_calls > 10 * counts.get("createTerminal", 0)

    def test_the_terminal_toast_is_rare_per_session(self) -> None:
        """The scoping pass cited `createTerminal` x7. Measured, it is about one
        per session -- which is what made "spend 250 lines on the toast" the
        wrong trade (docs/roadmap.md section 7)."""
        measured = self._measured()
        per_session = (
            measured["clientToServer"].get("createTerminal", 0) / measured["sessionsCreated"]
        )
        assert per_session <= 1
