"""`TurnSink.content_part`: content the agent shows, as a `contentRef` response part.

The bytes go in the session's content store, which `resourceRead` serves under
the session's visibility; the part carries the `ContentRef` a client fetches.
"""

from __future__ import annotations

import base64
from typing import Any

import pytest
from ahp_protocol.channels import ROOT_URI

from ahp_host.core import Host, LoopbackSingleUserPolicy
from ahp_host.core.changesets import DEFAULT_MAX_BLOB
from ahp_host.provider import EchoProvider, EchoSession
from ahp_host.provider.base import AgentSessionContext, TurnSink, UserMessage

from .hosting import assert_frames_valid, connect, finished, open_session, shut, state, turn_started

pytestmark = pytest.mark.anyio

_PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256))


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class Showing(EchoSession):
    refused: str | None = None

    async def send_user_message(self, message: UserMessage, sink: TurnSink) -> None:
        await sink.text_delta("Here it is:")
        await sink.content_part(_PNG, content_type="image/png")
        await sink.text_delta("Done.")
        try:
            await sink.content_part(b"x" * (DEFAULT_MAX_BLOB + 1), content_type="image/png")
        except ValueError as exc:
            Showing.refused = str(exc)


class ShowingProvider(EchoProvider):
    async def create_session(self, context: AgentSessionContext) -> Showing:
        return Showing(context)


def _parts(host: Host, chat: str) -> list[dict[str, Any]]:
    turns = state(host, chat).get("turns") or [{}]
    return list(turns[-1].get("responseParts", []))


async def test_an_image_is_a_content_ref_part_a_client_can_read() -> None:
    host = Host(ShowingProvider(), LoopbackSingleUserPolicy())
    wire, serving = await connect(host)
    try:
        chat = await open_session(wire, "echo:/show-1")
        await wire.dispatch(chat, turn_started("t1"))
        assert await wire.until(lambda: finished(host, chat, "t1"))

        parts = _parts(host, chat)
        assert [p["kind"] for p in parts] == ["markdown", "contentRef", "markdown"]
        ref = parts[1]
        assert "id" not in ref, "ResourceResponsePart has no id"
        assert ref["contentType"] == "image/png"
        assert ref["sizeHint"] == len(_PNG)

        response = await wire.request(
            "resourceRead", {"channel": ROOT_URI, "uri": ref["uri"], "encoding": "base64"}
        )
        assert base64.b64decode(response["result"]["data"]) == _PNG

        # Too big to store whole: refused, not truncated, and nothing published.
        assert Showing.refused is not None
        assert_frames_valid(wire, ("chat", chat))
    finally:
        await shut(host, wire, serving)


class Pointing(EchoSession):
    async def send_user_message(self, message: UserMessage, sink: TurnSink) -> None:
        await sink.text_delta("Rendered:")
        await sink.content_ref(
            "file:///work/out/image.png", content_type="image/png", size_hint=1234, nonce="v2"
        )
        await sink.content_ref("file:///work/out/plain.png", content_type="image/png")
        await sink.text_delta("Done.")


class PointingProvider(EchoProvider):
    async def create_session(self, context: AgentSessionContext) -> Pointing:
        return Pointing(context)


async def test_a_ref_to_existing_content_names_it_as_given() -> None:
    host = Host(PointingProvider(), LoopbackSingleUserPolicy())
    wire, serving = await connect(host)
    try:
        chat = await open_session(wire, "echo:/point-1")
        await wire.dispatch(chat, turn_started("t1"))
        assert await wire.until(lambda: finished(host, chat, "t1"))

        parts = _parts(host, chat)
        assert [p["kind"] for p in parts] == ["markdown", "contentRef", "contentRef", "markdown"]
        assert parts[1] == {
            "kind": "contentRef",
            "uri": "file:///work/out/image.png",
            "contentType": "image/png",
            "sizeHint": 1234,
            "nonce": "v2",
        }
        # Optional fields are left out rather than sent as null.
        assert parts[2] == {
            "kind": "contentRef",
            "uri": "file:///work/out/plain.png",
            "contentType": "image/png",
        }
        assert parts[3]["content"] == "Done."
        assert_frames_valid(wire, ("chat", chat))
    finally:
        await shut(host, wire, serving)
