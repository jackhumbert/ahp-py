"""Matching a restored session's open requests, and the notice that withdraws them."""

from __future__ import annotations

from typing import Any

from ahp_host.core.host import _failed_by_restart
from ahp_host.core.restart import (
    RESTART_META_KEY,
    RestoredInputRequest,
    restored_requests,
    withdrawn_notice,
    withdrawn_text,
)


def _confirm_entry(request_id: str = "confirm-3") -> dict[str, Any]:
    return {
        "kind": "toolConfirmation",
        "id": request_id,
        "chat": "c",
        "turnId": "t1",
        "toolCall": {"toolCallId": "call-1", "displayName": "Echo Tool"},
    }


def _input_entry(request_id: str = "input-4") -> dict[str, Any]:
    return {
        "kind": "chatInput",
        "id": request_id,
        "chat": "c",
        "request": {"id": request_id, "message": "Which style?"},
    }


def test_a_stored_record_is_matched_to_its_entry() -> None:
    records = [
        {"id": "confirm-3", "kind": "confirm", "chat": "c", "turnId": "t1", "key": "call-1"},
        {"id": "input-4", "kind": "input", "chat": "c", "turnId": "t1", "key": "input:q"},
    ]
    found = restored_requests(records, [_confirm_entry(), _input_entry()], turns={"c": "t1"})
    assert [(r.id, r.kind, r.chat_uri, r.turn_id, r.key) for r in found] == [
        ("confirm-3", "confirm", "c", "t1", "call-1"),
        # The sink's `input:` namespace is taken off: this is the provider's key.
        ("input-4", "input", "c", "t1", "q"),
    ]


def test_a_record_whose_entry_is_gone_was_answered_and_is_dropped() -> None:
    records = [{"id": "confirm-9", "kind": "confirm", "chat": "c", "turnId": "t1"}]
    assert restored_requests(records, [], turns={"c": "t1"}) == []


def test_an_entry_without_a_record_is_still_withdrawn() -> None:
    """A store written before records were kept: the entry alone says enough."""
    (found,) = restored_requests(None, [_input_entry()], turns={"c": "t7"})
    assert (found.id, found.kind, found.chat_uri, found.turn_id) == ("input-4", "input", "c", "t7")


def test_an_mcp_entry_is_not_a_turns_request() -> None:
    entry = {"kind": "toolAuthentication", "id": "mcp:github", "chat": "c"}
    assert restored_requests(None, [entry], turns={"c": "t1"}) == []


def test_malformed_input_is_skipped_not_raised() -> None:
    bad: Any = [1, {"id": 2}]
    assert restored_requests(bad, "nope", turns={}) == []
    assert restored_requests(None, [1, {"no": "id"}], turns={}) == []


def test_the_notice_names_each_request() -> None:
    found = restored_requests(None, [_confirm_entry(), _input_entry()], turns={"c": "t1"})
    assert withdrawn_text(found) == (
        "Withdrawn after a host restart, because nothing can answer them any more: "
        'approval of "Echo Tool"; question "Which style?".'
    )
    notice = withdrawn_notice(found)
    assert notice["kind"] == "systemNotification"
    assert notice["_meta"] == {
        RESTART_META_KEY: {"event": "inputWithdrawn", "requests": ["confirm-3", "input-4"]}
    }


def test_the_notice_goes_just_before_the_restart_error() -> None:
    request = RestoredInputRequest("confirm-3", "confirm", "c", "t1", "call-1", _confirm_entry())
    state = {
        "activeTurn": {
            "id": "t1",
            "startedAt": "1970-01-01T00:00:01.000Z",
            "message": {"text": "hi", "origin": {"kind": "user"}},
            "responseParts": [{"kind": "markdown", "id": "m1", "content": "partial"}],
        },
        "turns": [],
    }
    ended = _failed_by_restart(
        state, state["activeTurn"], resumable=True, notice=withdrawn_notice([request])
    )
    assert ended.get("activeTurn") is None
    parts = ended["turns"][-1]["responseParts"]
    assert [p["kind"] for p in parts] == ["markdown", "systemNotification", "error"]
    assert parts[-1]["error"]["errorType"] == "host.restart"
