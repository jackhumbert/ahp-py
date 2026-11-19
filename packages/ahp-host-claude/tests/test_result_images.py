"""A tool result's images reach the client as `embeddedResource` parts."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from claude_agent_sdk import AssistantMessage, ToolUseBlock
from claude_agent_sdk import UserMessage as SdkUserMessage
from claude_agent_sdk.types import ToolResultBlock

from ahp_host_claude.provider import MAX_RESULT_IMAGE, _result_content
from tests.test_file_edits import Harness, _result


def _image(data: str, mime: str = "image/png", kind: str = "base64") -> dict[str, Any]:
    return {"type": "image", "source": {"type": kind, "media_type": mime, "data": data}}


def test_an_image_block_is_an_embedded_resource() -> None:
    # `ToolResultEmbeddedResourceContent`: the literal keys a client decodes.
    parts = _result_content("1 image", [{"type": "text", "text": "1 image"}, _image("aGk=")])
    assert parts == [
        {"type": "text", "text": "1 image"},
        {"type": "embeddedResource", "data": "aGk=", "contentType": "image/png"},
    ]


def test_an_oversized_image_is_named_not_sent() -> None:
    parts = _result_content("", [_image("A" * (MAX_RESULT_IMAGE + 4))])
    assert parts[1:] == [{"type": "text", "text": "[image/png image left out: 1536 KiB]"}]


def test_an_image_block_without_image_data_is_dropped() -> None:
    for block in (
        _image(""),
        _image("aGk=", mime="text/plain"),
        _image("aGk=", kind="url"),
        {"type": "image"},
    ):
        assert _result_content("t", [block]) == [{"type": "text", "text": "t"}]
    assert _result_content("t", "t") == [{"type": "text", "text": "t"}]
    assert _result_content("", None) == [{"type": "text", "text": ""}]


async def test_a_read_of_a_picture_shows_it_on_the_call(tmp_path: Path) -> None:
    tool_input = {"file_path": str(tmp_path / "work" / "shot.png")}
    turn = [
        AssistantMessage([ToolUseBlock("r1", "Read", tool_input)], model="m"),
        SdkUserMessage(content=[ToolResultBlock("r1", [_image("aGk=")], False)]),
        _result(),
    ]
    sink = await Harness(tmp_path, turn).run()
    (completed,) = [e for e in sink.events if e[0] == "completed"]
    assert completed[3]["content"][1:] == [
        {"type": "embeddedResource", "data": "aGk=", "contentType": "image/png"}
    ]
