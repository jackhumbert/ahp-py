"""Root and session configuration, and the gate on changing it.

`root/configChanged` is client-dispatchable and VS Code sends it about ten times
per connection. Every one was previously dropped -- not by design, but because
`RootState.config` was absent and the reducer's own guard discarded it.
Publishing a schema turns that accident into a feature in one commit, so the
gate lands with the feature rather than after it.

The gate is the **schema**, not the policy. A permissive policy is the norm for
a loopback host, so anything that only a careful policy would catch is not
caught.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from agent_host_protocol.channels import ROOT_URI
from agent_host_protocol.transport import memory_pair

from agent_host_server.core import Host, LoopbackSingleUserPolicy
from agent_host_server.core.config import RootConfig
from agent_host_server.provider import EchoProvider

from .test_host_end_to_end import FakeClient

pytestmark = pytest.mark.anyio

_SCHEMA: dict[str, dict[str, Any]] = {
    "theme": {"type": "string", "title": "Theme", "enum": ["light", "dark"]},
    "verbose": {"type": "boolean", "title": "Verbose"},
    "build": {"type": "string", "title": "Build", "readOnly": True},
}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def _attach(host: Host, client_id: str = "c1") -> FakeClient:
    client_transport, server_transport = memory_pair()
    task = asyncio.create_task(host.serve(server_transport))
    client = FakeClient(client_transport)
    client.serve_task = task  # type: ignore[attr-defined]
    await client.request(
        "initialize",
        {
            "channel": ROOT_URI,
            "clientId": client_id,
            "protocolVersions": ["0.7.0"],
            "initialSubscriptions": [ROOT_URI],
        },
    )
    return client


async def _set_root_config(client: FakeClient, config: dict[str, Any], **extra: Any) -> None:
    await client.notify(
        "dispatchAction",
        {
            "channel": ROOT_URI,
            "clientSeq": 1,
            "action": {"type": "root/configChanged", "config": config, **extra},
        },
    )
    await client.collect(seconds=0.3)


def _rejection(client: FakeClient, action_type: str) -> str | None:
    for envelope in reversed(client.actions()):
        if envelope["action"]["type"] == action_type:
            reason: str | None = envelope.get("rejectionReason")
            return reason
    return None


class TestRootConfig:
    @pytest.fixture
    async def configured(self) -> AsyncIterator[Host]:
        host = Host(
            EchoProvider(),
            LoopbackSingleUserPolicy(),
            root_config=RootConfig(properties=_SCHEMA, values={"theme": "light"}),
        )
        try:
            yield host
        finally:
            await host.aclose()

    async def test_the_schema_is_published_on_root(self, configured: Host) -> None:
        client = await _attach(configured)
        result = (await client.request("subscribe", {"channel": ROOT_URI}))["result"]
        config = result["snapshot"]["state"]["config"]
        assert config["schema"]["properties"]["theme"]["enum"] == ["light", "dark"]
        assert config["values"] == {"theme": "light"}

    async def test_a_known_key_is_accepted_and_merged(self, configured: Host) -> None:
        client = await _attach(configured)
        await _set_root_config(client, {"verbose": True})
        state = (await client.request("subscribe", {"channel": ROOT_URI}))["result"]["snapshot"][
            "state"
        ]
        assert state["config"]["values"] == {"theme": "light", "verbose": True}

    async def test_an_unknown_key_is_rejected(self, configured: Host) -> None:
        """The schema is the gate. This must hold with a fully permissive
        policy, because that is what a loopback host ships with."""
        client = await _attach(configured)
        await _set_root_config(client, {"mcpServers": {"evil": {"command": "sh"}}})
        assert _rejection(client, "root/configChanged") is not None
        state = (await client.request("subscribe", {"channel": ROOT_URI}))["result"]["snapshot"][
            "state"
        ]
        assert "mcpServers" not in state["config"]["values"]

    async def test_a_wrong_type_is_rejected(self, configured: Host) -> None:
        client = await _attach(configured)
        await _set_root_config(client, {"verbose": "yes please"})
        assert _rejection(client, "root/configChanged") is not None

    async def test_a_value_outside_the_enum_is_rejected(self, configured: Host) -> None:
        client = await _attach(configured)
        await _set_root_config(client, {"theme": "chartreuse"})
        assert _rejection(client, "root/configChanged") is not None

    async def test_a_read_only_property_is_rejected(self, configured: Host) -> None:
        client = await _attach(configured)
        await _set_root_config(client, {"build": "tampered"})
        assert _rejection(client, "root/configChanged") is not None

    async def test_replace_is_refused(self, configured: Host) -> None:
        """`replace: true` drops every key the action omits, including ones this
        peer could not have set. A merge expresses every legitimate intent."""
        client = await _attach(configured)
        await _set_root_config(client, {"theme": "dark"}, replace=True)
        assert _rejection(client, "root/configChanged") is not None

    async def test_a_policy_can_refuse_a_key_the_schema_allows(self) -> None:
        class NoDarkMode(LoopbackSingleUserPolicy):
            def may_set_root_config(self, info: Any, key: str, value: Any) -> bool:
                return not (key == "theme" and value == "dark")

        host = Host(EchoProvider(), NoDarkMode(), root_config=RootConfig(properties=_SCHEMA))
        try:
            client = await _attach(host)
            await _set_root_config(client, {"theme": "dark"})
            assert _rejection(client, "root/configChanged") is not None
            await _set_root_config(client, {"theme": "light"})
            state = (await client.request("subscribe", {"channel": ROOT_URI}))["result"][
                "snapshot"
            ]["state"]
            assert state["config"]["values"]["theme"] == "light"
        finally:
            await host.aclose()

    async def test_a_host_without_a_schema_publishes_no_config(self) -> None:
        """The status quo, but on purpose: with no schema nothing is
        configurable, and saying so beats the reducer silently dropping it."""
        host = Host(EchoProvider(), LoopbackSingleUserPolicy())
        try:
            client = await _attach(host)
            state = (await client.request("subscribe", {"channel": ROOT_URI}))["result"][
                "snapshot"
            ]["state"]
            assert "config" not in state
            await _set_root_config(client, {"anything": 1})
            assert _rejection(client, "root/configChanged") is not None
        finally:
            await host.aclose()


class TestSessionConfig:
    @pytest.fixture
    async def host(self) -> AsyncIterator[Host]:
        host = Host(EchoProvider(configurable=True), LoopbackSingleUserPolicy())
        try:
            yield host
        finally:
            await host.aclose()

    async def test_resolve_returns_the_full_property_set(self, host: Host) -> None:
        client = await _attach(host)
        result = (await client.request("resolveSessionConfig", {"channel": ROOT_URI}))["result"]
        assert set(result["schema"]["properties"]) == {"style", "prefix"}
        assert result["values"]["style"] == "plain"

    async def test_resolve_is_contextual_to_what_is_already_chosen(self, host: Host) -> None:
        """ "Each response returns the full current property set (not a delta)."
        A property that only makes sense given a prior choice appears once that
        choice is made -- which is why this is a command and not a static
        schema."""
        client = await _attach(host)
        result = (
            await client.request(
                "resolveSessionConfig", {"channel": ROOT_URI, "config": {"style": "shout"}}
            )
        )["result"]
        assert "greeting" in result["schema"]["properties"]

    async def test_completions_filter_by_the_query(self, host: Host) -> None:
        client = await _attach(host)
        result = (
            await client.request(
                "sessionConfigCompletions",
                {"channel": ROOT_URI, "property": "greeting", "query": "h"},
            )
        )["result"]
        assert [item["value"] for item in result["items"]] == ["HELLO", "HI THERE"]

    async def test_a_provider_with_no_config_answers_an_empty_schema(self) -> None:
        """Not MethodNotFound: "nothing to configure" is a real answer, and a
        client cannot tell a refusal from a broken host."""
        host = Host(EchoProvider(), LoopbackSingleUserPolicy())
        try:
            client = await _attach(host)
            result = (await client.request("resolveSessionConfig", {"channel": ROOT_URI}))["result"]
            assert result == {"schema": {"type": "object", "properties": {}}, "values": {}}
            empty = (
                await client.request(
                    "sessionConfigCompletions", {"channel": ROOT_URI, "property": "x"}
                )
            )["result"]
            assert empty == {"items": []}
        finally:
            await host.aclose()

    async def test_create_session_seeds_the_config_into_state(self, host: Host) -> None:
        """The schema must be in the INITIAL state: `session/configChanged`
        carries values only, and the reducer no-ops when `config` is absent, so
        a schema not seeded here can never be added."""
        client = await _attach(host)
        uri = "echo:/cfg-1"
        await client.request(
            "createSession",
            {"channel": uri, "provider": "echo", "config": {"style": "shout"}},
        )
        await client.collect(seconds=0.3)
        state = (await client.request("subscribe", {"channel": uri}))["result"]["snapshot"]["state"]
        assert state["config"]["values"]["style"] == "shout"
        assert "prefix" in state["config"]["schema"]["properties"]

    async def test_the_chosen_config_reaches_the_agent(self, host: Host) -> None:
        client = await _attach(host)
        uri = "echo:/cfg-2"
        await client.request(
            "createSession",
            {"channel": uri, "provider": "echo", "config": {"style": "shout"}},
        )
        await client.collect(seconds=0.3)
        state = (await client.request("subscribe", {"channel": uri}))["result"]["snapshot"]["state"]
        chat_uri = state["chats"][0]["resource"]
        await client.request("subscribe", {"channel": chat_uri})
        await client.notify(
            "dispatchAction",
            {
                "channel": chat_uri,
                "clientSeq": 1,
                "action": {
                    "type": "chat/turnStarted",
                    "turnId": "t1",
                    "startedAt": "1970-01-01T00:00:01.000Z",
                    "message": {"text": "hello", "origin": {"kind": "user"}},
                },
            },
        )
        await client.collect(seconds=0.5)
        deltas = "".join(
            e["action"].get("content", "")
            for e in client.actions(chat_uri)
            if e["action"]["type"] == "chat/delta"
        )
        assert "HELLO" in deltas, "the configuration was published but not honoured"

    async def test_only_a_session_mutable_property_may_change_afterwards(self, host: Host) -> None:
        """`sessionMutable` is the protocol's own marker. Without it a property
        is a creation-time choice, and changing it later would leave the agent
        configured one way and the state claiming another."""
        client = await _attach(host)
        uri = "echo:/cfg-3"
        await client.request("createSession", {"channel": uri, "provider": "echo"})
        await client.collect(seconds=0.3)
        await client.request("subscribe", {"channel": uri})

        for key, value in (("prefix", "Heard:"), ("style", "shout")):
            await client.notify(
                "dispatchAction",
                {
                    "channel": uri,
                    "clientSeq": 1,
                    "action": {"type": "session/configChanged", "config": {key: value}},
                },
            )
        await client.collect(seconds=0.4)

        echoes = [e for e in client.actions(uri) if e["action"]["type"] == "session/configChanged"]
        assert "rejectionReason" not in echoes[0], "a sessionMutable property was refused"
        assert "rejectionReason" in echoes[1], "a creation-time property was changed after the fact"

        state = (await client.request("subscribe", {"channel": uri}))["result"]["snapshot"]["state"]
        assert state["config"]["values"]["prefix"] == "Heard:"
        assert state["config"]["values"].get("style") != "shout"


class TestCustomizationsAndProgress:
    """Session state that changes while nobody is taking a turn."""

    async def test_a_toggle_reaches_the_agent(self) -> None:
        """The reducer applies `enablement` in state on its own, so clients agree
        without any host code. What they cannot do is stop the AGENT using a
        disabled skill -- only the provider can, and only if it is told."""
        host = Host(EchoProvider(customizations=True), LoopbackSingleUserPolicy())
        try:
            client = await _attach(host)
            uri = "echo:/toggle-1"
            await client.request("createSession", {"channel": uri, "provider": "echo"})
            await client.collect(seconds=0.3)
            await client.request("subscribe", {"channel": uri})

            await client.notify(
                "dispatchAction",
                {
                    "channel": uri,
                    "clientSeq": 1,
                    "action": {
                        "type": "session/customizationToggled",
                        "id": "ahs-skill-hotel",
                        "enablement": [{"kind": "session", "enabled": False}],
                    },
                },
            )
            await client.collect(seconds=0.4)

            agent = host._sessions[uri].agent_session
            assert getattr(agent, "toggled", {}) == {"ahs-skill-hotel": False}
        finally:
            await host.aclose()

    async def test_a_pre_0_8_toggle_is_rejected_and_never_reaches_the_agent(self) -> None:
        """`enabled` was the 0.7.0 shape. The 0.8.0 reducer no-ops on it, so
        accepting it would leave the client's optimistic toggle applied and --
        worse -- tell the provider `False` for an action that said nothing."""
        host = Host(EchoProvider(customizations=True), LoopbackSingleUserPolicy())
        try:
            client = await _attach(host)
            uri = "echo:/toggle-2"
            await client.request("createSession", {"channel": uri, "provider": "echo"})
            await client.collect(seconds=0.3)
            await client.request("subscribe", {"channel": uri})

            await client.notify(
                "dispatchAction",
                {
                    "channel": uri,
                    "clientSeq": 1,
                    "action": {
                        "type": "session/customizationToggled",
                        "id": "ahs-skill-hotel",
                        "enabled": False,
                    },
                },
            )
            await client.collect(seconds=0.4)
            echoes = [
                e
                for e in client.actions(uri)
                if e["action"]["type"] == "session/customizationToggled"
            ]
            assert echoes
            assert "rejectionReason" in echoes[0]
            agent = host._sessions[uri].agent_session
            assert getattr(agent, "toggled", {}) == {}
        finally:
            await host.aclose()

    async def test_a_provider_can_republish_customizations_out_of_turn(self) -> None:
        host = Host(EchoProvider(), LoopbackSingleUserPolicy())
        try:
            client = await _attach(host)
            uri = "echo:/publish-1"
            await client.request("createSession", {"channel": uri, "provider": "echo"})
            await client.collect(seconds=0.3)
            await client.request("subscribe", {"channel": uri})

            publisher = host._sessions[uri].publisher
            assert publisher is not None
            await publisher.customizations_changed(
                [{"type": "plugin", "id": "late", "name": "Installed later"}]
            )
            await publisher.activity_changed("indexing")
            await client.collect(seconds=0.3)

            state = (await client.request("subscribe", {"channel": uri}))["result"]["snapshot"][
                "state"
            ]
            assert [c["id"] for c in state["customizations"]] == ["late"]
            assert state["activity"] == "indexing"
        finally:
            await host.aclose()

    async def test_progress_is_correlated_to_the_clients_token(self) -> None:
        host = Host(EchoProvider(), LoopbackSingleUserPolicy())
        try:
            client = await _attach(host)
            uri = "echo:/progress-1"
            await client.request(
                "createSession",
                {"channel": uri, "provider": "echo", "progressToken": "tok-7"},
            )
            await client.collect(seconds=0.3)
            publisher = host._sessions[uri].publisher
            assert publisher is not None
            await publisher.progress(3, total=10, message="cloning")
            await client.collect(seconds=0.3)

            frames = [
                n["params"] for n in client.notifications if n.get("method") == "root/progress"
            ]
            assert frames, "progress was never delivered"
            assert frames[-1]["progressToken"] == "tok-7"
            assert frames[-1]["progress"] == 3
            assert frames[-1]["total"] == 10
        finally:
            await host.aclose()

    async def test_progress_without_a_token_is_a_silent_no_op(self) -> None:
        """Most clients send no token. A provider should not have to check."""
        host = Host(EchoProvider(), LoopbackSingleUserPolicy())
        try:
            client = await _attach(host)
            uri = "echo:/progress-2"
            await client.request("createSession", {"channel": uri, "provider": "echo"})
            await client.collect(seconds=0.3)
            publisher = host._sessions[uri].publisher
            assert publisher is not None
            await publisher.progress(1)
            await client.collect(seconds=0.2)
            assert not [n for n in client.notifications if n.get("method") == "root/progress"]
        finally:
            await host.aclose()
