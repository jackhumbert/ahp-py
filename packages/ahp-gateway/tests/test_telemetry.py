"""Telemetry: the gateway's own OTLP channel per signal, fanned in from the nodes.

The end-to-end half runs real sibling hosts that emit OTLP and a raw client
that reads the handshake and the batches. The other half drives
`_SurfaceConnection` against recording links, because the sibling host only
advertises literal URIs, and the `{level}` template - expanded per node - is
exactly what needs pinning down.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from typing import Any

import pytest
from ahp_client import AhpClient, RpcError
from ahp_client.client import BroadcastReader, ClientEvent, OtlpEvent
from ahp_host import Host, LoopbackSingleUserPolicy
from ahp_host.provider.echo import EchoProvider
from ahp_protocol import AhpError

from ahp_gateway.core.gateway import _SurfaceConnection
from ahp_gateway.core.telemetry import (
    LOGS_TEMPLATE,
    Wanted,
    advertised,
    expand,
    node_signals,
    parse,
)
from ahp_gateway.registry import NodeRecord
from tests.fleet import DEV, Fleet, everyone_is_a_dev
from tests.links import RecordingLink, connection, drain

# ─── the pieces ──────────────────────────────────────────────────────────


def test_a_signal_is_advertised_when_any_node_emits_it() -> None:
    assert advertised([{"logs": "ahp-otlp://a/logs"}, {"traces": "ahp-otlp://b/t"}]) == {
        "logs": "ahp-otlp://logs",
        "traces": "ahp-otlp://traces",
    }
    assert advertised([{}, {}]) is None
    assert advertised([]) is None


def test_the_logs_template_needs_every_logging_node_to_filter() -> None:
    filtering = {"logs": "ahp-otlp://a/logs{?level}"}
    literal = {"logs": "ahp-otlp://b/logs"}
    assert advertised([filtering, {"logs": "ahp-otlp://c/{level}"}]) == {"logs": LOGS_TEMPLATE}
    assert advertised([filtering, literal]) == {"logs": "ahp-otlp://logs"}
    # A node that emits no logs does not count against the template.
    assert advertised([filtering, {"metrics": "ahp-otlp://b/m"}]) == {
        "logs": LOGS_TEMPLATE,
        "metrics": "ahp-otlp://metrics",
    }


def test_a_node_signal_this_version_does_not_define_is_ignored() -> None:
    handshake = {
        "telemetry": {"logs": "ahp-otlp://x/logs", "profiles": "ahp-otlp://x/p", "traces": 7},
    }
    assert node_signals(handshake) == {"logs": "ahp-otlp://x/logs"}
    assert node_signals({"telemetry": {"logs": "https://example.com/logs"}}) == {}
    assert node_signals({}) == {}


def test_the_surface_channel_says_which_signal_and_level() -> None:
    assert parse("ahp-otlp://logs") == Wanted("logs")
    assert parse("ahp-otlp://logs?level=WARN") == Wanted("logs", "warn")
    assert parse("ahp-otlp://traces") == Wanted("traces")
    assert parse("ahp-otlp://traces?level=info") is None
    assert parse("ahp-otlp://a/logs") is None
    with pytest.raises(ValueError, match="loud"):
        parse("ahp-otlp://logs?level=loud")


def test_a_nodes_template_is_expanded_by_rfc_6570() -> None:
    level = {"level": "info"}
    assert expand("ahp-otlp://a/logs{?level}", level) == "ahp-otlp://a/logs?level=info"
    assert expand("ahp-otlp://a/logs/{level}", level) == "ahp-otlp://a/logs/info"
    assert expand("ahp-otlp://a/logs{/level}", level) == "ahp-otlp://a/logs/info"
    assert expand("ahp-otlp://a/logs?x=1{&level}", level) == "ahp-otlp://a/logs?x=1&level=info"
    assert expand("ahp-otlp://a/logs{;level}", level) == "ahp-otlp://a/logs;level=info"
    # Unknown variables MUST be ignored: undefined, so they expand to nothing.
    assert expand("ahp-otlp://a/logs{?level,scope}", level) == "ahp-otlp://a/logs?level=info"
    # No level asked for: every severity.
    assert expand("ahp-otlp://a/logs{?level}", {}) == "ahp-otlp://a/logs"
    assert expand("ahp-otlp://a/logs", level) == "ahp-otlp://a/logs"


# ─── the router, against recording links ─────────────────────────────────


def tele(signals: Mapping[str, str]) -> dict[str, Any]:
    """A node handshake advertising `signals`."""
    return {"telemetry": dict(signals)}


def batch(channel: str, marker: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "method": "otlp/exportLogs",
        "params": {"channel": channel, "payload": {"resourceLogs": [{"marker": marker}]}},
    }


async def subscribe(conn: _SurfaceConnection, channel: str) -> Any:
    return await conn._dispatch("subscribe", {"channel": channel}, [])


def unsubscribe(conn: _SurfaceConnection, channel: str) -> None:
    conn._handle_notification({"method": "unsubscribe", "params": {"channel": channel}})


async def test_a_level_reaches_every_node_in_its_own_template() -> None:
    a = RecordingLink("a", tele({"logs": "ahp-otlp://a/logs{?level}"}))
    b = RecordingLink("b", tele({"logs": "ahp-otlp://b/logs/{level}"}))
    conn = connection(a, b)
    assert conn._agreed_handshake_fields()["telemetry"] == {"logs": LOGS_TEMPLATE}

    assert await subscribe(conn, "ahp-otlp://logs?level=WARN") == {}
    assert a.requests == [("subscribe", {"channel": "ahp-otlp://a/logs?level=warn"})]
    assert b.requests == [("subscribe", {"channel": "ahp-otlp://b/logs/warn"})]


async def test_a_node_that_cannot_filter_is_subscribed_to_everything() -> None:
    a = RecordingLink("a", tele({"logs": "ahp-otlp://a/logs{?level}"}))
    b = RecordingLink("b", tele({"logs": "ahp-otlp://b/logs"}))
    conn = connection(a, b)
    # Not every node filters, so no template is promised.
    assert conn._agreed_handshake_fields()["telemetry"] == {"logs": "ahp-otlp://logs"}

    await subscribe(conn, "ahp-otlp://logs?level=error")
    assert a.requests == [("subscribe", {"channel": "ahp-otlp://a/logs?level=error"})]
    assert b.requests == [("subscribe", {"channel": "ahp-otlp://b/logs"})]


async def test_batches_reach_the_surface_only_on_a_channel_it_holds() -> None:
    a = RecordingLink("a", tele({"logs": "ahp-otlp://a/logs"}))
    conn = connection(a)
    node = conn.nodes["a"]

    conn._relay(node, batch("ahp-otlp://a/logs", "before"))
    assert drain(conn) == [], "nothing was subscribed, so nothing was agreed"

    await subscribe(conn, "ahp-otlp://logs")
    conn._relay(node, batch("ahp-otlp://a/logs", "live"))
    conn._relay(node, batch("ahp-otlp://a/other", "stray"))
    (sent,) = drain(conn)
    assert sent["method"] == "otlp/exportLogs"
    assert sent["params"] == {
        "channel": "ahp-otlp://logs",
        "payload": {"resourceLogs": [{"marker": "live"}]},
    }

    unsubscribe(conn, "ahp-otlp://logs")
    assert a.notified == [("unsubscribe", {"channel": "ahp-otlp://a/logs"})]
    conn._relay(node, batch("ahp-otlp://a/logs", "after"))
    assert drain(conn) == []


async def test_one_node_channel_feeds_every_surface_channel_it_answers() -> None:
    # A literal logs URI serves both levels the surface asked for.
    a = RecordingLink("a", tele({"logs": "ahp-otlp://a/logs"}))
    conn = connection(a)
    node = conn.nodes["a"]
    await subscribe(conn, "ahp-otlp://logs")
    await subscribe(conn, "ahp-otlp://logs?level=error")

    conn._relay(node, batch("ahp-otlp://a/logs", "x"))
    assert sorted(frame["params"]["channel"] for frame in drain(conn)) == [
        "ahp-otlp://logs",
        "ahp-otlp://logs?level=error",
    ]

    unsubscribe(conn, "ahp-otlp://logs")
    assert a.notified == [], "the node channel still feeds the other subscription"
    conn._relay(node, batch("ahp-otlp://a/logs", "y"))
    assert [frame["params"]["channel"] for frame in drain(conn)] == ["ahp-otlp://logs?level=error"]
    unsubscribe(conn, "ahp-otlp://logs?level=error")
    assert a.notified == [("unsubscribe", {"channel": "ahp-otlp://a/logs"})]


async def test_one_refusal_costs_only_that_nodes_batches() -> None:
    a = RecordingLink("a", tele({"logs": "ahp-otlp://a/logs"}), refuse=True)
    b = RecordingLink("b", tele({"logs": "ahp-otlp://b/logs"}))
    conn = connection(a, b)
    assert await subscribe(conn, "ahp-otlp://logs") == {}
    conn._relay(conn.nodes["b"], batch("ahp-otlp://b/logs", "b"))
    assert len(drain(conn)) == 1


async def test_every_node_refusing_fails_the_subscribe() -> None:
    a = RecordingLink("a", tele({"logs": "ahp-otlp://a/logs"}), refuse=True)
    conn = connection(a)
    with pytest.raises(AhpError) as caught:
        await subscribe(conn, "ahp-otlp://logs")
    assert caught.value.code == -32009
    assert "ahp-otlp://logs" not in conn.subscriptions


async def test_a_level_the_protocol_does_not_define_is_refused() -> None:
    conn = connection(RecordingLink("a", tele({"logs": "ahp-otlp://a/logs{?level}"})))
    with pytest.raises(AhpError) as caught:
        await subscribe(conn, "ahp-otlp://logs?level=loud")
    assert caught.value.code == -32602


async def test_an_otlp_uri_the_gateway_never_advertised_is_no_channel() -> None:
    a = RecordingLink("a", tele({"logs": "ahp-otlp://a/logs"}))
    conn = connection(a)
    # A node's own URI is not the surface's to name: nothing is subscribed.
    assert await subscribe(conn, "ahp-otlp://a/logs") == {}
    assert a.requests == []


async def test_subscribe_options_ride_along_to_the_nodes() -> None:
    a = RecordingLink("a", tele({"traces": "ahp-otlp://a/traces"}))
    conn = connection(a)
    await conn._dispatch(
        "subscribe", {"channel": "ahp-otlp://traces", "delivery": {"maxLatencyMs": 0}}, []
    )
    assert a.requests == [
        ("subscribe", {"channel": "ahp-otlp://a/traces", "delivery": {"maxLatencyMs": 0}})
    ]


# ─── end to end: real hosts emitting OTLP ────────────────────────────────


def _emitting_host(provider: str, telemetry: Mapping[str, str]) -> Host:
    return Host(EchoProvider(provider_id=provider), LoopbackSingleUserPolicy(), telemetry=telemetry)


@pytest.fixture
async def emitting() -> AsyncIterator[Fleet]:
    """node-a emits logs and traces, node-b logs only; neither emits metrics."""
    fleet = Fleet(
        {
            "node-a": _emitting_host(
                "alpha", {"logs": "ahp-otlp://a/logs", "traces": "ahp-otlp://a/traces"}
            ),
            "node-b": _emitting_host("beta", {"logs": "ahp-otlp://b/logs"}),
        },
        [NodeRecord("node-a", "mem://a", DEV), NodeRecord("node-b", "mem://b", DEV)],
        everyone_is_a_dev,
    )
    yield fleet
    await fleet.aclose()


async def _next_otlp(reader: BroadcastReader[ClientEvent], timeout: float = 5.0) -> OtlpEvent:
    async with asyncio.timeout(timeout):
        async for tagged in reader:
            if isinstance(tagged.event, OtlpEvent):
                return tagged.event
    raise AssertionError("the event stream ended")


async def test_the_fleet_advertises_each_signal_some_node_emits(emitting: Fleet) -> None:
    raw = AhpClient(emitting.surface_transport())
    await raw.connect()
    result = await raw.initialize(client_id="c1")
    await raw.shutdown()
    assert result["telemetry"] == {"logs": "ahp-otlp://logs", "traces": "ahp-otlp://traces"}


async def test_no_telemetry_is_advertised_when_no_node_emits_any(fleet: Fleet) -> None:
    raw = AhpClient(fleet.surface_transport())
    await raw.connect()
    result = await raw.initialize(client_id="c1")
    await raw.shutdown()
    assert "telemetry" not in result


async def test_every_nodes_batches_arrive_on_the_one_channel(emitting: Fleet) -> None:
    a, b = emitting.hosts["node-a"], emitting.hosts["node-b"]
    raw = AhpClient(emitting.surface_transport())
    await raw.connect()
    await raw.initialize(client_id="c1")
    reader = raw.events()
    try:
        # Before any subscribe nothing was agreed: the hosts send nothing.
        await a.emit_telemetry("logs", {"resourceLogs": [{"from": "a", "n": 0}]})

        result, _ = await raw.subscribe("ahp-otlp://logs")
        assert result == {}
        await a.emit_telemetry("logs", {"resourceLogs": [{"from": "a", "n": 1}]})
        await b.emit_telemetry("logs", {"resourceLogs": [{"from": "b", "n": 1}]})
        got = [await _next_otlp(reader), await _next_otlp(reader)]
        assert {event.signal for event in got} == {"logs"}
        assert {event.params["channel"] for event in got} == {"ahp-otlp://logs"}
        # Verbatim: the payload is OTLP/JSON, and AHP only adds the envelope.
        assert sorted(event.params["payload"]["resourceLogs"][0]["from"] for event in got) == [
            "a",
            "b",
        ]

        # Unsubscribed, then a later signal as a sentinel: on one ordered
        # link, a log batch forwarded after the unsubscribe would come first.
        await raw.unsubscribe("ahp-otlp://logs")
        await raw.subscribe("ahp-otlp://traces")
        await a.emit_telemetry("logs", {"resourceLogs": [{"from": "a", "n": 2}]})
        await a.emit_telemetry("traces", {"resourceSpans": [{"from": "a"}]})
        sentinel = await _next_otlp(reader)
        assert sentinel.signal == "traces"
        assert sentinel.params["channel"] == "ahp-otlp://traces"
    finally:
        await reader.aclose()
        await raw.shutdown()


async def test_telemetry_is_not_resumed_by_a_reconnect(emitting: Fleet) -> None:
    raw = AhpClient(emitting.surface_transport())
    await raw.connect()
    result = await raw.reconnect(
        client_id="c1", last_seen_server_seq=0, subscriptions=["ahp-otlp://logs"]
    )
    reader = raw.events()
    try:
        # Stateless channels are "simply re-subscribed": the snapshot arm
        # leaves it out, and nothing streams until the client asks again.
        assert result == {"type": "snapshot", "snapshots": []}
        await emitting.hosts["node-a"].emit_telemetry("logs", {"resourceLogs": [{"n": 1}]})
        await raw.subscribe("ahp-otlp://traces")
        await emitting.hosts["node-a"].emit_telemetry("traces", {"resourceSpans": []})
        assert (await _next_otlp(reader)).signal == "traces"
    finally:
        await reader.aclose()
        await raw.shutdown()


async def test_a_subscribe_with_a_bad_level_is_refused_end_to_end(emitting: Fleet) -> None:
    raw = AhpClient(emitting.surface_transport())
    await raw.connect()
    await raw.initialize(client_id="c1")
    with pytest.raises(RpcError) as caught:
        await raw.request("subscribe", {"channel": "ahp-otlp://logs?level=loud"})
    await raw.shutdown()
    assert caught.value.code == -32602
