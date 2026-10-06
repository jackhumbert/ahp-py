"""A provider replacing its customization tree keeps everyone else's state.

`session/customizationsChanged` replaces `SessionState.customizations` whole,
and that one list also holds what is not the provider's: a client's published
plugin, which the host expands into it, and the user's on/off switches, which
`session/customizationToggled` writes onto its entries. A provider republishing
its own tree -- a plugin installed, an MCP server added -- erased both: the
client's plugin vanished until it republished with a new nonce, and every
switch snapped back on in state after the agent had been told it was off.
"""

from __future__ import annotations

from typing import Any

import pytest

from ahp_host.core import Host, LoopbackSingleUserPolicy
from ahp_host.provider import EchoProvider, EchoSession
from ahp_host.provider.base import AgentSessionContext, SessionPublisher

from .hosting import connect, open_session, shut, state

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class Holding(EchoProvider):
    def __init__(self) -> None:
        super().__init__()
        self.sessions: list[EchoSession] = []

    async def create_session(self, context: AgentSessionContext) -> EchoSession:
        session = EchoSession(context)
        self.sessions.append(session)
        return session

    @property
    def publisher(self) -> SessionPublisher:
        publisher = self.sessions[0].context.publisher
        assert publisher is not None
        return publisher


def _skill(identifier: str, **extra: Any) -> dict[str, Any]:
    return {
        "type": "skill",
        "id": identifier,
        "uri": f"file:///agent/{identifier}/SKILL.md",
        "name": identifier,
        **extra,
    }


def _plugin(*children: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {
        "type": "plugin",
        "id": "agent-plugin",
        "uri": "file:///agent/plugin",
        "name": "Agent plugin",
        "children": list(children),
        **extra,
    }


def _by_id(host: Host, uri: str) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for entry in state(host, uri).get("customizations") or []:
        found[entry["id"]] = entry
        for child in entry.get("children") or []:
            found[child["id"]] = child
    return found


async def test_a_replacement_keeps_client_plugins_and_the_users_switches() -> None:
    provider = Holding()
    host = Host(provider, LoopbackSingleUserPolicy())
    wire, serving = await connect(host)
    uri = "echo:/cust-1"
    try:
        await open_session(wire, uri)
        await provider.publisher.customizations_changed([_plugin(_skill("s1"))])

        # The client publishes a plugin of its own; the host expands it in.
        client_plugin = {
            "type": "plugin",
            "id": "client-plugin",
            "uri": "file:///client/helper.md",
            "name": "Client helper",
            "nonce": "n1",
        }
        await wire.dispatch(
            uri,
            {
                "type": "session/activeClientSet",
                "activeClient": {"clientId": "c1", "tools": [], "customizations": [client_plugin]},
            },
        )
        assert await wire.until(lambda: "client-plugin" in _by_id(host, uri))

        # The user switches the provider's plugin off for this session, and
        # its skill off globally.
        off = [{"kind": "session", "enabled": False}]
        await wire.dispatch(
            uri, {"type": "session/customizationToggled", "id": "agent-plugin", "enablement": off}
        )
        await wire.dispatch(
            uri,
            {
                "type": "session/customizationToggled",
                "id": "s1",
                "enablement": [{"kind": "global", "enabled": False}],
            },
        )
        assert await wire.until(lambda: _by_id(host, uri)["s1"].get("enabled") is False)

        # The provider republishes its tree, knowing nothing of either.
        await provider.publisher.customizations_changed([_plugin(_skill("s1"), _skill("s2"))])
        entries = _by_id(host, uri)
        assert "client-plugin" in entries, "the client's plugin was wiped"
        assert entries["agent-plugin"]["enablement"] == off
        assert entries["s1"]["enabled"] is False
        assert "enabled" not in entries["s2"]
        # Provider first, kept entries after.
        top = [c["id"] for c in state(host, uri)["customizations"]]
        assert top == ["agent-plugin", "client-plugin"]
    finally:
        await shut(host, wire, serving)


async def test_a_field_the_provider_states_wins() -> None:
    """The provider may know better -- a setting changed somewhere else."""
    provider = Holding()
    host = Host(provider, LoopbackSingleUserPolicy())
    wire, serving = await connect(host)
    uri = "echo:/cust-2"
    try:
        await open_session(wire, uri)
        await provider.publisher.customizations_changed([_plugin(_skill("s1"))])
        await wire.dispatch(
            uri,
            {
                "type": "session/customizationToggled",
                "id": "s1",
                "enablement": [{"kind": "global", "enabled": False}],
            },
        )
        assert await wire.until(lambda: _by_id(host, uri)["s1"].get("enabled") is False)
        await provider.publisher.customizations_changed(
            [_plugin(_skill("s1", enabled=True), enablement=[])]
        )
        entries = _by_id(host, uri)
        assert entries["s1"]["enabled"] is True
        # An explicit empty list is a statement ("no decision"), not an absence.
        assert entries["agent-plugin"]["enablement"] == []
    finally:
        await shut(host, wire, serving)


async def test_entries_the_provider_drops_are_dropped() -> None:
    """Only what is not the provider's is kept: its own removals still apply."""
    provider = Holding()
    host = Host(provider, LoopbackSingleUserPolicy())
    wire, serving = await connect(host)
    uri = "echo:/cust-3"
    try:
        await open_session(wire, uri)
        other = {**_plugin(), "id": "second", "uri": "file:///agent/second"}
        await provider.publisher.customizations_changed([_plugin(), other])
        await provider.publisher.customizations_changed([_plugin()])
        assert [c["id"] for c in state(host, uri)["customizations"]] == ["agent-plugin"]
    finally:
        await shut(host, wire, serving)


async def test_a_client_plugin_survives_even_once_the_host_forgot_it_contributed_it() -> None:
    """After a restart the host's own record is gone; an active client still
    publishing the plugin is evidence enough."""
    provider = Holding()
    host = Host(provider, LoopbackSingleUserPolicy())
    wire, serving = await connect(host)
    uri = "echo:/cust-4"
    try:
        await open_session(wire, uri)
        plugin = {"type": "plugin", "id": "cp", "uri": "file:///client/cp.md", "name": "CP"}
        await wire.dispatch(
            uri,
            {
                "type": "session/activeClientSet",
                "activeClient": {"clientId": "c1", "tools": [], "customizations": [plugin]},
            },
        )
        assert await wire.until(lambda: "cp" in _by_id(host, uri))
        host._sessions[uri].contributed_customizations.clear()
        await provider.publisher.customizations_changed([_plugin()])
        assert "cp" in _by_id(host, uri)
    finally:
        await shut(host, wire, serving)
