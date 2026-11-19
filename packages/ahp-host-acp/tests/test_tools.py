from __future__ import annotations

from ahp_host_acp.tools import MAX_RESULT_IMAGE, ToolCall, text_of


def _call(**update: object) -> ToolCall:
    call = ToolCall("c1")
    call.merge(update)
    return call


def test_kindless_shell_call_reads_as_a_command() -> None:
    # goose sends no kind: {"title": "shell · echo hi", "rawInput": {"command": ...}}
    call = _call(title="shell · echo hi", rawInput={"command": "echo hi"})
    assert call.kind == "execute"
    assert call.display_name == "Run command"
    assert call.approval_line() == "echo hi"
    call.merge({"status": "completed"})
    assert call.past_tense() == "Ran `echo hi`"


def test_kindless_write_reads_as_an_edit() -> None:
    call = _call(title="write · a.txt", rawInput={"path": "sub/a.txt", "content": "x"})
    assert call.kind == "edit"
    call.merge({"status": "completed"})
    assert call.past_tense() == "Edited a.txt"


def test_an_explicit_kind_is_kept() -> None:
    assert _call(kind="read", rawInput={"command": "cat a"}).kind == "read"


def test_unknown_shapes_stay_other() -> None:
    call = _call(title="memory · remember", rawInput={"text": "x"})
    assert call.kind == "other"
    assert call.display_name == "memory · remember"


def _block(**block: object) -> dict[str, object]:
    return {"type": "content", "content": block}


def test_an_image_block_is_an_embedded_resource() -> None:
    # `ToolResultEmbeddedResourceContent`: the literal keys a client decodes.
    image = _block(type="image", data="aGk=", mimeType="image/png")
    parts = text_of([_block(type="text", text="1 image"), image])
    assert parts == [
        {"type": "text", "text": "1 image"},
        {"type": "embeddedResource", "data": "aGk=", "contentType": "image/png"},
    ]


def test_an_oversized_image_is_named_not_sent() -> None:
    parts = text_of([_block(type="image", data="A" * (MAX_RESULT_IMAGE + 4), mimeType="image/png")])
    assert parts == [{"type": "text", "text": "[image/png image left out: 1536 KiB]"}]


def test_an_image_block_without_image_data_is_dropped() -> None:
    assert text_of([_block(type="image", data="", mimeType="image/png")]) == []
    assert text_of([_block(type="image", data="aGk=", mimeType="text/plain")]) == []
    assert text_of([_block(type="image", data="aGk=")]) == []
