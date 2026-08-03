"""MCP server state, and 0.6.0 step-up authentication.

Two things that share a word and are otherwise unrelated.

**MCP.** The host publishes an MCP server's *state* and routes a client's
start/stop request to whoever owns the runtime. It never owns the runtime
itself: spawning a process, speaking stdio, `tools/list` and restart-on-crash
live in the agent harness, and a host-side MCP client would be process execution
smuggled in behind a customization.

**Step-up.** A tool call that got partway through and was told "not with that
token". The distinguishing feature against every other suspended request here is
that the resolution arrives as a **command** (`authenticate`), not through
`dispatchAction` — which the registry does not care about, and that is the point
of having one.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from agent_host_protocol.channels import ROOT_URI
from agent_host_protocol.transport import memory_pair
from agent_host_protocol.types.protocol import SessionStatus

from agent_host_server.core import Host, LoopbackSingleUserPolicy
from agent_host_server.provider import EchoProvider
from agent_host_server.provider.base import (
    AgentSessionContext,
    AuthChallenge,
    TurnSink,
    UserMessage,
)
from agent_host_server.provider.echo import EchoSession

from .test_host_end_to_end import FakeClient

pytestmark = pytest.mark.anyio

_RESOURCE = {
    "resource": "https://mcp.example.invalid",
    "authorization_servers": ["https://auth.example.invalid"],
}


class McpSession(EchoSession):
    """An echo session that also fronts one MCP server, and pauses on auth."""

    def __init__(self, context: AgentSessionContext, **kw: Any) -> None:
        super().__init__(context, **kw)
        self.started: list[str] = []
        self.stopped: list[str] = []
        self.authenticated = False

    async def start_mcp_server(self, customization_id: str) -> None:
        self.started.append(customization_id)
        if self.context.publisher is not None:
            await self.context.publisher.mcp_server_changed(customization_id, {"kind": "ready"})

    async def stop_mcp_server(self, customization_id: str) -> None:
        self.stopped.append(customization_id)
        if self.context.publisher is not None:
            await self.context.publisher.mcp_server_changed(customization_id, {"kind": "stopped"})

    async def send_user_message(self, message: UserMessage, sink: TurnSink) -> None:
        call_id = "tool-1"
        await sink.tool_call_started(call_id, "mcp_tool", {"q": message.text})
        # The upstream service said no. The turn stays open while a human goes
        # and gets a token.
        await sink.request_authentication(
            call_id,
            AuthChallenge(
                resource=_RESOURCE, reason="insufficientScope", required_scopes=["read:all"]
            ),
        )
        self.authenticated = True
        await sink.tool_call_completed(call_id, {"content": []})
        await sink.text_delta("done after auth")


class McpProvider(EchoProvider):
    def __init__(self) -> None:
        super().__init__()
        self._info = type(self._info)(
            provider="echo",
            display_name="Echo",
            description="",
            models=(),
            protected_resources=(_RESOURCE,),
        )
        self.session: McpSession | None = None

    async def create_session(self, context: AgentSessionContext) -> McpSession:
        self.session = McpSession(context)
        return self.session


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def wired() -> AsyncIterator[tuple[Host, McpProvider]]:
    provider = McpProvider()
    host = Host(provider, LoopbackSingleUserPolicy())
    try:
        yield host, provider
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


async def _session(host: Host, client: FakeClient, uri: str) -> str:
    await client.request("createSession", {"channel": uri, "provider": "echo"})
    await client.collect(seconds=0.3)
    state = (await client.request("subscribe", {"channel": uri}))["result"]["snapshot"]["state"]
    chat: str = state["chats"][0]["resource"]
    await client.request("subscribe", {"channel": chat})
    return chat


class TestMcpLifecycle:
    async def test_a_start_request_reaches_the_provider(
        self, wired: tuple[Host, McpProvider]
    ) -> None:
        """The reducer already moves the customization to `starting`, so clients
        agree without this. What it cannot do is start an actual server."""
        host, provider = wired
        client = await _client(host)
        uri = "echo:/mcp-1"
        await _session(host, client, uri)

        await client.notify(
            "dispatchAction",
            {
                "channel": uri,
                "clientSeq": 1,
                "action": {"type": "session/mcpServerStartRequested", "id": "srv-1"},
            },
        )
        await client.collect(seconds=0.4)
        assert provider.session is not None
        assert provider.session.started == ["srv-1"]

    async def test_a_stop_request_reaches_the_provider(
        self, wired: tuple[Host, McpProvider]
    ) -> None:
        host, provider = wired
        client = await _client(host)
        uri = "echo:/mcp-2"
        await _session(host, client, uri)
        await client.notify(
            "dispatchAction",
            {
                "channel": uri,
                "clientSeq": 1,
                "action": {"type": "session/mcpServerStopRequested", "id": "srv-1"},
            },
        )
        await client.collect(seconds=0.4)
        assert provider.session is not None
        assert provider.session.stopped == ["srv-1"]

    async def test_the_provider_publishes_server_state(
        self, wired: tuple[Host, McpProvider]
    ) -> None:
        host, provider = wired
        client = await _client(host)
        uri = "echo:/mcp-3"
        await _session(host, client, uri)
        await client.request("subscribe", {"channel": uri})

        assert provider.session is not None
        publisher = provider.session.context.publisher
        assert publisher is not None
        await publisher.mcp_server_changed("srv-1", {"kind": "ready"}, channel="mcp://srv-1")
        await client.collect(seconds=0.3)

        published = [
            e["action"]
            for e in client.actions(uri)
            if e["action"]["type"] == "session/mcpServerStateChanged"
        ]
        assert published[-1]["state"] == {"kind": "ready"}
        assert published[-1]["channel"] == "mcp://srv-1"

    async def test_an_auth_required_server_surfaces_at_the_session_level(
        self, wired: tuple[Host, McpProvider]
    ) -> None:
        """Otherwise the only sign is a customization badge nobody is looking at.
        The spec pairs this transition with `InputNeeded` on the session summary
        "so the activity becomes visible at the session-summary level"."""
        host, provider = wired
        client = await _client(host)
        uri = "echo:/mcp-4"
        await _session(host, client, uri)
        assert provider.session is not None
        publisher = provider.session.context.publisher
        assert publisher is not None

        # The pinned `McpServerAuthRequiredState` (session-state.ts:1273,1325)
        # requires `reason` and nests the RFC 9728 metadata under `resource`;
        # a flattened payload here would pin a shape no conformant peer sends.
        await publisher.mcp_server_changed(
            "srv-1", {"kind": "authRequired", "reason": "required", "resource": _RESOURCE}
        )
        await client.collect(seconds=0.3)
        state = (await client.request("subscribe", {"channel": uri}))["result"]["snapshot"]["state"]
        assert any(r["kind"] == "toolAuthentication" for r in state.get("inputNeeded") or [])
        assert state["status"] & SessionStatus.INPUT_NEEDED == SessionStatus.INPUT_NEEDED

        await publisher.mcp_server_changed("srv-1", {"kind": "ready"})
        await client.collect(seconds=0.3)
        state = (await client.request("subscribe", {"channel": uri}))["result"]["snapshot"]["state"]
        assert state.get("inputNeeded", []) == []

    async def test_a_provider_that_manages_nothing_is_simply_not_called(self) -> None:
        """`ManagesMcpServers` is feature-detected. A provider without it gets
        the reducer's state change and nothing else -- no error, no spawn."""
        host = Host(EchoProvider(), LoopbackSingleUserPolicy())
        try:
            client = await _client(host)
            uri = "echo:/mcp-5"
            await _session(host, client, uri)
            await client.notify(
                "dispatchAction",
                {
                    "channel": uri,
                    "clientSeq": 1,
                    "action": {"type": "session/mcpServerStartRequested", "id": "srv-1"},
                },
            )
            await client.collect(seconds=0.3)
            assert "error" not in await client.request("ping", {"channel": ROOT_URI})
        finally:
            await host.aclose()


class TestStepUpAuth:
    async def test_a_tool_call_pauses_and_resumes_on_a_pushed_token(
        self, wired: tuple[Host, McpProvider]
    ) -> None:
        """The distinguishing feature: the resolution arrives as the
        `authenticate` COMMAND, not through `dispatchAction`."""
        host, provider = wired
        client = await _client(host)
        uri = "echo:/auth-1"
        chat = await _session(host, client, uri)

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
        await client.collect(seconds=0.4)

        challenge = [
            e["action"]
            for e in client.actions(chat)
            if e["action"]["type"] == "chat/toolCallAuthRequired"
        ]
        assert challenge, "the tool call never paused"
        assert challenge[-1]["auth"]["reason"] == "insufficientScope"
        assert challenge[-1]["auth"]["requiredScopes"] == ["read:all"]
        assert len(host.pending) == 1

        result = await client.request(
            "authenticate",
            {
                "channel": ROOT_URI,
                "resource": _RESOURCE["resource"],
                "token": "fresh-token",
                "scopes": ["read:all"],
            },
        )
        assert result["result"] == {}
        await client.collect(seconds=0.5)

        assert provider.session is not None
        assert provider.session.authenticated, "the provider never resumed"
        kinds = [e["action"]["type"] for e in client.actions(chat)]
        assert "chat/toolCallAuthResolved" in kinds
        assert len(host.pending) == 0

    async def test_the_session_surfaces_the_blocked_call(
        self, wired: tuple[Host, McpProvider]
    ) -> None:
        """ "Clients SHOULD watch for this kind on any MCP server backing a
        running tool call so they can present an explicit 'grant more access'
        affordance tied to the blocked tool call.\""""
        host, _provider = wired
        client = await _client(host)
        uri = "echo:/auth-2"
        chat = await _session(host, client, uri)
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
        await client.collect(seconds=0.4)

        state = (await client.request("subscribe", {"channel": uri}))["result"]["snapshot"]["state"]
        entries = state.get("inputNeeded") or []
        assert any(e["kind"] == "toolAuthentication" for e in entries)

    async def test_cancelling_the_turn_frees_the_paused_call(
        self, wired: tuple[Host, McpProvider]
    ) -> None:
        """Same primitive as everything else that waits, so the same guarantee."""
        host, _provider = wired
        client = await _client(host)
        uri = "echo:/auth-3"
        chat = await _session(host, client, uri)
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
        await client.collect(seconds=0.4)
        assert len(host.pending) == 1

        await client.notify(
            "dispatchAction",
            {
                "channel": chat,
                "clientSeq": 2,
                "action": {"type": "chat/turnCancelled", "turnId": "t1"},
            },
        )
        await client.collect(seconds=0.4)
        assert len(host.pending) == 0


_OTHER_RESOURCE = {
    "resource": "https://other.example.invalid",
    "authorization_servers": ["https://auth.example.invalid"],
}


class DynamicOnlyProvider(McpProvider):
    """Advertises NOTHING statically; a live challenge is the only advertisement."""

    def __init__(self) -> None:
        super().__init__()
        self._info = type(self._info)(
            provider="echo",
            display_name="Echo",
            description="",
            models=(),
            protected_resources=(),
        )


class TwoResourceProvider(McpProvider):
    """Both resources are static, so `authenticate` accepts a push for either.

    What distinguishes the parks is which resource each CHALLENGE named --
    exactly the case a resolve-everything `authenticate` gets wrong."""

    def __init__(self) -> None:
        super().__init__()
        self._info = type(self._info)(
            provider="echo",
            display_name="Echo",
            description="",
            models=(),
            protected_resources=(_RESOURCE, _OTHER_RESOURCE),
        )


async def _challenge(client: FakeClient, chat: str) -> None:
    """Start the turn that parks on `chat/toolCallAuthRequired`."""
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
    await client.collect(seconds=0.4)


class TestDynamicallyAdvertisedResources:
    """ "Servers MUST accept any `resource` value they have themselves
    advertised" -- statically in `AgentInfo.protectedResources`, or discovered
    dynamically from a live `ToolCallAuthRequiredState.auth.resource` or
    `McpServerAuthRequiredState.resource`. Checking only the static list
    deadlocked step-up for any provider that challenges for a resource it never
    declared up front: the token was refused -32602 and the parked call could
    never clear."""

    async def test_a_resource_advertised_only_by_a_tool_call_challenge_is_accepted(self) -> None:
        provider = DynamicOnlyProvider()
        host = Host(provider, LoopbackSingleUserPolicy())
        try:
            client = await _client(host)
            chat = await _session(host, client, "echo:/dyn-1")
            await _challenge(client, chat)
            assert len(host.pending) == 1

            pushed = await client.request(
                "authenticate",
                {
                    "channel": ROOT_URI,
                    "resource": _RESOURCE["resource"],
                    "token": "fresh-token",
                    "scopes": ["read:all"],
                },
            )
            assert "error" not in pushed, pushed.get("error")
            await client.collect(seconds=0.5)
            assert provider.session is not None
            assert provider.session.authenticated, "the parked call never resumed"
            assert len(host.pending) == 0
        finally:
            await host.aclose()

    async def test_a_resource_advertised_only_by_an_mcp_auth_state_is_accepted(self) -> None:
        """`McpServerAuthRequiredState.resource` is RFC 9728 metadata whose own
        `resource` member is the canonical identifier."""
        provider = DynamicOnlyProvider()
        host = Host(provider, LoopbackSingleUserPolicy())
        try:
            client = await _client(host)
            await _session(host, client, "echo:/dyn-2")
            assert provider.session is not None
            publisher = provider.session.context.publisher
            assert publisher is not None

            await publisher.mcp_server_changed(
                "srv-1",
                {"kind": "authRequired", "reason": "insufficientScope", "resource": _RESOURCE},
            )
            await client.collect(seconds=0.3)

            pushed = await client.request(
                "authenticate",
                {"channel": ROOT_URI, "resource": _RESOURCE["resource"], "token": "fresh-token"},
            )
            assert "error" not in pushed, pushed.get("error")
        finally:
            await host.aclose()


class TestChallengeKeyedResolution:
    """Step-up is "resolved by the client obtaining a token for
    `auth.resource`" -- so a push wakes only the calls whose challenge named
    the pushed resource AND whose required scopes the push covers, the two
    checks the reference session makes before resolving."""

    async def test_a_token_for_a_different_resource_does_not_resume_the_call(self) -> None:
        provider = TwoResourceProvider()
        host = Host(provider, LoopbackSingleUserPolicy())
        try:
            client = await _client(host)
            chat = await _session(host, client, "echo:/keyed-1")
            await _challenge(client, chat)
            assert len(host.pending) == 1

            # Advertised -- the push itself is accepted -- but it is not the
            # resource this challenge named.
            pushed = await client.request(
                "authenticate",
                {
                    "channel": ROOT_URI,
                    "resource": _OTHER_RESOURCE["resource"],
                    "token": "wrong-door",
                    "scopes": ["read:all"],
                },
            )
            assert "error" not in pushed, pushed.get("error")
            await client.collect(seconds=0.4)
            assert len(host.pending) == 1, "a token for resource B resumed a call parked on A"
            kinds = [e["action"]["type"] for e in client.actions(chat)]
            assert "chat/toolCallAuthResolved" not in kinds

            # The token the challenge actually asked for still works.
            await client.request(
                "authenticate",
                {
                    "channel": ROOT_URI,
                    "resource": _RESOURCE["resource"],
                    "token": "right-door",
                    "scopes": ["read:all"],
                },
            )
            await client.collect(seconds=0.5)
            assert len(host.pending) == 0
        finally:
            await host.aclose()

    async def test_a_token_missing_the_required_scopes_does_not_resume_the_call(self) -> None:
        """An explicit grant that omits the challenged scope is the client
        saying the token does not cover it; only an unscoped push gets the
        benefit of the doubt."""
        provider = McpProvider()
        host = Host(provider, LoopbackSingleUserPolicy())
        try:
            client = await _client(host)
            chat = await _session(host, client, "echo:/keyed-2")
            await _challenge(client, chat)
            assert len(host.pending) == 1

            await client.request(
                "authenticate",
                {
                    "channel": ROOT_URI,
                    "resource": _RESOURCE["resource"],
                    "token": "too-narrow",
                    "scopes": ["profile"],
                },
            )
            await client.collect(seconds=0.4)
            assert len(host.pending) == 1, "a token without the challenged scope resumed the call"

            await client.request(
                "authenticate",
                {
                    "channel": ROOT_URI,
                    "resource": _RESOURCE["resource"],
                    "token": "wide-enough",
                    "scopes": ["read:all"],
                },
            )
            await client.collect(seconds=0.5)
            assert len(host.pending) == 0
        finally:
            await host.aclose()
