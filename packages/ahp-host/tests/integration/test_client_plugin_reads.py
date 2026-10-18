"""`ReadsClientPlugins`: a provider reads the plugins published into its session."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from ahp_protocol import errors
from ahp_protocol.channels import AUTOMATIONS_URI

from ahp_host.core import Host, InMemoryAutomationStore, LoopbackSingleUserPolicy
from ahp_host.core.plugin_copies import COPY_SCHEME
from ahp_host.provider import AgentSessionContext, EchoProvider
from ahp_host.provider.base import ReadsClientPlugins

from .test_automation_customizations import (
    _PLUGIN,
    AUTOMATION,
    _client,
    _create,
    _definition,
    _plugin,
)

pytestmark = pytest.mark.anyio

SESSION = "ahp-session:/55555555-5555-5555-5555-555555555555"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class ReaderProvider(EchoProvider):
    """An echo agent that remembers each session's publisher."""

    def __init__(self) -> None:
        super().__init__(provider_id="reader")
        self.publishers: dict[str, Any] = {}

    async def create_session(self, context: AgentSessionContext) -> Any:
        self.publishers[context.session_uri] = context.publisher
        return await super().create_session(context)


async def test_client_plugin_reads_live() -> None:
    reader = ReaderProvider()
    host = Host(reader, LoopbackSingleUserPolicy())
    try:
        client, _ = await _client(host)
        await client.request("createSession", {"channel": SESSION, "provider": "reader"})
        await client.notify(
            "dispatchAction",
            {
                "channel": SESSION,
                "clientSeq": 1,
                "action": {
                    "type": "session/activeClientSet",
                    "activeClient": {"clientId": "c1", "tools": [], "customizations": [_plugin()]},
                },
            },
        )
        await client.serve(seconds=0.6)
        publisher = reader.publishers[SESSION]
        assert isinstance(publisher, ReadsClientPlugins)
        assert list(publisher.session_plugins()) == [
            {"id": "house", "uri": _PLUGIN, "name": "House rules", "nonce": "n1", "clientId": "c1"}
        ]

        async def reads() -> tuple[set[str], bytes, int]:
            listed = await publisher.list_plugin_resource(_PLUGIN)
            data = await publisher.read_plugin_resource(f"{_PLUGIN}/review.prompt.md")
            with pytest.raises(errors.AhpError) as raised:
                await publisher.read_plugin_resource("virtual://elsewhere/x")
            return {e["name"] for e in listed}, data, raised.value.code

        (names, data, code), _ = await asyncio.gather(reads(), client.serve(seconds=1.0))
        assert {"review.prompt.md", "skills"} <= names
        assert data == b"# Review Prompt\nbody"
        assert code == -32008
    finally:
        await host.aclose()


async def test_client_plugin_reads_copy() -> None:
    reader = ReaderProvider()
    host = Host(
        [EchoProvider(), reader],
        LoopbackSingleUserPolicy(),
        automations=InMemoryAutomationStore(),
    )
    try:
        client, _ = await _client(host)
        await _create(client, _definition(provider="reader", customizations=[_plugin()]))
        reply = await client.request(
            "runAutomation",
            {"channel": AUTOMATIONS_URI, "automation": AUTOMATION, "requestId": "r1"},
        )
        run = reply["result"]["resource"]
        deadline = asyncio.get_running_loop().time() + 5
        uri: str | None = None
        while asyncio.get_running_loop().time() < deadline:
            sessions = (host.sequencer.state_of(run) or {}).get("sessions") or []
            if sessions and sessions[0] in reader.publishers:
                state = host.sequencer.state_of(sessions[0]) or {}
                if any(c.get("id") == "house" for c in state.get("customizations") or []):
                    uri = sessions[0]
                    break
            await asyncio.sleep(0.02)
        assert uri is not None
        publisher = reader.publishers[uri]
        (plugin,) = publisher.session_plugins()
        assert plugin["uri"].startswith(COPY_SCHEME)
        assert "clientId" not in plugin

        await client.transport.close()
        await asyncio.sleep(0.1)
        assert await publisher.read_plugin_resource(f"{plugin['uri']}/review.prompt.md") == (
            b"# Review Prompt\nbody"
        )
        names = {e["name"] for e in await publisher.list_plugin_resource(plugin["uri"])}
        assert "skills" in names
    finally:
        await host.aclose()
