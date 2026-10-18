"""ahp-py's redaction extension, client side: opt-in, mirror application, events."""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any

from ahp_protocol.ext.redaction import (
    REDACTION_CAPABILITY,
    marker_of,
    message_redacted,
    part_id_of,
    part_redacted,
)
from ahp_protocol.transport import memory_pair

from ahp_client import MessageRedacted, PartRedacted, event_for
from ahp_client.api.events import is_modelled
from ahp_client.client.client import AhpClient, ClientConfig
from ahp_client.client.mirror import ApplyOutcome, StateMirror

from ._sibling import requires_sibling_host

CHAT = "ahp-chat://c/session"
AT = "2026-01-01T00:00:00.000Z"


def _chat_state() -> dict[str, Any]:
    return {
        "turns": [
            {
                "id": "t1",
                "message": {"text": "hello"},
                "responseParts": [{"kind": "markdown", "id": "p1", "content": "PLAINLEAK-4242"}],
            }
        ],
        "status": 1,
    }


def _redaction_envelope(seq: int, action: dict[str, Any]) -> dict[str, Any]:
    return {"channel": CHAT, "serverSeq": seq, "action": action}


def test_redaction_client_mirror_applies_the_extension_action() -> None:
    mirror = StateMirror(client_id="me")
    mirror.bind(CHAT, "chat")
    mirror.apply_snapshot({"resource": CHAT, "state": _chat_state(), "fromSeq": 3})
    outcome = mirror.apply(_redaction_envelope(4, part_redacted("t1", "p1", "leaked", AT)))
    assert outcome is ApplyOutcome.APPLIED
    state = mirror.state(CHAT)
    part = state["turns"][0]["responseParts"][0]
    assert part["content"] == "[redacted]"
    assert part["id"] == "p1"
    marker = marker_of(part)
    assert marker is not None
    assert marker["reason"] == "leaked"
    assert "PLAINLEAK" not in json.dumps(state)


def test_redaction_client_mirror_applies_a_message_redaction() -> None:
    mirror = StateMirror(client_id="me")
    mirror.bind(CHAT, "chat")
    mirror.apply_snapshot({"resource": CHAT, "state": _chat_state(), "fromSeq": 0})
    mirror.apply(_redaction_envelope(1, message_redacted("t1", "pii", AT)))
    message = mirror.state(CHAT)["turns"][0]["message"]
    assert message["text"] == "[redacted]"


def test_redaction_client_events_are_typed_and_modelled() -> None:
    part = event_for(_redaction_envelope(4, part_redacted("t1", "p1", "leaked", AT)))
    assert isinstance(part, PartRedacted)
    assert part.part_id == "p1"
    assert part.reason == "leaked"
    assert part.redacted_at == AT
    assert part.turn_id == "t1"
    message = event_for(_redaction_envelope(5, message_redacted("t1", "pii", AT)))
    assert isinstance(message, MessageRedacted)
    assert message.reason == "pii"
    assert message.redacted_at == AT
    for envelope in (part.envelope, message.envelope):
        assert is_modelled(envelope)


async def _initialize_params(config: ClientConfig, method: str) -> dict[str, Any]:
    client_side, host_side = memory_pair()
    client = AhpClient(client_side, config)
    await client.connect()
    if method == "initialize":
        task = asyncio.ensure_future(client.initialize(client_id="c1"))
    else:
        task = asyncio.ensure_future(
            client.reconnect(client_id="c1", last_seen_server_seq=0, subscriptions=[])
        )
    frame = await asyncio.wait_for(host_side.receive(), 2)
    assert frame is not None
    assert frame["method"] == method
    offered = frame["params"].get("protocolVersions") or ["1.0.0"]
    await host_side.send(
        {
            "jsonrpc": "2.0",
            "id": frame["id"],
            "result": {"protocolVersion": offered[0], "serverSeq": 0, "snapshots": []},
        }
    )
    await asyncio.wait_for(task, 2)
    await client.shutdown()
    params: dict[str, Any] = frame["params"]
    return params


async def test_redaction_client_initialize_advertises_in_meta() -> None:
    params = await _initialize_params(ClientConfig(redaction=True), "initialize")
    assert params["_meta"] == {REDACTION_CAPABILITY: {}}
    assert "redaction" not in json.dumps(params.get("capabilities", {}))
    params = await _initialize_params(ClientConfig(), "initialize")
    assert "_meta" not in params


async def test_redaction_client_reconnect_advertises_in_meta() -> None:
    params = await _initialize_params(ClientConfig(redaction=True), "reconnect")
    assert params["_meta"] == {REDACTION_CAPABILITY: {}}
    params = await _initialize_params(ClientConfig(), "reconnect")
    assert "_meta" not in params


async def _poll(predicate: Any, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("timed out")
        await asyncio.sleep(0.02)


@requires_sibling_host
async def test_redaction_client_end_to_end_against_the_sibling_host() -> None:
    from ahp_host.core import Host, LoopbackSingleUserPolicy
    from ahp_host.provider import EchoProvider

    from ahp_client.api import connect

    host = Host(EchoProvider(), LoopbackSingleUserPolicy())
    served: list[asyncio.Task[Any]] = []

    def attach() -> Any:
        client_side, host_side = memory_pair()
        served.append(asyncio.get_running_loop().create_task(host.serve(host_side)))
        return client_side

    try:
        async with (
            connect(transport=attach(), redaction=True) as opted,
            connect(transport=attach()) as stock,
        ):
            provider = str(opted.agents()[0]["provider"])
            session = await opted.create_session(provider=provider, cwd=".")
            chat = await session.chat()
            await chat.prompt("first", idle_timeout=10.0)
            stock_session = await stock.open_session(session.uri)
            stock_chat = await stock_session.open_chat(chat.uri)
            await _poll(lambda: len(stock_chat.turns()) == 1)

            seen: list[Any] = []
            stock_actions: list[str] = []

            async def tap_opted() -> None:
                async for event in chat.watch(from_start=False):
                    if isinstance(event, PartRedacted):
                        seen.append(event)

            async def tap_stock() -> None:
                async for tagged in stock.events():
                    # client.events() wraps each ActionEvent in a ClientEvent.
                    action = getattr(tagged.event, "action", None)
                    if isinstance(action, dict):
                        stock_actions.append(str(action.get("type")))

            taps = [asyncio.ensure_future(tap_opted()), asyncio.ensure_future(tap_stock())]

            await chat.prompt("the secret is PLAINLEAK-4242", idle_timeout=10.0)
            await _poll(lambda: len(stock_chat.turns()) == 2)
            turn = chat.turns()[-1]
            part = next(
                p for p in turn["responseParts"] if "PLAINLEAK" in json.dumps(p) and part_id_of(p)
            )
            part_id = part_id_of(part)
            assert part_id is not None

            await host.redact(chat.uri, str(turn["id"]), part_id, "leaked", actor="ops")

            # The user message also carries the secret and is not redacted here, so
            # look at the response parts only.
            await _poll(lambda: "PLAINLEAK" not in json.dumps(chat.turns()[-1]["responseParts"]))
            await _poll(lambda: bool(seen))
            assert seen[0].part_id == part_id
            assert seen[0].reason == "leaked"

            await asyncio.sleep(0.2)
            assert stock_actions, "the stock tap saw nothing, so the next check would be vacuous"
            assert not [t for t in stock_actions if t.startswith("ahp-py/")]
            assert "PLAINLEAK" in json.dumps(stock_chat.turns()[-1]["responseParts"])

            for tap in taps:
                tap.cancel()
            for tap in taps:
                with contextlib.suppress(asyncio.CancelledError):
                    await tap
    finally:
        for task in served:
            task.cancel()
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await task
