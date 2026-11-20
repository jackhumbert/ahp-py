"""Images an agent shows in its reply: `ahp_host_acp.shown`, and through a real agent process."""

from __future__ import annotations

import base64
from pathlib import Path

from ahp_host.provider.base import UserMessage

from ahp_host_acp.provider import AcpProvider, AgentSpec
from ahp_host_acp.shown import MAX_LINKED_IMAGE, show_block

from .fakes import FAKE_AGENT, RecordingSink, context

PNG = b"\x89PNG\r\n\x1a\nfake"


async def test_a_link_to_an_image_file_is_shown_by_reference(tmp_path: Path) -> None:
    picture = tmp_path / "render.png"
    picture.write_bytes(PNG)
    sink = RecordingSink()
    block = {
        "type": "resource_link",
        "uri": picture.as_uri(),
        "name": "render.png",
        "mimeType": "image/png",
    }
    assert await show_block(block, sink)
    stat = picture.stat()
    assert sink.events == [
        ("ref", picture.as_uri(), "image/png", len(PNG), f"{stat.st_mtime_ns:x}-{stat.st_size:x}")
    ]


async def test_the_nonce_changes_when_the_file_is_written_again(tmp_path: Path) -> None:
    picture = tmp_path / "latest.png"
    picture.write_bytes(PNG)
    block = {
        "type": "resource_link",
        "uri": picture.as_uri(),
        "name": "latest.png",
        "mimeType": "image/png",
    }
    first, second = RecordingSink(), RecordingSink()
    await show_block(block, first)
    picture.write_bytes(PNG + b"more")
    await show_block(block, second)
    assert first.events[0][1] == second.events[0][1]
    assert first.events[0][4] != second.events[0][4]


async def test_inline_image_bytes_go_to_the_content_store() -> None:
    sink = RecordingSink()
    block = {"type": "image", "data": base64.b64encode(PNG).decode(), "mimeType": "image/png"}
    assert await show_block(block, sink)
    assert sink.events == [("content", PNG, "image/png")]


async def test_what_cannot_be_shown_is_named_instead(tmp_path: Path) -> None:
    big = tmp_path / "big.png"
    with big.open("wb") as handle:
        handle.truncate(MAX_LINKED_IMAGE + 1)
    cases = [
        {
            "type": "resource_link",
            "uri": (tmp_path / "gone.png").as_uri(),
            "name": "gone.png",
            "mimeType": "image/png",
        },
        {"type": "resource_link", "uri": big.as_uri(), "name": "big.png", "mimeType": "image/png"},
        {"type": "image", "data": "not base64!", "mimeType": "image/png"},
    ]
    sink = RecordingSink()
    for block in cases:
        assert await show_block(block, sink)
    said = [event[1] for event in sink.events]
    assert [event[0] for event in sink.events] == ["text", "text", "text"]
    assert "image not found: gone.png" in said[0]
    assert "too large" in said[1]
    assert "big.png" in said[1]
    assert "not shown" in said[2]


async def test_other_blocks_are_left_alone(tmp_path: Path) -> None:
    sink = RecordingSink()
    for block in (
        {
            "type": "resource_link",
            "uri": (tmp_path / "notes.txt").as_uri(),
            "name": "notes.txt",
            "mimeType": "text/plain",
        },
        {
            "type": "resource_link",
            "uri": "https://example.com/a.png",
            "name": "a.png",
            "mimeType": "image/png",
        },
        {"type": "audio", "data": "AAAA", "mimeType": "audio/wav"},
    ):
        assert not await show_block(block, sink)
    assert sink.events == []


async def test_an_agents_images_reach_the_sink_in_order(tmp_path: Path) -> None:
    provider = AcpProvider(
        tmp_path, AgentSpec(FAKE_AGENT, env={"FAKE_ACP_LOG": str(tmp_path / "agent.log")})
    )
    session = await provider.create_session(context(tmp_path))
    sink = RecordingSink()
    try:
        await session.send_user_message(UserMessage(text="image"), sink)
    finally:
        await session.aclose()
    picture = (tmp_path / "render.png").resolve()
    kinds = [event[0] for event in sink.events]
    assert kinds == ["text", "ref", "content", "text", "text"], sink.events
    assert sink.events[1][1:4] == (picture.as_uri(), "image/png", len(PNG))
    assert sink.events[2] == ("content", PNG, "image/png")
    assert "image not found: gone.png" in sink.events[3][1]
    assert sink.events[4] == ("text", "Done.")
