"""Typed progress for workflow runs: descriptors, their schema and the fold.

This is an ahp-py extension ahead of the spec. A workflow host (ahp-host-flow)
narrates a run as ordinary AHP content: ``systemNotification`` response parts
for the run and its phases, and one ``toolCall`` per step on the main chat. Each
carries a small machine-readable *descriptor* under ``_meta[FLOW_META_KEY]``. A
stock client ignores ``_meta`` and renders the text it always did; a client that
knows this module folds the same chat state into a :class:`RunView`.

It lives in the protocol package because host and client must agree on the
descriptor shapes and on the fold byte for byte: the host builds descriptors
with the builders below, the client reads them back with :func:`run_view`, and
:data:`DESCRIPTOR_SCHEMA` (shipped as ``flow.schema.json``) is the contract for
any third party.

Everything here is pure and stdlib-only. :func:`run_view` never raises: a part it
does not understand is ignored, never an error, because the chat state is data
written by a peer.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from ahp_protocol.ext.redaction import REDACTED_TEXT

__all__ = [
    "DESCRIPTOR_SCHEMA",
    "EMPTY_RUN",
    "FLOW_META_KEY",
    "NOTIFICATION_KINDS",
    "RUN_FINISHED_STATUSES",
    "SCHEMA_FILE",
    "STEP_STATUSES",
    "PhaseView",
    "RunView",
    "StepView",
    "descriptor_of",
    "meta",
    "phase_finished",
    "phase_started",
    "redacted_step",
    "run_finished",
    "run_started",
    "run_view",
    "run_views",
    "step",
    "step_finished",
    "step_started",
]

#: Key in ``_meta``; the value is one descriptor.
FLOW_META_KEY: Final = "ahp-py/flow"
#: The JSON Schema of the descriptors, next to this module.
SCHEMA_FILE: Final = "flow.schema.json"
#: The one key of a system-notification descriptor.
NOTIFICATION_KINDS: Final = ("runStarted", "phaseStarted", "phaseFinished", "runFinished")
#: ``stepFinished.status``.
STEP_STATUSES: Final = ("ok", "null", "error", "skipped")
#: ``runFinished.status``.
RUN_FINISHED_STATUSES: Final = ("ok", "error")

_NULLABLE_STRING: Final = {"type": ["string", "null"]}

#: Generated into ``flow.schema.json``; never hand-edit that file.
DESCRIPTOR_SCHEMA: Final[Mapping[str, Any]] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://github.com/jackhumbert/ahp-py/tree/main/packages/ahp-protocol/src/ahp_protocol/ext/flow.schema.json",
    "title": "ahp-py/flow progress descriptor",
    "oneOf": [{"$ref": "#/$defs/notification"}, {"$ref": "#/$defs/step"}],
    "$defs": {
        "usage": {
            "type": "object",
            "required": ["inputTokens", "outputTokens"],
            "properties": {
                "inputTokens": {"type": "integer", "minimum": 0},
                "outputTokens": {"type": "integer", "minimum": 0},
            },
        },
        "runStarted": {
            "type": "object",
            "required": ["runId"],
            "properties": {"runId": _NULLABLE_STRING, "workflow": _NULLABLE_STRING},
        },
        "phaseStarted": {
            "type": "object",
            "required": ["title"],
            "properties": {"title": {"type": "string"}},
        },
        "phaseFinished": {
            "type": "object",
            "required": ["title"],
            "properties": {"title": {"type": "string"}},
        },
        "runFinished": {
            "type": "object",
            "required": ["status"],
            "properties": {
                "status": {"enum": list(RUN_FINISHED_STATUSES)},
                "error": {"type": "string"},
            },
        },
        "notification": {
            "type": "object",
            "minProperties": 1,
            "maxProperties": 1,
            "properties": {
                "runStarted": {"$ref": "#/$defs/runStarted"},
                "phaseStarted": {"$ref": "#/$defs/phaseStarted"},
                "phaseFinished": {"$ref": "#/$defs/phaseFinished"},
                "runFinished": {"$ref": "#/$defs/runFinished"},
            },
            "additionalProperties": False,
        },
        "stepStarted": {
            "type": "object",
            "required": ["ordinal", "label", "phase", "chat", "model", "provider"],
            "properties": {
                "ordinal": {"type": "integer", "minimum": 0},
                "label": _NULLABLE_STRING,
                "phase": _NULLABLE_STRING,
                "chat": _NULLABLE_STRING,
                "model": _NULLABLE_STRING,
                "provider": _NULLABLE_STRING,
            },
        },
        "stepFinished": {
            "type": "object",
            "required": ["status", "result", "usage"],
            "properties": {
                "status": {"enum": list(STEP_STATUSES)},
                "result": {},
                "usage": {"$ref": "#/$defs/usage"},
                "error": {"type": "string"},
            },
        },
        "step": {
            "type": "object",
            "required": ["stepStarted"],
            "properties": {
                "stepStarted": {"$ref": "#/$defs/stepStarted"},
                "stepFinished": {"$ref": "#/$defs/stepFinished"},
                "awaitingInput": {"const": True},
            },
            "additionalProperties": False,
        },
    },
}


def run_started(run_id: str | None, workflow: str | None) -> dict[str, Any]:
    return {"runStarted": {"runId": run_id, "workflow": workflow}}


def phase_started(title: str) -> dict[str, Any]:
    return {"phaseStarted": {"title": title}}


def phase_finished(title: str) -> dict[str, Any]:
    return {"phaseFinished": {"title": title}}


def run_finished(status: str, error: str | None = None) -> dict[str, Any]:
    if status not in RUN_FINISHED_STATUSES:
        raise ValueError(f"run status must be one of {RUN_FINISHED_STATUSES}, got {status!r}")
    body: dict[str, Any] = {"status": status}
    if error is not None:
        body["error"] = error
    return {"runFinished": body}


def step_started(
    *,
    ordinal: int,
    label: str | None,
    phase: str | None,
    chat: str | None,
    model: str | None,
    provider: str | None,
) -> dict[str, Any]:
    return {
        "ordinal": ordinal,
        "label": label,
        "phase": phase,
        "chat": chat,
        "model": model,
        "provider": provider,
    }


def step_finished(
    *,
    status: str,
    result: Any,
    input_tokens: int,
    output_tokens: int,
    error: str | None = None,
) -> dict[str, Any]:
    if status not in STEP_STATUSES:
        raise ValueError(f"step status must be one of {STEP_STATUSES}, got {status!r}")
    body: dict[str, Any] = {
        "status": status,
        "result": result,
        "usage": {"inputTokens": input_tokens, "outputTokens": output_tokens},
    }
    if error is not None:
        body["error"] = error
    return body


def step(
    started: Mapping[str, Any],
    finished: Mapping[str, Any] | None = None,
    *,
    awaiting: bool = False,
) -> dict[str, Any]:
    """A step descriptor; ``finished`` is added on the result, ``awaiting`` while blocked."""
    body: dict[str, Any] = {"stepStarted": dict(started)}
    if finished is not None:
        body["stepFinished"] = dict(finished)
    if awaiting:
        body["awaitingInput"] = True
    return body


def redacted_step(descriptor: Mapping[str, Any]) -> dict[str, Any]:
    """A copy of a step descriptor with its result (and error) replaced by ``[redacted]``.

    ``stepStarted`` and ``awaitingInput`` are kept; a descriptor without
    ``stepFinished`` is returned as a shallow copy.
    """
    body = dict(descriptor)
    finished = body.get("stepFinished")
    if isinstance(finished, Mapping):
        redacted = dict(finished)
        redacted["result"] = REDACTED_TEXT
        if "error" in redacted:
            redacted["error"] = REDACTED_TEXT
        body["stepFinished"] = redacted
    return body


def meta(descriptor: Mapping[str, Any]) -> dict[str, Any]:
    """The ``_meta`` object carrying ``descriptor``."""
    return {FLOW_META_KEY: dict(descriptor)}


def descriptor_of(meta: Any) -> Mapping[str, Any] | None:
    """The descriptor under :data:`FLOW_META_KEY`, or ``None``."""
    if not isinstance(meta, Mapping):
        return None
    found = meta.get(FLOW_META_KEY)
    return found if isinstance(found, Mapping) else None


@dataclass(frozen=True)
class StepView:
    tool_call_id: str
    ordinal: int
    label: str | None
    phase: str | None
    chat: str | None
    model: str | None
    provider: str | None
    #: ``running``, ``cancelled``, or a :data:`STEP_STATUSES` value.
    status: str
    result: Any = None
    usage: Mapping[str, int] | None = None
    error: str | None = None
    awaiting: bool = False


@dataclass(frozen=True)
class PhaseView:
    title: str | None
    #: ``running`` or ``finished``.
    status: str
    steps: tuple[StepView, ...]


@dataclass(frozen=True)
class RunView:
    turn_id: str | None
    run_id: str | None
    workflow: str | None
    #: ``none``, ``running``, ``ok``, ``error`` or ``cancelled``.
    status: str
    error: str | None
    phases: tuple[PhaseView, ...]
    usage: Mapping[str, int]

    @property
    def steps(self) -> tuple[StepView, ...]:
        """Every step of every phase, by ordinal."""
        return tuple(sorted((s for p in self.phases for s in p.steps), key=lambda s: s.ordinal))


EMPTY_RUN: Final = RunView(
    None, None, None, "none", None, (), {"inputTokens": 0, "outputTokens": 0}
)


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


class _Raw:
    """What one step's latest part said, before the run's status is known."""

    __slots__ = ("awaiting", "finished", "started", "status", "tool_call_id")

    def __init__(
        self,
        started: Mapping[str, Any],
        finished: Any,
        awaiting: bool,
        tool_call_id: str,
        status: Any,
    ) -> None:
        self.started = started
        self.finished = finished
        self.awaiting = awaiting
        self.tool_call_id = tool_call_id
        self.status = status


def _fold_turn(turn: Mapping[str, Any], active: bool) -> RunView | None:
    is_run = False
    run_id: str | None = None
    workflow: str | None = None
    finished_status: str | None = None
    finished_error: str | None = None
    phase_order: list[str | None] = []
    phase_state: dict[str, str] = {}
    steps: dict[int, _Raw] = {}

    def note_phase(title: str | None) -> None:
        if title not in phase_order:
            phase_order.append(title)

    parts = turn.get("responseParts")
    for part in parts if isinstance(parts, list) else []:
        if not isinstance(part, Mapping):
            continue
        kind = part.get("kind")
        if kind == "systemNotification":
            d = descriptor_of(part.get("_meta"))
            if d is None or len(d) != 1:
                continue
            (name, body), *_ = d.items()
            if name not in NOTIFICATION_KINDS or not isinstance(body, Mapping):
                continue
            title = body.get("title")
            if name == "runStarted":
                is_run = True
                run_id = _str(body.get("runId"))
                workflow = _str(body.get("workflow"))
            elif name == "phaseStarted" and isinstance(title, str):
                note_phase(title)
                phase_state[title] = "running"
            elif name == "phaseFinished" and isinstance(title, str):
                note_phase(title)
                phase_state[title] = "finished"
            elif name == "runFinished" and body.get("status") in RUN_FINISHED_STATUSES:
                finished_status = body["status"]
                finished_error = _str(body.get("error"))
        elif kind == "toolCall":
            tc = part.get("toolCall")
            if not isinstance(tc, Mapping):
                continue
            d = descriptor_of(tc.get("_meta"))
            started = d.get("stepStarted") if d is not None else None
            if d is None or not isinstance(started, Mapping):
                continue
            ordinal = started.get("ordinal")
            if not isinstance(ordinal, int) or isinstance(ordinal, bool):
                continue
            steps[ordinal] = _Raw(
                started,
                d.get("stepFinished"),
                d.get("awaitingInput") is True,
                _str(tc.get("toolCallId")) or "",
                tc.get("status"),
            )
            note_phase(_str(started.get("phase")))

    if not is_run:
        return None
    if finished_status is not None:
        status = finished_status
    elif active:
        status = "running"
    elif turn.get("state") == "cancelled":
        status = "cancelled"
    elif turn.get("state") == "error":
        status = "error"
    else:
        status = "ok"

    by_phase: dict[str | None, list[StepView]] = {title: [] for title in phase_order}
    total_in = 0
    total_out = 0
    for ordinal in sorted(steps):
        raw = steps[ordinal]
        fin = raw.finished
        usage: Mapping[str, int] | None = None
        result: Any = None
        error: str | None = None
        if isinstance(fin, Mapping) and fin.get("status") in STEP_STATUSES:
            step_status = fin["status"]
            result = fin.get("result")
            error = _str(fin.get("error"))
            raw_usage = fin.get("usage")
            if isinstance(raw_usage, Mapping):
                usage = {
                    "inputTokens": _count(raw_usage.get("inputTokens")),
                    "outputTokens": _count(raw_usage.get("outputTokens")),
                }
                total_in += usage["inputTokens"]
                total_out += usage["outputTokens"]
        elif raw.status == "cancelled" or status != "running":
            step_status = "cancelled"
        else:
            step_status = "running"
        s = raw.started
        by_phase[_str(s.get("phase"))].append(
            StepView(
                tool_call_id=raw.tool_call_id,
                ordinal=ordinal,
                label=_str(s.get("label")),
                phase=_str(s.get("phase")),
                chat=_str(s.get("chat")),
                model=_str(s.get("model")),
                provider=_str(s.get("provider")),
                status=step_status,
                result=result,
                usage=usage,
                error=error,
                awaiting=raw.awaiting and step_status == "running",
            )
        )
    phases = tuple(
        PhaseView(
            title=title,
            status=(
                "finished"
                if status != "running"
                or (title is not None and phase_state.get(title) == "finished")
                else "running"
            ),
            steps=tuple(by_phase[title]),
        )
        for title in phase_order
    )
    turn_id = turn.get("id")
    return RunView(
        turn_id=_str(turn_id),
        run_id=run_id,
        workflow=workflow,
        status=status,
        error=finished_error,
        phases=phases,
        usage={"inputTokens": total_in, "outputTokens": total_out},
    )


def run_views(chat_state: Mapping[str, Any]) -> tuple[RunView, ...]:
    """One view per turn that holds a run, oldest first."""
    if not isinstance(chat_state, Mapping):
        return ()
    views: list[RunView] = []
    turns = chat_state.get("turns")
    for turn in turns if isinstance(turns, list) else []:
        if isinstance(turn, Mapping) and (view := _fold_turn(turn, False)) is not None:
            views.append(view)
    active = chat_state.get("activeTurn")
    if isinstance(active, Mapping) and (view := _fold_turn(active, True)) is not None:
        views.append(view)
    return tuple(views)


def run_view(chat_state: Mapping[str, Any], *, turn_id: str | None = None) -> RunView:
    """The run of turn ``turn_id``, else the last run; :data:`EMPTY_RUN` if none."""
    views = run_views(chat_state)
    if turn_id is not None:
        return next((v for v in views if v.turn_id == turn_id), EMPTY_RUN)
    return views[-1] if views else EMPTY_RUN
