"""`authenticate.target` (c5628cd4): a token for one MCP server in one session.

"When present, the host MUST apply the token only to that MCP server in that
session, never to sibling servers, other sessions, or agent-level resources,
even if they share the resource and scopes. `resource` MUST still equal that
server's advertised `McpAuthRequirement.resource.resource`. The host MUST reject
an unknown session or customization, or a customization that is not an MCP
server, with `InvalidParams` (-32602)."

The rest is this host's reading of it: an unknown `kind` is refused (a consumer
we cannot name is one we cannot honour, and applying its token broadly is the
leak the field exists to prevent), and a session the pushing connection may not
see answers exactly like one that does not exist.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from ahp_protocol.channels import ROOT_URI

from ahp_host.core import Host, LoopbackSingleUserPolicy
from ahp_host.core.audit import AuditEvent
from ahp_host.provider.base import AgentSessionContext, AuthChallenge, TurnSink, UserMessage

from .test_host_end_to_end import FakeClient
from .test_mcp_and_stepup import (
    _OTHER_RESOURCE,
    _RESOURCE,
    McpProvider,
    McpSession,
    _challenge,
    _client,
    _session,
)

pytestmark = pytest.mark.anyio

SRV = "srv"
_RESOURCE_ID = str(_RESOURCE["resource"])
INVALID_PARAMS = -32602


class TargetSession(McpSession):
    """Parks on an auth challenge raised by the MCP server `SRV`, or by no server."""

    def __init__(self, context: AgentSessionContext, *, customization: str | None) -> None:
        super().__init__(context)
        self.customization = customization

    async def send_user_message(self, message: UserMessage, sink: TurnSink) -> None:
        await sink.tool_call_started("tool-1", "mcp_tool", {"q": message.text})
        await sink.request_authentication(
            "tool-1",
            AuthChallenge(resource=_RESOURCE, customization_id=self.customization),
        )
        self.authenticated = True
        await sink.tool_call_completed("tool-1", {"content": []})


class TargetProvider(McpProvider):
    """Advertises both resources statically, so what differs is the server's own."""

    def __init__(self, *, customization: str | None = SRV) -> None:
        super().__init__()
        self._info = type(self._info)(
            provider="echo",
            display_name="Echo",
            description="",
            models=(),
            protected_resources=(_RESOURCE, _OTHER_RESOURCE),
        )
        self.customization = customization
        self.by_uri: dict[str, TargetSession] = {}

    async def create_session(self, context: AgentSessionContext) -> TargetSession:
        session = TargetSession(context, customization=self.customization)
        self.by_uri[context.session_uri] = session
        return session


class Hiding(LoopbackSingleUserPolicy):
    """The connection may not see the channels in `hidden`."""

    def __init__(self) -> None:
        self.hidden: set[str] = set()

    def may_see_channel(self, info: Any, channel: str) -> bool:
        return channel not in self.hidden


class Recording:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def record(self, event: AuditEvent) -> None:
        self.events.append(event)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def world() -> AsyncIterator[tuple[Host, TargetProvider, Hiding, Recording, FakeClient]]:
    provider = TargetProvider()
    policy = Hiding()
    sink = Recording()
    host = Host(provider, policy, audit=sink)
    client = await _client(host)
    try:
        yield host, provider, policy, sink, client
    finally:
        await host.aclose()


async def _publish(provider: TargetProvider, uri: str, *, extra: bool = True) -> None:
    """The session's customizations: an MCP server that needs `_RESOURCE`, plus
    a plugin and an MCP server that has advertised nothing."""
    publisher = provider.by_uri[uri].context.publisher
    assert publisher is not None
    entries: list[dict[str, Any]] = [
        {
            "type": "mcpServer",
            "id": SRV,
            "name": SRV,
            "state": {"kind": "authRequired", "reason": "unauthorized", "resource": _RESOURCE},
        }
    ]
    if extra:
        entries += [
            {"type": "plugin", "id": "plug", "name": "plug"},
            {"type": "mcpServer", "id": "quiet", "name": "quiet", "state": {"kind": "ready"}},
        ]
    await publisher.customizations_changed(entries)
    # As a provider does when the server first asks: also surfaces `mcp:srv` in
    # the session's `inputNeeded`.
    await publisher.mcp_server_changed(SRV, entries[0]["state"])


async def _parked(
    host: Host, provider: TargetProvider, client: FakeClient, *uris: str
) -> list[str]:
    """Create the sessions, publish their servers, and park one tool call in each."""
    chats = []
    for uri in uris:
        chats.append(await _session(host, client, uri))
        await _publish(provider, uri)
    for chat in chats:
        await _challenge(client, chat)
    assert len(host.pending) == len(uris)
    return chats


def _target(session: str, customization_id: str = SRV, **extra: Any) -> dict[str, Any]:
    return {"kind": "mcpServer", "session": session, "customizationId": customization_id, **extra}


async def _push(
    client: FakeClient,
    *,
    target: Any = None,
    resource: str = _RESOURCE_ID,
    token: str = "fresh-token",
    **extra: Any,
) -> dict[str, Any]:
    params: dict[str, Any] = {"channel": ROOT_URI, "resource": resource, "token": token, **extra}
    if target is not None:
        params["target"] = target
    return await client.request("authenticate", params)


class TestScopedWake:
    async def test_a_targeted_push_wakes_only_the_targeted_servers_park(self, world: Any) -> None:
        host, provider, _policy, _sink, client = world
        await _parked(host, provider, client, "echo:/a", "echo:/b")
        resource = _RESOURCE["resource"]

        pushed = await _push(client, target=_target("echo:/a"))
        assert "error" not in pushed, pushed.get("error")
        await client.collect(seconds=0.4)

        assert provider.by_uri["echo:/a"].authenticated, "the targeted park never woke"
        assert not provider.by_uri["echo:/b"].authenticated, "a sibling session was woken"
        assert len(host.pending) == 1
        # And the credential is A's alone.
        assert host.tokens.get_for(resource, session="echo:/a", customization_id=SRV) is not None
        assert host.tokens.get_for(resource, session="echo:/b", customization_id=SRV) is None
        assert host.tokens.get(resource) is None, "nothing agent-level was granted"
        assert host.tokens.unsatisfied(host._protected_resources())

    async def test_b_is_woken_by_an_untargeted_grant_and_keeps_it_apart(self, world: Any) -> None:
        host, provider, _policy, _sink, client = world
        await _parked(host, provider, client, "echo:/a", "echo:/b")
        resource = _RESOURCE["resource"]
        await _push(client, target=_target("echo:/a"), token="for-a")
        await client.collect(seconds=0.4)

        await _push(client, token="for-everyone")
        await client.collect(seconds=0.4)

        assert provider.by_uri["echo:/b"].authenticated
        assert len(host.pending) == 0
        own = host.tokens.get_for(resource, session="echo:/a", customization_id=SRV)
        sibling = host.tokens.get_for(resource, session="echo:/b", customization_id=SRV)
        assert own is not None
        assert own.token.reveal() == "for-a"
        assert sibling is not None
        assert sibling.token.reveal() == "for-everyone"

    async def test_a_park_with_no_customization_is_woken_only_by_an_untargeted_push(self) -> None:
        provider = TargetProvider(customization=None)
        host = Host(provider, LoopbackSingleUserPolicy())
        try:
            client = await _client(host)
            await _parked(host, provider, client, "echo:/a")

            await _push(client, target=_target("echo:/a"))
            await client.collect(seconds=0.4)
            assert not provider.by_uri["echo:/a"].authenticated
            assert len(host.pending) == 1

            await _push(client)
            await client.collect(seconds=0.4)
            assert provider.by_uri["echo:/a"].authenticated
            assert len(host.pending) == 0
        finally:
            await host.aclose()

    async def test_a_park_for_another_server_of_the_same_session_stays_parked(
        self, world: Any
    ) -> None:
        host, provider, _policy, _sink, client = world
        provider.customization = "quiet"
        await _parked(host, provider, client, "echo:/a")

        await _push(client, target=_target("echo:/a", SRV))
        await client.collect(seconds=0.4)
        assert len(host.pending) == 1, "the token was applied to a sibling server"

    async def test_an_untargeted_push_still_wakes_every_matching_park(self, world: Any) -> None:
        host, provider, _policy, _sink, client = world
        await _parked(host, provider, client, "echo:/a", "echo:/b")

        pushed = await _push(client)
        assert "error" not in pushed
        await client.collect(seconds=0.4)

        assert provider.by_uri["echo:/a"].authenticated
        assert provider.by_uri["echo:/b"].authenticated
        assert len(host.pending) == 0
        granted = host.tokens.get(_RESOURCE["resource"])
        assert granted is not None
        assert granted.session is None

    async def test_the_servers_session_level_input_needed_is_left_to_the_provider(
        self, world: Any
    ) -> None:
        """What the untargeted path does today, kept: the park's own entry is
        withdrawn, but `mcp:<id>` -- the SERVER's challenge -- stays until the
        provider publishes the server out of `authRequired`."""
        host, provider, _policy, _sink, client = world
        await _parked(host, provider, client, "echo:/a")

        def entries() -> list[str]:
            state = host.sequencer.state_of("echo:/a") or {}
            return [e["id"] for e in state.get("inputNeeded") or []]

        assert f"mcp:{SRV}" in entries()
        before = len(entries())
        await _push(client, target=_target("echo:/a"))
        await client.collect(seconds=0.4)
        assert f"mcp:{SRV}" in entries()
        assert len(entries()) == before - 1, "only the woken park's entry went"

        publisher = provider.by_uri["echo:/a"].context.publisher
        assert publisher is not None
        await publisher.mcp_server_changed(SRV, {"kind": "ready"})
        assert f"mcp:{SRV}" not in entries()


class TestRefusals:
    async def _refused(self, client: FakeClient, **kw: Any) -> str:
        pushed = await _push(client, **kw)
        assert pushed.get("error", {}).get("code") == INVALID_PARAMS, pushed
        message = pushed["error"]["message"]
        assert isinstance(message, str)
        return message

    async def test_an_unknown_session(self, world: Any) -> None:
        host, provider, _policy, _sink, client = world
        await _parked(host, provider, client, "echo:/a")
        await self._refused(client, target=_target("echo:/nowhere"))
        assert host.tokens.targets() == ()

    async def test_a_session_the_connection_may_not_see_answers_like_an_unknown_one(
        self, world: Any
    ) -> None:
        host, provider, policy, _sink, client = world
        await _parked(host, provider, client, "echo:/a", "echo:/secret")
        policy.hidden.add("echo:/secret")

        unknown = await self._refused(client, target=_target("echo:/nowhere"))
        hidden = await self._refused(client, target=_target("echo:/secret"))
        assert hidden.replace("echo:/secret", "X") == unknown.replace("echo:/nowhere", "X")
        assert host.tokens.targets() == ()
        assert len(host.pending) == 2, "nothing was woken"

    async def test_an_unknown_customization(self, world: Any) -> None:
        host, provider, _policy, _sink, client = world
        await _parked(host, provider, client, "echo:/a")
        await self._refused(client, target=_target("echo:/a", "ghost"))

    async def test_a_customization_that_is_not_an_mcp_server(self, world: Any) -> None:
        host, provider, _policy, _sink, client = world
        await _parked(host, provider, client, "echo:/a")
        await self._refused(client, target=_target("echo:/a", "plug"))

    async def test_a_resource_other_than_the_servers_advertised_one(self, world: Any) -> None:
        host, provider, _policy, _sink, client = world
        await _parked(host, provider, client, "echo:/a")
        # Advertised by the agent -- the push would be accepted untargeted --
        # but it is not what THIS server asked for.
        await self._refused(client, target=_target("echo:/a"), resource=_OTHER_RESOURCE["resource"])
        assert host.tokens.targets() == ()

    async def test_a_server_that_never_advertised_a_resource(self, world: Any) -> None:
        host, provider, _policy, _sink, client = world
        await _parked(host, provider, client, "echo:/a")
        await self._refused(client, target=_target("echo:/a", "quiet"))

    @pytest.mark.parametrize(
        "target",
        [
            {"kind": "futureThing", "session": "echo:/a", "customizationId": SRV},
            {"kind": "futureThing"},
            {"session": "echo:/a", "customizationId": SRV},
            {"kind": 7, "session": "echo:/a", "customizationId": SRV},
            "echo:/a",
            ["mcpServer"],
            {"kind": "mcpServer", "customizationId": SRV},
            {"kind": "mcpServer", "session": "echo:/a"},
            {"kind": "mcpServer", "session": 7, "customizationId": SRV},
            {"kind": "mcpServer", "session": "echo:/a", "customizationId": ["srv"]},
        ],
        ids=lambda t: repr(t)[:40],
    )
    async def test_a_malformed_or_unknown_kind_of_target(self, world: Any, target: Any) -> None:
        """Never broadened to an untargeted push: that is the leak."""
        host, provider, _policy, _sink, client = world
        await _parked(host, provider, client, "echo:/a")
        await self._refused(client, target=target)
        assert host.tokens.get(_RESOURCE["resource"]) is None
        assert host.tokens.targets() == ()
        assert len(host.pending) == 1

    async def test_a_null_target_is_refused_like_any_other_present_one(self, world: Any) -> None:
        host, provider, _policy, _sink, client = world
        await _parked(host, provider, client, "echo:/a")
        pushed = await client.request(
            "authenticate",
            {
                "channel": ROOT_URI,
                "resource": _RESOURCE["resource"],
                "token": "x",
                "target": None,
            },
        )
        assert pushed.get("error", {}).get("code") == INVALID_PARAMS
        assert host.tokens.get(_RESOURCE["resource"]) is None

    async def test_the_existing_checks_still_apply_to_a_targeted_push(self, world: Any) -> None:
        host, provider, _policy, _sink, client = world
        await _parked(host, provider, client, "echo:/a")
        await self._refused(client, target=_target("echo:/a"), expiresIn=0)
        await self._refused(
            client, target=_target("echo:/a"), resource="https://unadvertised.example.invalid"
        )


class TestRevoke:
    async def test_a_targeted_revoke_leaves_the_untargeted_grant_alone(self, world: Any) -> None:
        host, provider, _policy, _sink, client = world
        await _parked(host, provider, client, "echo:/a")
        resource = _RESOURCE["resource"]
        await _push(client, token="agent-level")
        await _push(client, target=_target("echo:/a"), token="targeted")
        own = host.tokens.get_for(resource, session="echo:/a", customization_id=SRV)
        assert own is not None
        assert own.token.reveal() == "targeted"

        revoked = await _push(client, target=_target("echo:/a"), token="")
        assert "error" not in revoked, revoked.get("error")

        kept = host.tokens.get(resource)
        assert kept is not None
        assert kept.token.reveal() == "agent-level"
        fallback = host.tokens.get_for(resource, session="echo:/a", customization_id=SRV)
        assert fallback is kept
        assert host.tokens.targets() == ()

    async def test_an_untargeted_revoke_leaves_the_targeted_grant_alone(self, world: Any) -> None:
        host, provider, _policy, _sink, client = world
        await _parked(host, provider, client, "echo:/a")
        resource = _RESOURCE["resource"]
        await _push(client, token="agent-level")
        await _push(client, target=_target("echo:/a"), token="targeted")

        await _push(client, token="")

        assert host.tokens.get(resource) is None
        targeted = host.tokens.get_for(resource, session="echo:/a", customization_id=SRV)
        assert targeted is not None
        assert targeted.token.reveal() == "targeted"

    async def test_a_targeted_revoke_is_validated_like_a_targeted_push(self, world: Any) -> None:
        host, provider, _policy, _sink, client = world
        await _parked(host, provider, client, "echo:/a")
        for target in (_target("echo:/nowhere"), _target("echo:/a", "plug"), {"kind": "other"}):
            revoked = await _push(client, target=target, token="")
            assert revoked.get("error", {}).get("code") == INVALID_PARAMS


class TestExpiry:
    async def test_a_targeted_expiry_timer_does_not_touch_the_untargeted_grant(
        self, world: Any
    ) -> None:
        host, provider, _policy, _sink, client = world
        await _parked(host, provider, client, "echo:/a")
        resource = _RESOURCE["resource"]
        await _push(client, token="agent-level", expiresIn=3600)
        await _push(client, target=_target("echo:/a"), token="targeted", expiresIn=1)
        assert len(host._token_expiry) == 2, "one timer per grant"

        await asyncio.sleep(1.4)

        assert host.tokens.get_for(resource, session="echo:/a", customization_id=SRV) is not None
        # Expired grants read as absent, so look at the fall-back: it is A's
        # untargeted token that answers now, not the dead targeted one.
        remaining = host.tokens.get_for(resource, session="echo:/a", customization_id=SRV)
        assert remaining is not None
        assert remaining.token.reveal() == "agent-level"
        assert host.tokens.get(resource) is not None
        assert len(host._token_expiry) == 1

    async def test_a_targeted_push_does_not_cancel_the_untargeted_timer(self, world: Any) -> None:
        host, provider, _policy, _sink, client = world
        await _parked(host, provider, client, "echo:/a")
        resource = _RESOURCE["resource"]
        await _push(client, token="agent-level", expiresIn=1)
        await _push(client, target=_target("echo:/a"), token="targeted")  # no expiry, no timer

        await asyncio.sleep(1.4)

        assert host.tokens.get(resource) is None, "the untargeted timer still fired"
        targeted = host.tokens.get_for(resource, session="echo:/a", customization_id=SRV)
        assert targeted is not None
        assert targeted.token.reveal() == "targeted"


class TestAudit:
    async def test_the_detail_names_the_target_and_never_the_token(self, world: Any) -> None:
        host, provider, _policy, sink, client = world
        await _parked(host, provider, client, "echo:/a")
        await _push(client, target=_target("echo:/a"), token="s3cret-value")
        await _push(client, target=_target("echo:/a"), token="")
        await _push(client, token="s3cret-agent")

        accepted = [e for e in sink.events if e.kind == "auth.accepted"]
        revoked = [e for e in sink.events if e.kind == "auth.revoked"]
        expected = {"session": "echo:/a", "customizationId": SRV}
        assert accepted[0].detail["target"] == expected
        assert revoked[0].detail["target"] == expected
        assert "target" not in accepted[1].detail, "an untargeted push audits as before"
        assert "s3cret" not in repr([e.detail for e in sink.events])

    async def test_a_refused_target_is_audited_without_the_token(self, world: Any) -> None:
        host, provider, _policy, sink, client = world
        await _parked(host, provider, client, "echo:/a")
        await _push(client, target=_target("echo:/nowhere"), token="s3cret-value")
        refused = [e for e in sink.events if e.kind == "auth.refused"]
        assert refused
        assert refused[-1].allowed is False
        assert refused[-1].detail["target"] == {"session": "echo:/nowhere", "customizationId": SRV}
        assert "s3cret" not in repr([e.detail for e in sink.events])
