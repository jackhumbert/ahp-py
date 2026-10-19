"""The typed-progress extension: builders, schema and fold, exactly."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest

import ahp_protocol.ext.flow as flow
from ahp_protocol.ext.flow import (
    DESCRIPTOR_SCHEMA,
    EMPTY_RUN,
    PhaseView,
    RunView,
    StepView,
)

VALIDATOR = jsonschema.Draft202012Validator(DESCRIPTOR_SCHEMA)


def note(descriptor: dict[str, Any]) -> dict[str, Any]:
    return {"kind": "systemNotification", "content": "text", "_meta": flow.meta(descriptor)}


def started(ordinal: int, phase: str | None = None, label: str | None = None) -> dict[str, Any]:
    return flow.step_started(
        ordinal=ordinal,
        label=label or f"s{ordinal}",
        phase=phase,
        chat=f"chat:/w{ordinal}",
        model="m",
        provider="echo",
    )


def finished(result: Any, i: int = 1, o: int = 2, status: str = "ok") -> dict[str, Any]:
    return flow.step_finished(status=status, result=result, input_tokens=i, output_tokens=o)


def call(
    tcid: str,
    started_: dict[str, Any],
    finished_: dict[str, Any] | None = None,
    *,
    awaiting: bool = False,
    status: str = "running",
) -> dict[str, Any]:
    return {
        "kind": "toolCall",
        "toolCall": {
            "toolCallId": tcid,
            "status": status,
            "_meta": flow.meta(flow.step(started_, finished_, awaiting=awaiting)),
        },
    }


def turn(parts: list[dict[str, Any]], state: str = "complete", tid: str = "t1") -> dict[str, Any]:
    return {"id": tid, "state": state, "responseParts": parts}


def test_flow_ext_constants() -> None:
    assert flow.FLOW_META_KEY == "ahp-py/flow"
    assert flow.SCHEMA_FILE == "flow.schema.json"
    assert flow.NOTIFICATION_KINDS == ("runStarted", "phaseStarted", "phaseFinished", "runFinished")
    assert flow.STEP_STATUSES == ("ok", "null", "error", "skipped")
    assert flow.RUN_FINISHED_STATUSES == ("ok", "error")


def test_flow_ext_builders_exact() -> None:
    assert flow.run_started("r", "w") == {"runStarted": {"runId": "r", "workflow": "w"}}
    assert flow.run_started(None, None) == {"runStarted": {"runId": None, "workflow": None}}
    assert flow.phase_started("A") == {"phaseStarted": {"title": "A"}}
    assert flow.phase_finished("A") == {"phaseFinished": {"title": "A"}}
    assert flow.run_finished("ok") == {"runFinished": {"status": "ok"}}
    assert flow.run_finished("error", "boom") == {
        "runFinished": {"status": "error", "error": "boom"}
    }
    s = flow.step_started(
        ordinal=3, label="l", phase="p", chat="chat:/c", model="m", provider="echo"
    )
    assert s == {
        "ordinal": 3,
        "label": "l",
        "phase": "p",
        "chat": "chat:/c",
        "model": "m",
        "provider": "echo",
    }
    f = flow.step_finished(status="ok", result={"a": 1}, input_tokens=4, output_tokens=5)
    assert f == {
        "status": "ok",
        "result": {"a": 1},
        "usage": {"inputTokens": 4, "outputTokens": 5},
    }
    assert flow.step_finished(
        status="error", result=None, input_tokens=0, output_tokens=0, error="x"
    ) == {
        "status": "error",
        "result": None,
        "usage": {"inputTokens": 0, "outputTokens": 0},
        "error": "x",
    }
    assert flow.step(s) == {"stepStarted": s}
    assert flow.step(s, f) == {"stepStarted": s, "stepFinished": f}
    assert flow.step(s, awaiting=True) == {"stepStarted": s, "awaitingInput": True}
    assert flow.meta({"a": 1}) == {"ahp-py/flow": {"a": 1}}


def test_flow_ext_builders_reject_unknown_status() -> None:
    with pytest.raises(ValueError, match="status must be"):
        flow.run_finished("running")
    with pytest.raises(ValueError, match="status must be"):
        flow.step_finished(status="maybe", result=None, input_tokens=0, output_tokens=0)


def test_flow_ext_descriptor_of() -> None:
    assert flow.descriptor_of({"ahp-py/flow": {"a": 1}}) == {"a": 1}
    assert flow.descriptor_of({"ahp-py/flow": 1}) is None
    assert flow.descriptor_of({}) is None
    assert flow.descriptor_of("x") is None
    assert flow.descriptor_of(None) is None


def test_flow_ext_schema_file_matches_the_constant() -> None:
    path = Path(flow.__file__).with_name(flow.SCHEMA_FILE)
    assert json.loads(path.read_text(encoding="utf-8")) == DESCRIPTOR_SCHEMA


def test_flow_ext_schema_is_a_valid_schema() -> None:
    jsonschema.Draft202012Validator.check_schema(DESCRIPTOR_SCHEMA)


def test_flow_ext_builder_outputs_validate() -> None:
    s = started(0)
    f = finished("r")
    outputs = [
        flow.run_started("r", "w"),
        flow.run_started(None, None),
        flow.phase_started("A"),
        flow.phase_finished("A"),
        flow.run_finished("ok"),
        flow.run_finished("error", "boom"),
        flow.step(s),
        flow.step(s, f),
        flow.step(s, awaiting=True),
        flow.step(s, f, awaiting=True),
        flow.step(
            s, flow.step_finished(status="skipped", result=None, input_tokens=0, output_tokens=0)
        ),
    ]
    for out in outputs:
        VALIDATOR.validate(out)


def test_flow_ext_schema_rejects_malformed() -> None:
    s = started(0)
    bad: list[dict[str, Any]] = [
        {"runStarted": {"runId": None}, "phaseStarted": {"title": "A"}},
        {"stepStarted": s, "stepFinished": {**finished("r"), "status": "maybe"}},
        {"stepFinished": finished("r")},
        {"stepStarted": s, "awaitingInput": False},
    ]
    for doc in bad:
        assert not VALIDATOR.is_valid(doc), doc


def test_flow_ext_empty_state() -> None:
    assert flow.run_view({}) is EMPTY_RUN
    assert flow.run_view({"turns": []}) is EMPTY_RUN
    assert EMPTY_RUN.status == "none"
    assert EMPTY_RUN.steps == ()
    assert flow.run_views({}) == ()


def test_flow_ext_completed_run() -> None:
    parts = [
        note(flow.run_started("r1", "demo")),
        note(flow.phase_started("A")),
        call("c0", started(0, "A"), finished("x", 1, 2), status="completed"),
        call("c1", started(1, "A"), finished(None, 3, 4, "null"), status="completed"),
        note(flow.phase_finished("A")),
        note(flow.phase_started("B")),
        call("c2", started(2, "B"), finished({"k": 1}, 5, 6), status="completed"),
        note(flow.phase_finished("B")),
        note(flow.run_finished("ok")),
    ]
    view = flow.run_view({"turns": [turn(parts)]})

    def sv(o: int, phase: str, result: Any, i: int, out: int, status: str) -> StepView:
        return StepView(
            tool_call_id=f"c{o}",
            ordinal=o,
            label=f"s{o}",
            phase=phase,
            chat=f"chat:/w{o}",
            model="m",
            provider="echo",
            status=status,
            result=result,
            usage={"inputTokens": i, "outputTokens": out},
            error=None,
            awaiting=False,
        )

    s0 = sv(0, "A", "x", 1, 2, "ok")
    s1 = sv(1, "A", None, 3, 4, "null")
    s2 = sv(2, "B", {"k": 1}, 5, 6, "ok")
    assert view == RunView(
        turn_id="t1",
        run_id="r1",
        workflow="demo",
        status="ok",
        error=None,
        phases=(PhaseView("A", "finished", (s0, s1)), PhaseView("B", "finished", (s2,))),
        usage={"inputTokens": 9, "outputTokens": 12},
    )
    assert view.steps == (s0, s1, s2)


def test_flow_ext_active_turn_is_running() -> None:
    parts = [
        note(flow.run_started("r1", None)),
        note(flow.phase_started("A")),
        call("c0", started(0, "A"), finished("x"), status="completed"),
        call("c1", started(1, "A"), awaiting=True),
        call("c2", started(2, "A")),
    ]
    view = flow.run_view({"turns": [], "activeTurn": turn(parts, state="active")})
    assert view.status == "running"
    assert view.phases[0].status == "running"
    assert [s.status for s in view.steps] == ["ok", "running", "running"]
    assert [s.awaiting for s in view.steps] == [False, True, False]
    assert view.usage == {"inputTokens": 1, "outputTokens": 2}


def test_flow_ext_awaiting_ignored_once_finished() -> None:
    parts = [
        note(flow.run_started(None, None)),
        call("c0", started(0), finished("x"), awaiting=True),
    ]
    view = flow.run_view({"activeTurn": turn(parts, state="active")})
    assert view.steps[0].awaiting is False


def test_flow_ext_cancelled_turn_without_run_finished() -> None:
    parts = [
        note(flow.run_started("r1", "demo")),
        note(flow.phase_started("A")),
        call("c0", started(0, "A")),
    ]
    view = flow.run_view({"turns": [turn(parts, state="cancelled")]})
    assert view.status == "cancelled"
    assert view.steps[0].status == "cancelled"
    assert view.phases == (PhaseView("A", "finished", view.steps),)


def test_flow_ext_turn_error_state_without_run_finished() -> None:
    parts = [note(flow.run_started("r1", None))]
    assert flow.run_view({"turns": [turn(parts, state="error")]}).status == "error"


def test_flow_ext_run_finished_error() -> None:
    parts = [note(flow.run_started("r1", None)), note(flow.run_finished("error", "boom"))]
    view = flow.run_view({"turns": [turn(parts)]})
    assert view.status == "error"
    assert view.error == "boom"


def test_flow_ext_step_error_fields() -> None:
    f = flow.step_finished(
        status="error", result=None, input_tokens=1, output_tokens=1, error="bad"
    )
    parts = [note(flow.run_started(None, None)), call("c0", started(0), f, status="completed")]
    step = flow.run_view({"turns": [turn(parts)]}).steps[0]
    assert (step.status, step.error, step.result) == ("error", "bad", None)


def test_flow_ext_two_runs() -> None:
    t1 = turn([note(flow.run_started("r1", None))], tid="t1")
    t2 = turn([note(flow.run_started("r2", None))], tid="t2")
    state = {"turns": [t1, t2]}
    assert [v.run_id for v in flow.run_views(state)] == ["r1", "r2"]
    assert flow.run_view(state).run_id == "r2"
    assert flow.run_view(state, turn_id="t1").run_id == "r1"
    assert flow.run_view(state, turn_id="nope") is EMPTY_RUN


def test_flow_ext_turn_without_run_started_is_not_a_run() -> None:
    state = {"turns": [turn([call("c0", started(0))])]}
    assert flow.run_views(state) == ()


def test_flow_ext_step_without_phase_started_gets_its_own_phase() -> None:
    parts = [
        note(flow.run_started(None, None)),
        call("c0", started(0, None), finished("x")),
        call("c1", started(1, "Late"), finished("y")),
    ]
    view = flow.run_view({"turns": [turn(parts)]})
    assert [p.title for p in view.phases] == [None, "Late"]
    assert [len(p.steps) for p in view.phases] == [1, 1]


def test_flow_ext_phase_without_steps_is_kept() -> None:
    parts = [note(flow.run_started(None, None)), note(flow.phase_started("Empty"))]
    view = flow.run_view({"activeTurn": turn(parts, state="active")})
    assert view.phases == (PhaseView("Empty", "running", ()),)


def test_flow_ext_malformed_parts_are_ignored() -> None:
    bad_parts: list[Any] = [
        "text",
        None,
        {"kind": "markdown", "content": "x"},
        {"kind": "systemNotification", "_meta": "nope"},
        {"kind": "systemNotification", "_meta": {"ahp-py/flow": {"mystery": {}}}},
        {"kind": "systemNotification", "_meta": {"ahp-py/flow": {"phaseStarted": "A"}}},
        {"kind": "systemNotification", "_meta": {"ahp-py/flow": {"phaseStarted": {"title": 1}}}},
        {"kind": "systemNotification", "_meta": flow.meta({"runFinished": {"status": "maybe"}})},
        {"kind": "toolCall", "toolCall": "x"},
        {"kind": "toolCall", "toolCall": {"toolCallId": "c", "_meta": "nope"}},
        {"kind": "toolCall", "toolCall": {"toolCallId": "c"}},
        {"kind": "toolCall", "toolCall": {"_meta": flow.meta({"stepStarted": "x"})}},
        call("cb", {**started(0), "ordinal": True}),
    ]
    parts = [note(flow.run_started("r", None)), *bad_parts]
    view = flow.run_view({"turns": [turn(parts), "junk", None], "activeTurn": "junk"})
    assert view.run_id == "r"
    assert view.status == "ok"
    assert view.phases == ()
    assert flow.run_view({"turns": "x"}) is EMPTY_RUN
    assert flow.run_view({"turns": [{"responseParts": "x"}]}) is EMPTY_RUN


def test_flow_ext_bad_finished_is_still_running() -> None:
    f = {"status": "maybe", "result": 1, "usage": {"inputTokens": 1, "outputTokens": 1}}
    parts = [note(flow.run_started(None, None)), call("c0", started(0), f)]
    view = flow.run_view({"activeTurn": turn(parts, state="active")})
    assert view.steps[0].status == "running"
    assert view.steps[0].usage is None


def test_flow_ext_non_int_usage_counts_as_zero() -> None:
    f = {"status": "ok", "result": 1, "usage": {"inputTokens": True, "outputTokens": "x"}}
    parts = [note(flow.run_started(None, None)), call("c0", started(0), f)]
    step = flow.run_view({"turns": [turn(parts)]}).steps[0]
    assert step.usage == {"inputTokens": 0, "outputTokens": 0}


def test_flow_ext_later_part_with_same_ordinal_wins() -> None:
    parts = [
        note(flow.run_started(None, None)),
        call("first", started(0), None),
        call("second", started(0), finished("done"), status="completed"),
    ]
    view = flow.run_view({"turns": [turn(parts)]})
    assert len(view.steps) == 1
    assert view.steps[0].tool_call_id == "second"
    assert view.steps[0].result == "done"


def offering(schedule: Any) -> dict[str, Any]:
    return {"name": "nightly", "_meta": {flow.FLOW_META_KEY: {"schedule": schedule}}}


def test_flowext_schedule_offered() -> None:
    good = {"expression": "0 6 * * 1-5", "timeZone": "UTC"}
    assert flow.offered_schedule(offering(good)) == good
    assert flow.offered_schedule({"name": "x"}) is None
    assert flow.offered_schedule({"_meta": {flow.FLOW_META_KEY: {}}}) is None
    assert flow.offered_schedule(offering("0 6 * * *")) is None
    assert flow.offered_schedule(offering({"expression": "0 6 * * *"})) is None
    assert flow.offered_schedule(offering({"expression": 5, "timeZone": "UTC"})) is None


def test_flowext_schedule_applied() -> None:
    offered = {"expression": "0 6 * * 1-5", "timeZone": "UTC"}
    child = offering(offered)
    trigger = {"id": "ahp-py/flow-schedule", "kind": "schedule", "schedule": offered}
    assert flow.SCHEDULE_TRIGGER_ID == "ahp-py/flow-schedule"

    definition: dict[str, Any] = {"title": "t", "triggers": []}
    before = copy.deepcopy(definition)
    assert flow.with_offered_schedule(definition, child)["triggers"] == [trigger]
    assert definition == before

    mine = {"id": "mine", "kind": "schedule", "schedule": {"expression": "1 1 * * *"}}
    held = {"title": "t", "triggers": [mine]}
    assert flow.with_offered_schedule(held, child)["triggers"] == [mine]

    assert flow.with_offered_schedule(definition, {"name": "x"})["triggers"] == []
    assert flow.with_offered_schedule({"title": "t"}, {"name": "x"})["triggers"] == []
    assert flow.with_offered_schedule({"title": "t"}, child)["triggers"] == [trigger]


def test_flowext_schedule_applied_to_any_sequence() -> None:
    offered = {"expression": "0 6 * * 1-5", "timeZone": "UTC"}
    child = offering(offered)
    trigger = {"id": "ahp-py/flow-schedule", "kind": "schedule", "schedule": offered}
    manual = {"id": "m", "kind": "manual"}
    # A tuple of triggers is extended, like a list; the result is always a list.
    assert flow.with_offered_schedule({"triggers": (manual,)}, child)["triggers"] == [
        manual,
        trigger,
    ]
    held = {"id": "mine", "kind": "schedule", "schedule": {"expression": "1 1 * * *"}}
    assert flow.with_offered_schedule({"triggers": (held,)}, child)["triggers"] == [held]
    # An empty tuple is a sequence of no triggers, not a missing field.
    assert flow.with_offered_schedule({"triggers": ()}, child)["triggers"] == [trigger]
    # Not a sequence of triggers: replaced, never iterated.
    for raw in ("schedule", b"xy", {"kind": "schedule"}, None, 5):
        assert flow.with_offered_schedule({"triggers": raw}, child)["triggers"] == [trigger]


def vcall(
    tcid: str,
    started_: dict[str, Any],
    finished_: dict[str, Any] | None = None,
    *,
    status: str = "running",
) -> dict[str, Any]:
    return {
        "kind": "toolCall",
        "toolCall": {
            "toolCallId": tcid,
            "status": status,
            "_meta": flow.meta(flow.verb(started_, finished_)),
        },
    }


def test_flow_ext_verb_builders_exact() -> None:
    assert flow.VERB_STATUSES == ("ok", "error")
    assert flow.STEP_KINDS == ("agent", "verb")
    s = flow.verb_started(name="tool", ordinal=2, phase="A")
    assert s == {"name": "tool", "ordinal": 2, "phase": "A"}
    assert flow.verb_started(name="n", ordinal=0, phase=None) == {
        "name": "n",
        "ordinal": 0,
        "phase": None,
    }
    assert flow.verb_finished(status="ok") == {"status": "ok"}
    f = flow.verb_finished(status="error", error="x")
    assert f == {"status": "error", "error": "x"}
    assert flow.verb(s) == {"verbStarted": s}
    assert flow.verb(s, f) == {"verbStarted": s, "verbFinished": f}
    with pytest.raises(ValueError, match="verb status must be"):
        flow.verb_finished(status="maybe")


def test_flow_ext_verb_schema() -> None:
    s = flow.verb_started(name="tool", ordinal=0, phase=None)
    for ok in (
        flow.verb(s),
        flow.verb(s, flow.verb_finished(status="ok")),
        flow.verb(s, flow.verb_finished(status="error", error="x")),
    ):
        VALIDATOR.validate(ok)
    bad: list[dict[str, Any]] = [
        {"verbStarted": s, "stepStarted": started(0)},
        {"verbStarted": s, "verbFinished": {"status": "maybe"}},
        {"verbStarted": {"ordinal": 0, "phase": None}},
        {"verbFinished": {"status": "ok"}},
    ]
    for doc in bad:
        assert not VALIDATOR.is_valid(doc), doc
    path = Path(flow.__file__).with_name(flow.SCHEMA_FILE)
    assert json.loads(path.read_text(encoding="utf-8")) == DESCRIPTOR_SCHEMA


def test_flow_ext_verb_fold() -> None:
    parts = [
        note(flow.run_started("r1", "demo")),
        note(flow.phase_started("A")),
        call("c0", started(0, "A"), finished("x", 1, 2), status="completed"),
        vcall(
            "v0",
            flow.verb_started(name="tool", ordinal=1, phase="A"),
            flow.verb_finished(status="ok"),
            status="completed",
        ),
        vcall(
            "v1",
            flow.verb_started(name="boom", ordinal=2, phase=None),
            flow.verb_finished(status="error", error="x"),
            status="completed",
        ),
        note(flow.phase_finished("A")),
        note(flow.run_finished("ok")),
    ]
    view = flow.run_view({"turns": [turn(parts)]})
    v0 = StepView(
        tool_call_id="v0",
        ordinal=1,
        label=None,
        phase="A",
        chat=None,
        model=None,
        provider=None,
        status="ok",
        kind="verb",
        name="tool",
    )
    v1 = StepView(
        tool_call_id="v1",
        ordinal=2,
        label=None,
        phase=None,
        chat=None,
        model=None,
        provider=None,
        status="error",
        error="x",
        kind="verb",
        name="boom",
    )
    assert [s.ordinal for s in view.steps] == [0]
    assert view.steps[0].kind == "agent"
    assert view.steps[0].name is None
    assert view.verbs == (v0, v1)
    assert view.phases[0].title == "A"
    assert view.phases[0].verbs == (v0,)
    assert view.phases[1].title is None
    assert view.phases[1].steps == ()
    assert view.phases[1].verbs == (v1,)
    assert view.usage == {"inputTokens": 1, "outputTokens": 2}


def test_flow_ext_verb_running_and_cancelled() -> None:
    parts = [
        note(flow.run_started("r1", None)),
        note(flow.phase_started("A")),
        vcall("v0", flow.verb_started(name="tool", ordinal=0, phase="A")),
    ]
    active = flow.run_view({"turns": [], "activeTurn": turn(parts, state="active")})
    assert [v.status for v in active.verbs] == ["running"]
    done = flow.run_view({"turns": [turn(parts)]})
    assert [v.status for v in done.verbs] == ["cancelled"]


def test_flow_ext_verb_malformed_ignored() -> None:
    bad_starts: list[Any] = [
        "x",
        None,
        {"name": "t", "ordinal": "x", "phase": None},
        {"name": "t", "ordinal": True, "phase": None},
    ]
    for bad in bad_starts:
        part = {
            "kind": "toolCall",
            "toolCall": {"toolCallId": "v", "_meta": flow.meta({"verbStarted": bad})},
        }
        view = flow.run_view({"turns": [turn([note(flow.run_started("r", None)), part])]})
        assert view.verbs == ()
        assert view.steps == ()


def test_flow_ext_workflow_constants() -> None:
    assert flow.RUN_ID_PATTERN == r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"
    assert flow.BAD_ARGS_ERROR_TYPE == "flow.badArgs"


def test_flow_ext_workflow_start_builders_exact() -> None:
    assert flow.workflow_start("host:x") == {"workflow": "host:x"}
    assert flow.workflow_start("host:x", args={"n": 1}, run_id="r1") == {
        "workflow": "host:x",
        "args": {"n": 1},
        "runId": "r1",
    }
    assert flow.workflow_start("host:x", args=None) == {"workflow": "host:x", "args": None}
    assert list(flow.workflow_start("w", args=1, run_id="r")) == ["workflow", "args", "runId"]
    names: list[Any] = ["", 5]
    for name in names:
        with pytest.raises(ValueError, match="workflow must be a non-empty string"):
            flow.workflow_start(name)
    for run_id in ("../x", "a" * 65):
        with pytest.raises(ValueError, match="runId must match"):
            flow.workflow_start("w", run_id=run_id)


def test_flow_ext_workflow_start_parse() -> None:
    assert flow.parse_workflow_start({}) is None
    assert flow.parse_workflow_start(flow.meta(flow.phase_started("A"))) is None
    marker = {"kind": "workflow", "phases": [], "source": "h"}
    assert flow.parse_workflow_start(flow.meta(marker)) is None
    got = flow.parse_workflow_start(
        flow.meta(flow.workflow_start("h:x", args={"n": 1}, run_id="r1"))
    )
    assert got == flow.WorkflowStart("h:x", {"n": 1}, True, "r1")
    null = flow.parse_workflow_start(flow.meta(flow.workflow_start("h:x", args=None)))
    assert null == flow.WorkflowStart("h:x", None, True, None)
    bare = flow.parse_workflow_start(flow.meta(flow.workflow_start("h:x")))
    assert bare == flow.WorkflowStart("h:x", None, False, None)
    extra = flow.parse_workflow_start(flow.meta({"workflow": "h:x", "other": 1}))
    assert extra == flow.WorkflowStart("h:x", None, False, None)
    with pytest.raises(ValueError, match="workflow must be a non-empty string"):
        flow.parse_workflow_start(flow.meta({"workflow": 5}))
    with pytest.raises(ValueError, match="runId must be a string"):
        flow.parse_workflow_start(flow.meta({"workflow": "x", "runId": 5}))


def test_flow_ext_workflow_schema() -> None:
    ok: list[dict[str, Any]] = [
        flow.workflow_start("host:x"),
        flow.workflow_start("host:x", args={"n": 1}, run_id="r1"),
        flow.workflow_start("host:x", args=None),
        {
            "kind": "workflow",
            "phases": [{"title": "A"}],
            "source": "host",
            "argsSchema": {"type": "object"},
        },
    ]
    for out in ok:
        VALIDATOR.validate(out)
    bad: list[dict[str, Any]] = [
        {"workflow": ""},
        {"workflow": "x", "kind": "workflow"},
        {"workflow": "x", "runId": "../x"},
        {"kind": "workflow", "phases": [], "source": "h", "language": "rust"},
    ]
    for doc in bad:
        assert not VALIDATOR.is_valid(doc), doc
    path = Path(flow.__file__).with_name(flow.SCHEMA_FILE)
    assert json.loads(path.read_text(encoding="utf-8")) == DESCRIPTOR_SCHEMA


def _prompt(name: str, source: str | None, **marker: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"kind": "workflow", "phases": []}
    if source is not None:
        body["source"] = source
    body.update(marker)
    return {"type": "prompt", "name": name, "_meta": flow.meta(body)}


def test_flow_ext_workflow_workflows_of() -> None:
    nested = _prompt(
        "a",
        "plug",
        phases=[{"title": "P1"}, {"x": 1}, "junk", {"title": 3}, {"title": "P2"}],
        argsSchema={"type": "object"},
        language="python",
    )
    nested["description"] = "does a"
    customizations = [
        {
            "type": "plugin",
            "children": [
                nested,
                "junk",
                {"type": "prompt", "name": "plain"},
                _prompt("nosrc", None),
            ],
        },
        {"type": "directory", "children": [_prompt("b", "dir")]},
        _prompt("c", "host"),
        "junk",
        None,
        {"type": "prompt", "name": "unmarked"},
    ]
    assert flow.workflows_of(customizations) == (
        flow.WorkflowInfo(
            "a", "plug:a", "does a", ("P1", "P2"), {"type": "object"}, "plug", "python"
        ),
        flow.WorkflowInfo("b", "dir:b", "", (), None, "dir", "js"),
        flow.WorkflowInfo("c", "host:c", "", (), None, "host", "js"),
    )
    assert flow.workflows_of(None) == ()
