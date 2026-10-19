"""Waiting entries on step descriptors and ``RunView.waiting``, exactly."""

from __future__ import annotations

from typing import Any

import jsonschema
import pytest

import ahp_protocol.ext.flow as flow
from ahp_protocol.ext.flow import DESCRIPTOR_SCHEMA, WaitingView

VALIDATOR = jsonschema.Draft202012Validator(DESCRIPTOR_SCHEMA)


def started(ordinal: int) -> dict[str, Any]:
    return flow.step_started(
        ordinal=ordinal,
        label=f"s{ordinal}",
        phase=None,
        chat=f"chat:/w{ordinal}",
        model="m",
        provider="echo",
    )


def raw_call(tcid: str, descriptor: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": "toolCall",
        "toolCall": {"toolCallId": tcid, "status": "running", "_meta": flow.meta(descriptor)},
    }


def run_state(*calls: dict[str, Any]) -> dict[str, Any]:
    head = {
        "kind": "systemNotification",
        "content": "text",
        "_meta": flow.meta(flow.run_started("r1", "demo")),
    }
    return {"activeTurn": {"id": "t1", "state": "active", "responseParts": [head, *calls]}}


def test_flowwaiting_builder_shapes() -> None:
    s = started(1)
    assert flow.step(s) == {"stepStarted": s}
    assert flow.step(s, waiting=()) == {"stepStarted": s}
    assert flow.step(s, awaiting=True) == {"stepStarted": s, "awaitingInput": True}
    assert flow.step(s, awaiting=True, waiting=()) == {"stepStarted": s, "awaitingInput": True}
    entries: list[dict[str, Any]] = [
        {"kind": "toolConfirmation", "chat": "chat:/a", "extra": 1},
        {"kind": "input"},
    ]
    assert flow.step(s, waiting=entries) == {
        "stepStarted": s,
        "awaitingInput": True,
        "waiting": [{"kind": "toolConfirmation", "chat": "chat:/a"}, {"kind": "input"}],
    }
    with pytest.raises(ValueError, match="waiting entries need a string kind"):
        flow.step(s, waiting=[{"chat": "chat:/a"}])


def test_flowwaiting_builder_validates_against_schema() -> None:
    s = started(1)
    VALIDATOR.validate(flow.step(s, waiting=[{"kind": "k", "chat": "chat:/a"}]))
    bad = flow.step(s, waiting=[{"kind": "k"}])
    bad["waiting"][0]["extra"] = 1
    assert not VALIDATOR.is_valid(bad)


def test_flowwaiting_runview_lists_two_steps() -> None:
    first = flow.step(started(1), waiting=[{"kind": "toolConfirmation", "chat": "chat:/a"}])
    second = flow.step(started(2), waiting=[{"kind": "toolConfirmation", "chat": "chat:/b"}])
    state = run_state(raw_call("c2", second), raw_call("c1", first))
    assert flow.run_view(state).waiting == (
        WaitingView(1, "c1", "toolConfirmation", "chat:/a"),
        WaitingView(2, "c2", "toolConfirmation", "chat:/b"),
    )
    assert flow.EMPTY_RUN.waiting == ()


def test_flowwaiting_legacy_awaiting_has_unknown_kind() -> None:
    state = run_state(raw_call("c1", flow.step(started(1), awaiting=True)))
    assert flow.run_view(state).waiting == (WaitingView(1, "c1", None, None),)


def test_flowwaiting_finished_step_not_waiting() -> None:
    done = flow.step_finished(status="ok", result="x", input_tokens=1, output_tokens=1)
    d = flow.step(started(1), done, waiting=[{"kind": "k"}])
    view = flow.run_view(run_state(raw_call("c1", d)))
    assert view.waiting == ()
    assert view.steps[0].waiting == ()


def test_flowwaiting_garbage_ignored() -> None:
    for garbage in (5, [1, {"kind": 2}]):
        d = flow.step(started(1), awaiting=True)
        d["waiting"] = garbage
        view = flow.run_view(run_state(raw_call("c1", d)))
        assert view.steps[0].waiting == ()
        assert view.waiting == (WaitingView(1, "c1", None, None),)
