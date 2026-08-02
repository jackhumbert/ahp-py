"""The reverse direction, and what it is for.

`resource*` is symmetrical — "MAY be sent in either direction" — and the
direction that had never been implemented is the one that makes a
client-published plugin render. A client "MAY synthesize a virtual plugin in
memory and rely on the host to expand it into concrete children", and until the
host does, that plugin appears as a container with nothing in it.

That is not hypothetical: it is why a plugin's skills, prompts, instructions and
hooks did not appear in VS Code's Agents app against this host. VS Code expands
them through `fileService` → `resourceRead` against the *client's* URIs, and
this host answered `MethodNotFound` to every one.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from agent_host_protocol.channels import ROOT_URI
from agent_host_protocol.transport import memory_pair

from agent_host_server.core import Host, LoopbackSingleUserPolicy
from agent_host_server.provider import EchoProvider

from .test_host_end_to_end import FakeClient

pytestmark = pytest.mark.anyio

_PLUGIN_URI = "virtual://my-client/workspace-skills"

#: What the client will serve when the host reads back.
_FILES: dict[str, str] = {
    f"{_PLUGIN_URI}/deploy.skill.md": "---\nname: Deploy Helper\n---\nsteps",
    f"{_PLUGIN_URI}/review.prompt.md": "# Review Prompt\nbody",
    f"{_PLUGIN_URI}/house-style.instructions.md": "# House Style\nrules",
    f"{_PLUGIN_URI}/notes.txt": "not a customization",
}


class ServingClient(FakeClient):
    """A client that answers the host's reverse `resource*` requests.

    This is the half that did not exist before: the host now *asks*, and the
    test has to be a peer rather than only a caller.
    """

    def __init__(self, transport: Any) -> None:
        super().__init__(transport)
        self.served: list[str] = []

    async def serve_reverse(self, *, seconds: float = 1.0) -> None:
        deadline = asyncio.get_running_loop().time() + seconds
        while asyncio.get_running_loop().time() < deadline:
            remaining = deadline - asyncio.get_running_loop().time()
            try:
                message = await asyncio.wait_for(self.transport.receive(), timeout=remaining)
            except TimeoutError:
                return
            if message is None:
                return
            if "method" not in message or "id" not in message:
                self.notifications.append(message)
                continue
            await self._answer(message)

    async def _answer(self, message: dict[str, Any]) -> None:
        method, params = message["method"], message.get("params") or {}
        uri = params.get("uri", "")
        self.served.append(f"{method} {uri}")
        if method == "resourceList":
            entries = [
                {"name": key.rsplit("/", 1)[1], "type": "file"}
                for key in _FILES
                if key.startswith(f"{uri}/")
            ]
            result: Any = {"entries": entries}
        elif method == "resourceRead" and uri in _FILES:
            result = {"data": _FILES[uri], "encoding": "utf-8"}
        else:
            await self.transport.send(
                {
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "error": {"code": -32008, "message": "no such resource"},
                }
            )
            return
        await self.transport.send({"jsonrpc": "2.0", "id": message["id"], "result": result})


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def host() -> AsyncIterator[Host]:
    host = Host(EchoProvider(), LoopbackSingleUserPolicy())
    try:
        yield host
    finally:
        await host.aclose()


async def _attach(host: Host) -> ServingClient:
    client_transport, server_transport = memory_pair()
    task = asyncio.create_task(host.serve(server_transport))
    client = ServingClient(client_transport)
    client.serve_task = task  # type: ignore[attr-defined]
    await client.request(
        "initialize",
        {
            "channel": ROOT_URI,
            "clientId": "vscode",
            "protocolVersions": ["0.7.0"],
            "initialSubscriptions": [ROOT_URI],
        },
    )
    return client


def _plugin(nonce: str = "sha256:one") -> dict[str, Any]:
    return {
        "type": "plugin",
        "id": "client-plugin-1",
        "uri": _PLUGIN_URI,
        "name": "Workspace Skills",
        "enabled": True,
        "nonce": nonce,
        # An unmodelled field. ADR 0001: the host is authoritative for state it
        # replays to clients newer than itself, so this MUST survive.
        "vendorExtension": {"future": True},
    }


async def _publish(client: ServingClient, session: str, plugin: dict[str, Any]) -> None:
    await client.notify(
        "dispatchAction",
        {
            "channel": session,
            "clientSeq": 1,
            "action": {
                "type": "session/activeClientSet",
                "activeClient": {
                    "clientId": "vscode",
                    "displayName": "VS Code",
                    "tools": [],
                    "customizations": [plugin],
                },
            },
        },
    )
    await client.serve_reverse(seconds=1.0)


async def _session(host: Host, client: ServingClient, uri: str) -> None:
    await client.request("createSession", {"channel": uri, "provider": "echo"})
    await client.collect(seconds=0.3)
    await client.request("subscribe", {"channel": uri})


class TestPluginExpansion:
    async def test_the_children_are_read_from_the_client_and_published(self, host: Host) -> None:
        """The whole point. Before this the plugin rendered as an empty
        container, because its children exist only in the client's memory."""
        client = await _attach(host)
        uri = "echo:/plugins-1"
        await _session(host, client, uri)
        await _publish(client, uri, _plugin())

        state = (await client.request("subscribe", {"channel": uri}))["result"]["snapshot"]["state"]
        plugin = next(c for c in state["customizations"] if c["id"] == "client-plugin-1")
        by_type = {c["type"]: c["name"] for c in plugin["children"]}
        assert by_type == {
            "skill": "Deploy Helper",
            "prompt": "Review Prompt",
            "rule": "House Style",
        }

    async def test_the_host_actually_asked_the_client(self, host: Host) -> None:
        client = await _attach(host)
        uri = "echo:/plugins-2"
        await _session(host, client, uri)
        await _publish(client, uri, _plugin())

        assert f"resourceList {_PLUGIN_URI}" in client.served
        assert any(call.startswith("resourceRead") for call in client.served)

    async def test_an_unmodelled_field_survives_expansion(self, host: Host) -> None:
        """ADR 0001: a parser that rebuilt the entry through closed models would
        drop fields it does not know, and the client that published them would
        get them back missing."""
        client = await _attach(host)
        uri = "echo:/plugins-3"
        await _session(host, client, uri)
        await _publish(client, uri, _plugin())

        state = (await client.request("subscribe", {"channel": uri}))["result"]["snapshot"]["state"]
        plugin = next(c for c in state["customizations"] if c["id"] == "client-plugin-1")
        assert plugin["vendorExtension"] == {"future": True}

    async def test_an_unrecognised_file_is_skipped_not_guessed(self, host: Host) -> None:
        """A mislabelled child renders in the wrong section and the client that
        published it cannot correct that."""
        client = await _attach(host)
        uri = "echo:/plugins-4"
        await _session(host, client, uri)
        await _publish(client, uri, _plugin())

        state = (await client.request("subscribe", {"channel": uri}))["result"]["snapshot"]["state"]
        plugin = next(c for c in state["customizations"] if c["id"] == "client-plugin-1")
        assert not any(c["uri"].endswith("notes.txt") for c in plugin["children"])

    async def test_an_unchanged_nonce_does_not_re_read(self, host: Host) -> None:
        """`nonce` is "an opaque version token used by the host to detect
        changes" -- a republication of the same thing must not cost a round trip
        per child file."""
        client = await _attach(host)
        uri = "echo:/plugins-5"
        await _session(host, client, uri)
        await _publish(client, uri, _plugin())
        first = len(client.served)

        await _publish(client, uri, _plugin())
        assert len(client.served) == first

    async def test_a_changed_nonce_re_reads(self, host: Host) -> None:
        client = await _attach(host)
        uri = "echo:/plugins-6"
        await _session(host, client, uri)
        await _publish(client, uri, _plugin())
        first = len(client.served)

        await _publish(client, uri, _plugin(nonce="sha256:two"))
        assert len(client.served) > first

    async def test_a_client_that_cannot_serve_its_plugin_is_not_fatal(self, host: Host) -> None:
        """It published something it could not back up -- its problem. The
        session carries on."""
        client = await _attach(host)
        uri = "echo:/plugins-7"
        await _session(host, client, uri)
        broken = {**_plugin(), "uri": "virtual://my-client/does-not-exist"}
        await _publish(client, uri, broken)

        assert "error" not in await client.request("ping", {"channel": ROOT_URI})
        state = (await client.request("subscribe", {"channel": uri}))["result"]["snapshot"]["state"]
        assert state["activeClients"][0]["clientId"] == "vscode"


class TestOutboundLifetime:
    async def test_a_response_frame_does_not_end_the_read_loop(self, host: Host) -> None:
        """An id-bearing, method-less frame that is not ours has no reply that
        could carry the problem -- invariant 17 covers responses too."""
        client = await _attach(host)
        await client.transport.send({"jsonrpc": "2.0", "id": 99999, "result": {}})
        await asyncio.sleep(0.2)
        assert "error" not in await client.request("ping", {"channel": ROOT_URI})

    async def test_a_stray_error_response_does_not_end_the_read_loop(self, host: Host) -> None:
        client = await _attach(host)
        await client.transport.send(
            {"jsonrpc": "2.0", "id": "ahs-99999", "error": {"code": -1, "message": "x"}}
        )
        await asyncio.sleep(0.2)
        assert "error" not in await client.request("ping", {"channel": ROOT_URI})

    async def test_requests_still_work_after_the_classification_change(self, host: Host) -> None:
        """A request carries BOTH `method` and `id`; testing `id` first would
        route every one of them into the response path."""
        client = await _attach(host)
        result = await client.request("listSessions", {"channel": ROOT_URI})
        assert "items" in result["result"]

    async def test_nothing_leaks_when_a_connection_drops_mid_request(self, host: Host) -> None:
        client = await _attach(host)
        uri = "echo:/plugins-8"
        await _session(host, client, uri)
        # Publish, then vanish without answering the host's reads.
        await client.notify(
            "dispatchAction",
            {
                "channel": uri,
                "clientSeq": 1,
                "action": {
                    "type": "session/activeClientSet",
                    "activeClient": {
                        "clientId": "vscode",
                        "tools": [],
                        "customizations": [_plugin()],
                    },
                },
            },
        )
        await asyncio.sleep(0.1)
        await client.transport.close()
        await asyncio.sleep(0.4)
        assert len(host.outbound) == 0


class TestFileShapedPlugin:
    """A plugin whose `uri` is a file, not a container.

    The spec says clients publish "always container-shaped plugins". The only
    third-party AHP client in the wild does not: `ahpx` publishes one plugin per
    agent file, with the file's own URI, and answers `ENOTDIR` when a host tries
    to list it. Found by pointing it at this host.

    Being strict here would only make the feature not work, so the file is read
    as the plugin's single child — which is what the publication plainly means.
    """

    async def test_a_file_uri_yields_one_child_rather_than_none(self, host: Host) -> None:
        client = await _attach(host)
        uri = "echo:/plugins-file"
        await _session(host, client, uri)

        file_plugin = {
            "type": "plugin",
            "id": "agents/team-lead.md",
            "uri": "file:///repo/.github/agents/team-lead.md",
            "name": "Team Lead",
            "enabled": True,
            "nonce": "n1",
        }
        await client.notify(
            "dispatchAction",
            {
                "channel": uri,
                "clientSeq": 1,
                "action": {
                    "type": "session/activeClientSet",
                    "activeClient": {
                        "clientId": "ahpx",
                        "tools": [],
                        "customizations": [file_plugin],
                    },
                },
            },
        )
        # Answer the list with the same ENOTDIR the real client sends.
        await _serve_enotdir(client, seconds=0.8)

        state = (await client.request("subscribe", {"channel": uri}))["result"]["snapshot"]["state"]
        plugin = next(c for c in state["customizations"] if c["id"] == "agents/team-lead.md")
        assert len(plugin["children"]) == 1
        child = plugin["children"][0]
        assert child["type"] == "agent"
        # Named for the plugin, because the client already said what to call it.
        assert child["name"] == "Team Lead"


async def _serve_enotdir(client: ServingClient, *, seconds: float) -> None:
    """Answer every reverse request the way a real client answers a file."""
    deadline = asyncio.get_running_loop().time() + seconds
    while asyncio.get_running_loop().time() < deadline:
        remaining = deadline - asyncio.get_running_loop().time()
        try:
            message = await asyncio.wait_for(client.transport.receive(), timeout=remaining)
        except TimeoutError:
            return
        if message is None or "id" not in message or "method" not in message:
            if message is not None:
                client.notifications.append(message)
            continue
        await client.transport.send(
            {
                "jsonrpc": "2.0",
                "id": message["id"],
                "error": {"code": -32008, "message": "ENOTDIR: not a directory"},
            }
        )
