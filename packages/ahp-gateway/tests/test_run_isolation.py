"""Run isolation: one principal's flow run is neither readable nor drivable by another.

A flow session, its chats, every session its run started and every chat of
those are its principal's alone. The gateway checks this at one choke point
(`_check_run_access`) on every path that reaches such a channel: a subscribe
(including `initialize`'s and `reconnect`'s), any request that names one, and
any client action dispatched on one. Each path is tried here twice: refused
for another principal, and still working for the run's own.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Any

import pytest
from ahp_client import AhpClient, RpcError
from ahp_client.client import actions
from ahp_host import Host, LoopbackSingleUserPolicy

from ahp_gateway.core.node import AhpNodeLink
from tests.fleet import Fleet
from tests.flowfleet import (
    RecordingEcho,
    ToolCaller,
    active_clients,
    eventually,
    flow_fleet,
    run_tool,
    start_flow,
)

pytestmark = pytest.mark.anyio

RUN = "flow:/r1"
OWNER = "surface-1"
OTHER = "surface-2"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def run(tmp_path: Path) -> AsyncIterator[tuple[Fleet, dict[str, str]]]:
    """A flow run of `surface-1`'s, with one child session on `alpha`.

    The names: `session`/`chat` (the child and its default chat), `flow_chat`
    (the flow session's default chat) and `side_chat` (a second chat of the
    child, opened by the owner, known to the gateway only through its mirror).
    """
    many: dict[str, Any] = {"multipleChats": {}}
    hosts = {
        "flow": Host(ToolCaller(provider_id="flow"), LoopbackSingleUserPolicy()),
        "alpha": Host(
            RecordingEcho(provider_id="claude", capabilities=many), LoopbackSingleUserPolicy()
        ),
    }
    fleet = flow_fleet(tmp_path, hosts=hosts)
    try:
        await start_flow(fleet, RUN, OWNER)
        await eventually(lambda: bool(active_clients(fleet.hosts["flow"], RUN)))
        started = (
            await run_tool(fleet, RUN, "start_session", node="alpha", agent="claude", prompt="hi")
        )["structuredContent"]
        orchestrator = fleet.gateway.orchestrator
        assert orchestrator is not None
        flow_client = orchestrator._clients[(OWNER, "flow")]
        flow_state = flow_client.mirror.state(RUN)
        assert isinstance(flow_state, Mapping)
        names = {
            "session": started["session"],
            "chat": started["chat"],
            "flow_chat": str(flow_state["defaultChat"]),
            "side_chat": f"{started['session']}/side",
        }
        owner = await _raw(fleet, OWNER)
        await owner.request("subscribe", {"channel": names["session"]})
        await owner.request("createChat", {"channel": names["session"], "chat": names["side_chat"]})
        child_client = orchestrator._clients[(OWNER, "alpha")]

        def side_chat_mirrored() -> bool:
            state = child_client.mirror.state(names["session"])
            chats = state.get("chats") if isinstance(state, Mapping) else None
            return any(
                isinstance(c, Mapping) and c.get("resource") == names["side_chat"]
                for c in chats or []
            )

        await eventually(side_chat_mirrored)
        await owner.shutdown()
        yield fleet, names
    finally:
        await fleet.gateway.aclose()
        await fleet.aclose()


async def _raw(fleet: Fleet, client_id: str, **initialize: Any) -> AhpClient:
    raw = AhpClient(fleet.surface_transport())
    await raw.connect()
    await raw.initialize(client_id=client_id, **initialize)
    return raw


async def _learn(raw: AhpClient, names: Mapping[str, str]) -> None:
    """Have the gateway learn which node holds the run's channels, as a client browsing would."""
    await raw.request("listSessions", {})
    for channel in (RUN, names["session"]):
        with contextlib.suppress(RpcError):
            await raw.request("subscribe", {"channel": channel})


def _resources(result: Mapping[str, Any]) -> set[str]:
    return {str(s.get("resource")) for s in result.get("snapshots") or []}


def _channels(names: Mapping[str, str]) -> list[str]:
    return [RUN, names["flow_chat"], names["session"], names["chat"], names["side_chat"]]


async def test_initialize_subscriptions_are_the_owners_only(
    run: tuple[Fleet, dict[str, str]],
) -> None:
    fleet, names = run
    for client_id, expected in ((OTHER, set()), (OWNER, set(_channels(names)))):
        raw = AhpClient(fleet.surface_transport())
        await raw.connect()
        result = await raw.initialize(client_id=client_id, initial_subscriptions=_channels(names))
        assert _resources(result) == expected, client_id
        await raw.shutdown()


async def test_reconnect_subscriptions_are_the_owners_only(
    run: tuple[Fleet, dict[str, str]],
) -> None:
    fleet, names = run
    for client_id, expected in ((OTHER, set()), (OWNER, set(_channels(names)))):
        raw = AhpClient(fleet.surface_transport())
        await raw.connect()
        result = await raw.reconnect(
            client_id=client_id, last_seen_server_seq=1, subscriptions=_channels(names)
        )
        assert _resources(result) == expected, client_id
        await raw.shutdown()


@pytest.mark.parametrize("which", ["flow", "flow_chat", "session", "chat", "side_chat"])
async def test_subscribe_is_the_owners_only(run: tuple[Fleet, dict[str, str]], which: str) -> None:
    fleet, names = run
    channel = RUN if which == "flow" else names[which]
    other = await _raw(fleet, OTHER)
    with pytest.raises(RpcError, match="another user's flow run"):
        await other.request("subscribe", {"channel": channel})
    await other.shutdown()
    owner = await _raw(fleet, OWNER)
    result = await owner.request("subscribe", {"channel": channel})
    assert result["snapshot"]["resource"] == channel
    await owner.shutdown()


async def test_requests_naming_a_runs_channel_are_the_owners_only(
    run: tuple[Fleet, dict[str, str]],
) -> None:
    fleet, names = run
    requests: list[tuple[str, dict[str, Any]]] = [
        ("fetchTurns", {"channel": names["chat"]}),
        ("fetchTurns", {"channel": names["side_chat"]}),
        ("createChat", {"channel": names["session"], "chat": f"{names['session']}/extra"}),
        ("disposeChat", {"channel": names["side_chat"]}),
        ("disposeSession", {"channel": names["session"]}),
    ]
    other = await _raw(fleet, OTHER)
    await _learn(other, names)
    for method, params in requests:
        with pytest.raises(RpcError, match="another user's flow run"):
            await other.request(method, params)
    await other.shutdown()
    on_alpha = fleet.hosts["alpha"].sequencer
    assert on_alpha.state_of(names["session"]) is not None
    assert on_alpha.state_of(names["side_chat"]) is not None

    owner = await _raw(fleet, OWNER)
    await _learn(owner, names)
    for method, params in requests:
        await owner.request(method, params)
    await owner.shutdown()
    assert on_alpha.state_of(names["session"]) is None


async def test_client_actions_on_a_runs_channel_are_the_owners_only(
    run: tuple[Fleet, dict[str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every client action, not only `chat/turnStarted`, is refused to another principal."""
    fleet, names = run
    sent: list[tuple[str, str]] = []
    original = AhpNodeLink.notify

    def recording(self: AhpNodeLink, method: str, params: Mapping[str, Any]) -> None:
        action = params.get("action")
        if method == "dispatchAction" and isinstance(action, Mapping):
            sent.append((str(params.get("channel")), str(action.get("type"))))
        original(self, method, params)

    monkeypatch.setattr(AhpNodeLink, "notify", recording)
    message = {"text": "queued", "origin": {"kind": "user"}}
    dispatches: list[tuple[str, Mapping[str, Any]]] = [
        (
            names["chat"],
            {"type": "chat/pendingMessageSet", "id": "q1", "kind": "queued", "message": message},
        ),
        (names["chat"], {"type": "chat/turnResume", "turnId": "t-none"}),
        (
            names["side_chat"],
            {"type": "chat/pendingMessageSet", "id": "q2", "kind": "queued", "message": message},
        ),
        (names["flow_chat"], {"type": "chat/turnResume", "turnId": "t-none"}),
        (names["flow_chat"], actions.turn_started("t-other", text="steer it")),
    ]
    other = await _raw(fleet, OTHER)
    await _learn(other, names)
    for channel, action in dispatches:
        other.dispatch(channel, action)
    await asyncio.sleep(0.3)
    await other.shutdown()
    assert sent == []
    chat = fleet.hosts["alpha"].sequencer.state_of(names["chat"])
    assert isinstance(chat, Mapping)
    assert not chat.get("queuedMessages")

    owner = await _raw(fleet, OWNER)
    await _learn(owner, names)
    for channel, action in dispatches[:-1]:
        owner.dispatch(channel, action)
    await eventually(lambda: len(sent) == len(dispatches) - 1)
    await owner.shutdown()
    assert [type_ for _, type_ in sent] == [str(a["type"]) for _, a in dispatches[:-1]]
