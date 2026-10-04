"""Chat canvases and the `ahp-canvas:` channel (1.0.0, experimental)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from ahp_protocol.channels import ROOT_URI
from ahp_protocol.transport import memory_pair

from ahp_host.core import Host, LoopbackSingleUserPolicy
from ahp_host.core.store import FileSessionStore
from ahp_host.core.wirelog import REDACTED, redact
from ahp_host.provider import EchoProvider
from ahp_host.provider.base import Canvas, SessionPublisher

from .test_host_end_to_end import FakeClient

pytestmark = pytest.mark.anyio

_PREVIEW = Canvas(
    instance_id="preview-1",
    extension_id="project:preview",
    canvas_id="preview",
    title="Preview",
    status="Ready",
    url="https://example.com/live",
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _host(tmp_path: Path | None = None) -> Host:
    extra: dict[str, Any] = {}
    if tmp_path is not None:
        extra = {
            "store": FileSessionStore(tmp_path / "sessions", debounce=0.05),
            "sequence_file": tmp_path / "seq",
        }
    return Host(
        EchoProvider(capabilities={"multipleChats": {}}), LoopbackSingleUserPolicy(), **extra
    )


@pytest.fixture
async def host() -> AsyncIterator[Host]:
    made = _host()
    try:
        yield made
    finally:
        await made.aclose()


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
            "protocolVersions": ["1.0.0"],
            "initialSubscriptions": [ROOT_URI],
        },
    )
    return client


async def _session(host: Host, client: FakeClient, uri: str) -> tuple[str, SessionPublisher]:
    await client.request("createSession", {"channel": uri, "provider": "echo"})
    await client.collect_until(
        lambda: bool((host.sequencer.state_of(uri) or {}).get("chats")), timeout=10.0
    )
    publisher = host._sessions[uri].publisher
    assert publisher is not None
    return host._sessions[uri].chat_uri, publisher


def _references(host: Host, chat: str) -> list[str]:
    state = host.sequencer.state_of(chat) or {}
    return [c["resource"] for c in state.get("canvases") or []]


class TestCanvases:
    async def test_a_canvas_is_referenced_by_its_chat(self, host: Host) -> None:
        client = await _client(host)
        chat, publisher = await _session(host, client, "echo:/cv-1")
        channel = await publisher.canvas_set(_PREVIEW)
        assert channel.startswith("ahp-canvas:/")
        assert _references(host, chat) == [channel]
        snapshot = (await client.request("subscribe", {"channel": channel}))["result"]["snapshot"]
        assert snapshot["state"] == _PREVIEW.to_wire()

    async def test_publishing_again_replaces_the_state(self, host: Host) -> None:
        client = await _client(host)
        chat, publisher = await _session(host, client, "echo:/cv-2")
        channel = await publisher.canvas_set(_PREVIEW)
        await client.request("subscribe", {"channel": channel})
        unavailable = Canvas(
            instance_id="preview-1",
            extension_id="project:preview",
            canvas_id="preview",
            status="Stopped",
        )
        assert await publisher.canvas_set(unavailable) == channel
        assert host.sequencer.state_of(channel) == unavailable.to_wire()
        assert "url" not in (host.sequencer.state_of(channel) or {})
        assert _references(host, chat) == [channel]

    async def test_removing_the_last_canvas_clears_the_list(self, host: Host) -> None:
        client = await _client(host)
        chat, publisher = await _session(host, client, "echo:/cv-3")
        channel = await publisher.canvas_set(_PREVIEW)
        await publisher.canvas_removed("preview-1")
        assert "canvases" not in (host.sequencer.state_of(chat) or {})
        assert host.sequencer.state_of(channel) is None

    async def test_disposing_the_chat_drops_its_canvases(self, host: Host) -> None:
        client = await _client(host)
        _, publisher = await _session(host, client, "echo:/cv-4")
        side = "ahp-chat:/cv-4-side"
        await client.request("createChat", {"channel": "echo:/cv-4", "chat": side})
        channel = await publisher.canvas_set(_PREVIEW, chat=side)
        await client.request("disposeChat", {"channel": side})
        assert host.sequencer.state_of(channel) is None

    def test_a_non_http_url_is_refused(self) -> None:
        with pytest.raises(ValueError, match="HTTP"):
            Canvas(instance_id="i", extension_id="e", canvas_id="c", url="file:///x").to_wire()


def test_a_canvas_url_is_redacted_from_the_wire_log() -> None:
    frame = {
        "method": "action",
        "params": {"action": {"type": "canvas/stateChanged", "canvas": _PREVIEW.to_wire()}},
    }
    logged = redact(frame)
    assert logged["params"]["action"]["canvas"]["url"] == REDACTED
    assert logged["params"]["action"]["canvas"]["title"] == "Preview"
    assert "example.com" not in json.dumps(logged)
    # A URL that is not a canvas source is ordinary data.
    assert redact({"uri": "https://example.com"}) == {"uri": "https://example.com"}


async def test_nothing_about_a_canvas_survives_a_restart(tmp_path: Path) -> None:
    first = _host(tmp_path)
    try:
        client = await _client(first)
        chat, publisher = await _session(first, client, "echo:/cv-restart")
        await publisher.canvas_set(_PREVIEW)
        await first._persist(first._sessions["echo:/cv-restart"])
    finally:
        await first.aclose()
    stored = await FileSessionStore(tmp_path / "sessions").load_all()
    assert "example.com" not in json.dumps([s.to_json() for s in stored])

    second = _host(tmp_path)
    try:
        assert await second.restore() == 1
        assert "canvases" not in (second.sequencer.state_of(chat) or {})
    finally:
        await second.aclose()
