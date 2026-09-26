"""Completions and the OTLP telemetry channel.

Two small surfaces with one sharp edge each.

Completions: `offset` is in **UTF-16 code units**, which is the protocol's unit
and not Python's. Once an emoji is in the text a Python string index is a
different number, and slicing by the wrong one silently completes against the
wrong prefix -- a bug that is invisible in ASCII and wrong everywhere else.

Telemetry: a pass-through. The payload is OTLP/JSON verbatim, so this host owes
no OTLP implementation and must not corrupt one.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from ahp_protocol.channels import ROOT_URI
from ahp_protocol.transport import memory_pair

from ahp_host.core import Host, LoopbackSingleUserPolicy
from ahp_host.provider import EchoProvider
from ahp_host.provider.base import CompletionRequest

from .test_host_end_to_end import FakeClient

pytestmark = pytest.mark.anyio

_TELEMETRY = {"logs": "ahp-otlp://logs", "traces": "ahp-otlp://traces"}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def host() -> AsyncIterator[Host]:
    host = Host(EchoProvider(), LoopbackSingleUserPolicy(), telemetry=_TELEMETRY)
    try:
        yield host
    finally:
        await host.aclose()


async def _client(host: Host) -> FakeClient:
    client_transport, server_transport = memory_pair()
    task = asyncio.create_task(host.serve(server_transport))
    client = FakeClient(client_transport)
    client.serve_task = task  # type: ignore[attr-defined]
    return client


async def _ready(host: Host, uri: str) -> tuple[FakeClient, str]:
    client = await _client(host)
    await client.request(
        "initialize",
        {
            "channel": ROOT_URI,
            "clientId": "c1",
            "protocolVersions": ["0.7.0"],
            "initialSubscriptions": [ROOT_URI],
        },
    )
    await client.request("createSession", {"channel": uri, "provider": "echo"})
    await client.collect(seconds=0.3)
    state = (await client.request("subscribe", {"channel": uri}))["result"]["snapshot"]["state"]
    return client, state["chats"][0]["resource"]


class TestOffsetConversion:
    def test_the_offset_is_utf16_code_units_not_python_indices(self) -> None:
        """An emoji is one Python character and TWO UTF-16 code units. A host
        that slices by the Python index completes against the wrong prefix, and
        the bug is invisible in ASCII."""
        request = CompletionRequest(kind="file", chat="c", text="🙂 #re", offset=5)
        # UTF-16: [🙂 (2)] [space (1)] [# (1)] [r (1)] = offset 5 lands after 'r'.
        assert request.text_before_cursor() == "🙂 #r"

    def test_ascii_is_unaffected(self) -> None:
        request = CompletionRequest(kind="file", chat="c", text="hello #re", offset=8)
        assert request.text_before_cursor() == "hello #r"

    def test_an_offset_past_the_end_is_clamped(self) -> None:
        request = CompletionRequest(kind="file", chat="c", text="hi", offset=999)
        assert request.text_before_cursor() == "hi"


class TestCompletions:
    async def test_items_come_back_for_a_chat_uri(self, host: Host) -> None:
        client, chat = await _ready(host, "echo:/cmp-1")
        result = (
            await client.request(
                "completions",
                {"channel": chat, "kind": "file", "text": "look at #re", "offset": 11},
            )
        )["result"]
        assert [i["attachment"]["label"] for i in result["items"]] == ["readme.md", "recipe.txt"]
        assert result["items"][0]["rangeStart"] == 8

    async def test_a_session_uri_is_accepted_too(self, host: Host) -> None:
        """Documented as "the chat URI", but VS Code sends the SESSION URI while
        upstream's own e2e suite sends a chat URI -- and the reference host
        accepts both. Being strict would break the one client that exists."""
        client, _chat = await _ready(host, "echo:/cmp-2")
        result = (
            await client.request(
                "completions",
                {"channel": "echo:/cmp-2", "kind": "file", "text": "#rec", "offset": 4},
            )
        )["result"]
        assert [i["attachment"]["label"] for i in result["items"]] == ["recipe.txt"]

    async def test_a_provider_with_no_completions_returns_an_empty_list(self) -> None:
        """Not a refusal: an empty picker and a broken host must look different."""

        class Bare(EchoProvider):
            complete = None  # type: ignore[assignment]

        bare = Host(Bare(), LoopbackSingleUserPolicy())
        try:
            client, chat = await _ready(bare, "echo:/cmp-3")
            result = (
                await client.request(
                    "completions", {"channel": chat, "kind": "file", "text": "#", "offset": 1}
                )
            )["result"]
            assert result == {"items": []}
        finally:
            await bare.aclose()

    async def test_an_unknown_channel_is_refused(self, host: Host) -> None:
        client, _ = await _ready(host, "echo:/cmp-4")
        response = await client.request(
            "completions", {"channel": "echo:/nope", "kind": "file", "text": "#", "offset": 1}
        )
        assert response["error"]["code"] == -32602


class TestTelemetry:
    async def test_the_signals_are_advertised_on_initialize(self, host: Host) -> None:
        client = await _client(host)
        result = (
            await client.request(
                "initialize",
                {"channel": ROOT_URI, "clientId": "c", "protocolVersions": ["0.7.0"]},
            )
        )["result"]
        assert result["telemetry"] == _TELEMETRY

    async def test_a_host_emitting_nothing_advertises_nothing(self) -> None:
        """ "A host that emits no telemetry at all omits `telemetry` entirely."
        Advertising a channel nothing publishes to is worse than advertising
        none."""
        quiet = Host(EchoProvider(), LoopbackSingleUserPolicy())
        try:
            client = await _client(quiet)
            result = (
                await client.request(
                    "initialize",
                    {"channel": ROOT_URI, "clientId": "c", "protocolVersions": ["0.7.0"]},
                )
            )["result"]
            assert "telemetry" not in result
        finally:
            await quiet.aclose()

    async def test_a_payload_arrives_verbatim(self, host: Host) -> None:
        """A pass-through: nothing here parses, validates or re-encodes OTLP, so
        this host owes no OTLP implementation and cannot corrupt one."""
        client = await _client(host)
        await client.request(
            "initialize",
            {"channel": ROOT_URI, "clientId": "c", "protocolVersions": ["0.7.0"]},
        )
        await client.request("subscribe", {"channel": "ahp-otlp://logs"})

        payload: dict[str, Any] = {
            "resourceLogs": [{"scopeLogs": [{"logRecords": [{"severityText": "INFO"}]}]}]
        }
        await host.emit_telemetry("logs", payload)
        await client.collect(seconds=0.3)

        frames = [n for n in client.notifications if n.get("method") == "otlp/exportLogs"]
        assert frames
        assert frames[-1]["params"]["channel"] == "ahp-otlp://logs"
        assert frames[-1]["params"]["payload"] == payload

    async def test_a_stateless_channel_subscribes_without_a_snapshot(self, host: Host) -> None:
        """ "`snapshot` is present when the subscribed channel has associated
        state, and absent for stateless channels.\""""
        client = await _client(host)
        await client.request(
            "initialize",
            {"channel": ROOT_URI, "clientId": "c", "protocolVersions": ["0.7.0"]},
        )
        result = (await client.request("subscribe", {"channel": "ahp-otlp://traces"}))["result"]
        assert result == {}

    async def test_an_unadvertised_signal_is_dropped(self, host: Host) -> None:
        """A client that never saw the channel on `initialize` has not
        subscribed to it."""
        client = await _client(host)
        await client.request(
            "initialize",
            {"channel": ROOT_URI, "clientId": "c", "protocolVersions": ["0.7.0"]},
        )
        await host.emit_telemetry("metrics", {"resourceMetrics": []})
        await client.collect(seconds=0.2)
        assert not [n for n in client.notifications if n.get("method") == "otlp/exportMetrics"]

    async def test_telemetry_is_not_replayed_on_reconnect(self, host: Host) -> None:
        """ "Telemetry is not replayed on reconnect." It is a notification, so it
        never touches the replay log at all."""
        client = await _client(host)
        await client.request(
            "initialize",
            {"channel": ROOT_URI, "clientId": "c", "protocolVersions": ["0.7.0"]},
        )
        await client.request("subscribe", {"channel": "ahp-otlp://logs"})
        await host.emit_telemetry("logs", {"resourceLogs": []})
        await client.collect(seconds=0.2)

        result = (
            await client.request(
                "reconnect",
                {
                    "clientId": "c",
                    "lastSeenServerSeq": 0,
                    "subscriptions": ["ahp-otlp://logs"],
                },
            )
        )["result"]
        assert not result.get("actions")


class TestCompletionShape:
    """The wire shape a client actually reads.

    `label`/`detail` on the item were this project's invention -- not in
    `CompletionItem` (channels-session/commands.ts:266-297) and read by
    nothing. The display name lives on the attachment, where the spec makes it
    required. The discriminant is `type` from MessageAttachmentKind, not
    `kind`, and the shipping client's switch ends in a bare `default: return`,
    so the wrong key dropped every suggestion in silence.
    """

    async def test_items_carry_a_spec_shaped_attachment(self, host: Host) -> None:
        client, _chat = await _ready(host, "echo:/cmp-shape")
        result = (
            await client.request(
                "completions",
                {"channel": "echo:/cmp-shape", "kind": "file", "text": "#re", "offset": 3},
            )
        )["result"]

        assert result["items"], "nothing to check"
        for item in result["items"]:
            attachment = item["attachment"]
            assert attachment["type"] == "resource"
            assert "kind" not in attachment
            assert isinstance(attachment["label"], str)
            assert attachment["label"]
            # Ours, and not the spec's. Their presence meant a blank picker.
            assert "label" not in item
            assert "detail" not in item

    async def test_the_trigger_characters_are_advertised(self) -> None:
        """Otherwise `completions` is implemented and never called.

        A client issues the request only for a character the host named, so an
        unadvertised trigger leaves a complete implementation unreachable.
        """
        assert await _initialize_result(
            Host(
                EchoProvider(),
                LoopbackSingleUserPolicy(),
                completion_trigger_characters=("#",),
            )
        ) == ["#"]

    async def test_a_provider_without_completions_advertises_nothing(self) -> None:
        """A trigger character is a promise. Do not make one we cannot keep.

        An advertised trigger a provider ignores opens an empty picker on every
        keystroke, which reads as a broken host rather than as no results.
        """

        class Bare(EchoProvider):
            complete = None  # type: ignore[assignment]

        assert (
            await _initialize_result(
                Host(
                    Bare(),
                    LoopbackSingleUserPolicy(),
                    completion_trigger_characters=("#",),
                )
            )
            is None
        )

    async def test_the_terminal_prefix_is_absent_without_a_backend(self) -> None:
        """ "Absence means the host does not support command prefixes."

        With the refusing default backend, advertising `!` would make `!ls`
        render as a terminal request the host then declines -- a working input
        turned into a dead end.
        """
        host = Host(EchoProvider(), LoopbackSingleUserPolicy())
        try:
            client = await _client(host)
            result = (await client.request("initialize", _INIT))["result"]
            assert "terminalCommandPrefix" not in result
        finally:
            await host.aclose()


_INIT = {
    "channel": ROOT_URI,
    "protocolVersions": ["0.7.0"],
    "clientInfo": {"name": "shape", "version": "0"},
    "capabilities": {},
}


async def _initialize_result(host: Host) -> list[str] | None:
    """`completionTriggerCharacters` from a fresh host, or ``None`` if absent."""
    try:
        client = await _client(host)
        result = (await client.request("initialize", _INIT))["result"]
        value = result.get("completionTriggerCharacters")
        return list(value) if value is not None else None
    finally:
        await host.aclose()
