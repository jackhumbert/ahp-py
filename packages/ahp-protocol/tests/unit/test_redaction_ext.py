"""The redaction extension: marker shape, allowlist, purity and no-op targets."""

from __future__ import annotations

import copy
from typing import Any

import pytest

import ahp_protocol.ext.flow as flow
from ahp_protocol.ext.redaction import (
    MESSAGE_REDACTED,
    PART_REDACTED,
    REDACTED_TEXT,
    REDACTION_CAPABILITY,
    REDACTION_META_KEY,
    apply_redaction,
    find_part,
    find_turn,
    is_redaction_action,
    marker_of,
    message_redacted,
    part_redacted,
)
from ahp_protocol.reducers import REDUCERS
from tests.conformance.schemas import assert_valid_state

SECRET = "PLAINLEAK-4242"
WHEN = "2026-01-01T00:00:00.000Z"


def chat(turns: list[dict[str, Any]], active: dict[str, Any] | None = None) -> dict[str, Any]:
    state: dict[str, Any] = {
        "resource": "chat:/c1",
        "title": "t",
        "status": 1,  # ChatStatus.Idle (a bit flag, not a string)
        "modifiedAt": WHEN,
        "turns": turns,
    }
    if active is not None:
        state["activeTurn"] = active
    return state


def turn(parts: list[dict[str, Any]], tid: str = "t1", **message: Any) -> dict[str, Any]:
    return {
        "id": tid,
        "message": {"text": f"hello {SECRET}", "origin": {"kind": "user"}, **message},
        "responseParts": parts,
        "state": "complete",
    }


def active_turn(parts: list[dict[str, Any]], tid: str = "t2") -> dict[str, Any]:
    return {
        "id": tid,
        "startedAt": WHEN,
        "message": {"text": "go", "origin": {"kind": "user"}},
        "responseParts": parts,
    }


def markdown(pid: str = "p1") -> dict[str, Any]:
    return {"kind": "markdown", "id": pid, "content": f"token={SECRET}"}


def completed_call(**extra: Any) -> dict[str, Any]:
    call: dict[str, Any] = {
        "toolCallId": "c0",
        "toolName": "bash",
        "displayName": "Bash",
        "invocationMessage": f"run {SECRET}",
        "toolInput": f'{{"k": "{SECRET}"}}',
        "success": True,
        "pastTenseMessage": f"ran {SECRET}",
        "content": [{"type": "text", "text": SECRET}],
        "structuredContent": {"secret": SECRET},
        "confirmed": "not-needed",
        "status": "completed",
    }
    call.update(extra)
    return {"kind": "toolCall", "toolCall": call}


def test_redaction_ext_constants_and_builders() -> None:
    assert REDACTION_CAPABILITY == "ahp-py/redaction"
    a = part_redacted("t1", "p1", "leak", WHEN)
    assert a == {
        "type": PART_REDACTED,
        "turnId": "t1",
        "partId": "p1",
        "reason": "leak",
        "redactedAt": WHEN,
    }
    assert "meta" not in a
    assert part_redacted("t1", "p1", "leak", WHEN, meta={"x": 1})["meta"] == {"x": 1}
    m = message_redacted("t1", "leak", WHEN)
    assert m["type"] == MESSAGE_REDACTED
    assert is_redaction_action(a)
    assert is_redaction_action(m)
    assert not is_redaction_action({"type": "chat/delta"})
    assert not is_redaction_action("x")


def test_redaction_ext_markdown_in_completed_turn() -> None:
    before = chat([turn([markdown("p0"), markdown("p1"), markdown("p2")])])
    assert_valid_state("chat", before)
    snapshot = copy.deepcopy(before)
    after = apply_redaction(before, part_redacted("t1", "p1", "leaked", WHEN))
    assert before == snapshot
    assert_valid_state("chat", after)
    parts = after["turns"][0]["responseParts"]
    assert [p["id"] for p in parts] == ["p0", "p1", "p2"]
    assert parts[1]["content"] == REDACTED_TEXT
    assert parts[0] == before["turns"][0]["responseParts"][0]
    assert SECRET not in repr(parts[1])
    assert marker_of(parts[1]) == {"reason": "leaked", "redactedAt": WHEN}
    assert find_part(after, "t1", "p1") is parts[1]


def test_redaction_ext_reasoning_in_active_turn() -> None:
    part = {"kind": "reasoning", "id": "r1", "content": SECRET}
    before = chat([], active_turn([part]))
    assert_valid_state("chat", before)
    after = apply_redaction(before, part_redacted("t2", "r1", "why", WHEN))
    assert_valid_state("chat", after)
    new = after["activeTurn"]["responseParts"][0]
    assert new["kind"] == "reasoning"
    assert new["id"] == "r1"
    assert new["content"] == REDACTED_TEXT
    assert SECRET not in repr(new)
    assert find_turn(after, "t2") is after["activeTurn"]


def test_redaction_ext_tool_call_completed_with_flow_descriptor() -> None:
    started = flow.step_started(
        ordinal=0, label="a", phase=None, chat="chat:/w0", model=None, provider="echo"
    )
    desc = flow.step(
        started,
        flow.step_finished(status="ok", result=SECRET, input_tokens=1, output_tokens=2),
    )
    part = completed_call(_meta={flow.FLOW_META_KEY: desc, "other": SECRET})
    note = {
        "kind": "systemNotification",
        "content": "run",
        "_meta": flow.meta(flow.run_started("r1", "d")),
    }
    before = chat([turn([note, part])])
    assert_valid_state("chat", before)
    assert flow.run_view(before).steps[0].result == SECRET
    action = part_redacted("t1", "c0", "leaked", WHEN, meta=flow.meta(flow.redacted_step(desc)))
    after = apply_redaction(before, action)
    assert_valid_state("chat", after)
    new = after["turns"][0]["responseParts"][1]
    assert SECRET not in repr(new)
    call = new["toolCall"]
    assert call["status"] == "completed"
    assert call["success"] is True
    assert call["confirmed"] == "not-needed"
    assert call["invocationMessage"] == REDACTED_TEXT
    assert call["content"] == [{"type": "text", "text": REDACTED_TEXT}]
    assert "structuredContent" not in call
    assert marker_of(new) == {"reason": "leaked", "redactedAt": WHEN}
    assert flow.run_view(after).steps[0].result == REDACTED_TEXT


def test_redaction_ext_tool_call_cancelled_keeps_reason() -> None:
    part = {
        "kind": "toolCall",
        "toolCall": {
            "toolCallId": "c1",
            "toolName": "bash",
            "displayName": "Bash",
            "invocationMessage": f"run {SECRET}",
            "toolInput": SECRET,
            "status": "cancelled",
            "reason": "denied",
            "reasonMessage": SECRET,
        },
    }
    before = chat([turn([part])])
    assert_valid_state("chat", before)
    after = apply_redaction(before, part_redacted("t1", "c1", "r", WHEN))
    assert_valid_state("chat", after)
    call = after["turns"][0]["responseParts"][0]["toolCall"]
    assert call["reason"] == "denied"
    assert call["status"] == "cancelled"
    assert call["reasonMessage"] == REDACTED_TEXT
    assert SECRET not in repr(call)


def test_redaction_ext_message_variant() -> None:
    before = chat(
        [
            turn(
                [markdown()],
                origin={"kind": "user"},
                attachments=[{"type": "simple", "label": SECRET}],
            )
        ]
    )
    assert_valid_state("chat", before)
    after = apply_redaction(before, message_redacted("t1", "pii", WHEN))
    assert_valid_state("chat", after)
    message = after["turns"][0]["message"]
    assert message["text"] == REDACTED_TEXT
    assert message["origin"] == {"kind": "user"}
    assert "attachments" not in message
    assert SECRET not in repr(message)
    assert marker_of(message) == {"reason": "pii", "redactedAt": WHEN}
    assert after["turns"][0]["responseParts"] == before["turns"][0]["responseParts"]


def test_redaction_ext_marker_of_absent() -> None:
    assert marker_of(markdown()) is None
    assert marker_of({"text": "x"}) is None
    assert marker_of(None) is None
    assert marker_of({"kind": "toolCall", "toolCall": 3}) is None


def test_redaction_ext_host_meta_cannot_overwrite_marker() -> None:
    forged = {REDACTION_META_KEY: {"reason": "forged", "redactedAt": "x"}, "keep": 1}
    after = apply_redaction(
        chat([turn([markdown()])]), part_redacted("t1", "p1", "real", WHEN, meta=forged)
    )
    part = after["turns"][0]["responseParts"][0]
    assert marker_of(part) == {"reason": "real", "redactedAt": WHEN}
    assert part["_meta"]["keep"] == 1


@pytest.mark.parametrize(
    ("state", "action"),
    [
        (chat([turn([markdown()])]), part_redacted("nope", "p1", "r", WHEN)),
        (chat([turn([markdown()])]), part_redacted("t1", "nope", "r", WHEN)),
        (
            chat([turn([{"kind": "systemNotification", "content": SECRET, "id": "n1"}])]),
            part_redacted("t1", "n1", "r", WHEN),
        ),
        (
            chat([turn([{"kind": "inputRequest", "id": "i1", "request": {}}])]),
            part_redacted("t1", "i1", "r", WHEN),
        ),
        (chat([turn([markdown()])]), {**part_redacted("t1", "p1", "r", WHEN), "partId": 1}),
        (chat([turn([markdown()])]), {**part_redacted("t1", "p1", "r", WHEN), "reason": None}),
        (chat([turn([markdown()])]), {**part_redacted("t1", "p1", "r", WHEN), "turnId": 1}),
        (chat([turn([markdown()])]), {"type": "ahp-py/chat/other", "turnId": "t1"}),
        (chat([turn([markdown()])]), {"type": "chat/delta", "turnId": "t1"}),
        ({"turns": "x"}, message_redacted("t1", "r", WHEN)),
        (
            {"turns": [{"id": "t1", "message": "str", "responseParts": []}]},
            message_redacted("t1", "r", WHEN),
        ),
        ("not a state", part_redacted("t1", "p1", "r", WHEN)),
        (None, part_redacted("t1", "p1", "r", WHEN)),
    ],
)
def test_redaction_ext_unknown_targets_return_the_input(state: Any, action: Any) -> None:
    assert apply_redaction(state, action) is state


def test_redaction_ext_idempotent() -> None:
    action = part_redacted("t1", "p1", "r", WHEN)
    once = apply_redaction(chat([turn([markdown()])]), action)
    assert apply_redaction(once, action) == once
    msg = message_redacted("t1", "r", WHEN)
    once = apply_redaction(chat([turn([markdown()])]), msg)
    assert apply_redaction(once, msg) == once


def test_redaction_ext_stock_reducer_ignores_the_action() -> None:
    state = chat([turn([markdown()])])
    assert REDUCERS["chat"](state, part_redacted("t1", "p1", "r", WHEN)) == state
    assert REDUCERS["chat"](state, message_redacted("t1", "r", WHEN)) == state


def test_redaction_ext_delta_after_redaction_appends() -> None:
    state = chat([], active_turn([markdown("p1")]))
    state = apply_redaction(state, part_redacted("t2", "p1", "r", WHEN))
    state = REDUCERS["chat"](
        state, {"type": "chat/delta", "turnId": "t2", "partId": "p1", "content": "!"}
    )
    assert state["activeTurn"]["responseParts"][0]["content"] == "[redacted]!"


def test_redaction_ext_flow_redacted_step() -> None:
    started = flow.step_started(
        ordinal=0, label="a", phase="p", chat=None, model=None, provider="echo"
    )
    finished = flow.step_finished(
        status="error", result=SECRET, input_tokens=1, output_tokens=2, error=SECRET
    )
    desc = flow.step(started, finished, awaiting=True)
    out = flow.redacted_step(desc)
    assert SECRET not in repr(out)
    assert out["stepStarted"] == started
    assert out["awaitingInput"] is True
    assert out["stepFinished"]["result"] == REDACTED_TEXT
    assert out["stepFinished"]["error"] == REDACTED_TEXT
    assert out["stepFinished"]["usage"] == finished["usage"]
    assert desc["stepFinished"]["result"] == SECRET
    unfinished = flow.step(started)
    copied = flow.redacted_step(unfinished)
    assert copied == unfinished
    assert copied is not unfinished
