from __future__ import annotations

from agent_host_server_acp.tools import ToolCall


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
