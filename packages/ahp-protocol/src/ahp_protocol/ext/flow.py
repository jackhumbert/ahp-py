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

A host also marks each custom verb's tool call with a ``verbStarted`` /
``verbFinished`` descriptor; :func:`run_view` lists those as ``verbs`` (kind
``verb``) beside the agent steps, which stay in ``steps``.

A client starts a workflow with a *typed start*: :func:`workflow_start` builds the
descriptor a ``UserMessage`` carries under ``_meta[FLOW_META_KEY]`` (the workflow's
qualified name, optional ``args`` and an optional ``runId``), and
:func:`parse_workflow_start` reads it back on the host. :func:`workflows_of` lists the
workflows a host offers by walking its customizations for the ``workflow`` marker on
each prompt, flattening the plugin and directory containers that nest them.

Everything here is pure and stdlib-only. :func:`run_view` never raises: a part it
does not understand is ignored, never an error, because the chat state is data
written by a peer.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from ahp_protocol.ext.redaction import REDACTED_TEXT

__all__ = [
    "BAD_ARGS_ERROR_TYPE",
    "DESCRIPTOR_SCHEMA",
    "EMPTY_RUN",
    "FLOW_META_KEY",
    "HEARTBEAT_KIND",
    "NOTIFICATION_KINDS",
    "RUN_FINISHED_STATUSES",
    "RUN_ID_PATTERN",
    "SCHEDULE_TRIGGER_ID",
    "SCHEMA_FILE",
    "STEP_KINDS",
    "STEP_STATUSES",
    "VERB_STATUSES",
    "PhaseView",
    "RunView",
    "StepView",
    "WaitingView",
    "WorkflowInfo",
    "WorkflowStart",
    "descriptor_of",
    "heartbeat",
    "meta",
    "offered_schedule",
    "parse_workflow_start",
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
    "verb",
    "verb_finished",
    "verb_started",
    "with_offered_schedule",
    "workflow_start",
    "workflows_of",
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
#: ``verbFinished.status``.
VERB_STATUSES: Final = ("ok", "error")
#: :attr:`StepView.kind`: a model step or a custom verb.
STEP_KINDS: Final = ("agent", "verb")
#: The one key of a heartbeat descriptor (``chat/activityChanged`` ``_meta``).
HEARTBEAT_KIND: Final = "heartbeat"
#: The id of the schedule trigger :func:`with_offered_schedule` adds.
SCHEDULE_TRIGGER_ID: Final = "ahp-py/flow-schedule"

#: A run id: it names a directory, so it is the pattern ahp-host-flow accepts.
RUN_ID_PATTERN: Final = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"
#: The turn's ``errorType`` when a workflow start is malformed or its args fail the schema.
BAD_ARGS_ERROR_TYPE: Final = "flow.badArgs"

_RUN_ID: Final = re.compile(RUN_ID_PATTERN)
#: Tells "no args" from ``args=None``, which is a value.
_ABSENT: Final = object()

_NULLABLE_STRING: Final = {"type": ["string", "null"]}

#: Generated into ``flow.schema.json``; never hand-edit that file.
DESCRIPTOR_SCHEMA: Final[Mapping[str, Any]] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://github.com/jackhumbert/ahp-py/tree/main/packages/ahp-protocol/src/ahp_protocol/ext/flow.schema.json",
    "title": "ahp-py/flow progress descriptor",
    "oneOf": [
        {"$ref": "#/$defs/notification"},
        {"$ref": "#/$defs/step"},
        {"$ref": "#/$defs/verb"},
        {"$ref": "#/$defs/workflow"},
        {"$ref": "#/$defs/workflowStart"},
        {
            "type": "object",
            "required": ["heartbeat"],
            "properties": {"heartbeat": {"$ref": "#/$defs/heartbeat"}},
            "additionalProperties": False,
        },
    ],
    "$defs": {
        "heartbeat": {
            "type": "object",
            "required": ["running", "tick"],
            "properties": {
                "running": {"type": "integer", "minimum": 0},
                "tick": {"type": "integer", "minimum": 1},
            },
            "additionalProperties": False,
        },
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
        "waiting": {
            "type": "object",
            "required": ["kind"],
            "properties": {
                "kind": {"type": "string"},
                "chat": {"type": "string"},
            },
            "additionalProperties": False,
        },
        "step": {
            "type": "object",
            "required": ["stepStarted"],
            "properties": {
                "stepStarted": {"$ref": "#/$defs/stepStarted"},
                "stepFinished": {"$ref": "#/$defs/stepFinished"},
                "awaitingInput": {"const": True},
                "waiting": {"type": "array", "items": {"$ref": "#/$defs/waiting"}},
            },
            "additionalProperties": False,
        },
        "verbStarted": {
            "type": "object",
            "required": ["name", "ordinal", "phase"],
            "properties": {
                "name": {"type": "string"},
                "ordinal": {"type": "integer", "minimum": 0},
                "phase": _NULLABLE_STRING,
            },
        },
        "verbFinished": {
            "type": "object",
            "required": ["status"],
            "properties": {
                "status": {"enum": list(VERB_STATUSES)},
                "error": {"type": "string"},
            },
        },
        "verb": {
            "type": "object",
            "required": ["verbStarted"],
            "properties": {
                "verbStarted": {"$ref": "#/$defs/verbStarted"},
                "verbFinished": {"$ref": "#/$defs/verbFinished"},
            },
            "additionalProperties": False,
        },
        "workflowStart": {
            "type": "object",
            "required": ["workflow"],
            "properties": {
                "workflow": {"type": "string", "minLength": 1},
                "args": {},
                "runId": {"type": "string", "pattern": RUN_ID_PATTERN},
            },
            "additionalProperties": False,
        },
        # The customization marker on a workflow's prompt; it may grow, so it is open.
        "workflow": {
            "type": "object",
            "required": ["kind", "phases", "source"],
            "properties": {
                "kind": {"const": "workflow"},
                "phases": {"type": "array"},
                "source": {"type": "string"},
                "shadowedBy": {"type": "string"},
                "language": {"enum": ["js", "python"]},
                "schedule": {
                    "type": "object",
                    "required": ["expression", "timeZone"],
                    "properties": {
                        "expression": {"type": "string"},
                        "timeZone": {"type": "string"},
                    },
                },
                "warnings": {"type": "array", "items": {"type": "string"}},
                "argsSchema": {"type": "object"},
            },
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
    waiting: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """A step descriptor; ``finished`` is added on the result, ``awaiting`` while blocked.

    ``waiting`` lists what the step is blocked on, one ``{kind, chat?}`` per distinct
    request; a non-empty list implies ``awaitingInput``.
    """
    entries: list[dict[str, Any]] = []
    for w in waiting:
        kind = w.get("kind")
        if not isinstance(kind, str):
            raise ValueError("waiting entries need a string kind")
        entry: dict[str, Any] = {"kind": kind}
        chat = w.get("chat")
        if isinstance(chat, str):
            entry["chat"] = chat
        entries.append(entry)
    body: dict[str, Any] = {"stepStarted": dict(started)}
    if finished is not None:
        body["stepFinished"] = dict(finished)
    if awaiting or entries:
        body["awaitingInput"] = True
    if entries:
        body["waiting"] = entries
    return body


def heartbeat(*, running: int, tick: int) -> dict[str, Any]:
    """A liveness tick: ``running`` steps in flight, the ``tick``-th (from 1) of the run."""
    if running < 0:
        raise ValueError(f"running must be >= 0, got {running!r}")
    if tick < 1:
        raise ValueError(f"tick must be >= 1, got {tick!r}")
    return {HEARTBEAT_KIND: {"running": running, "tick": tick}}


def verb_started(*, name: str, ordinal: int, phase: str | None) -> dict[str, Any]:
    return {"name": name, "ordinal": ordinal, "phase": phase}


def verb_finished(*, status: str, error: str | None = None) -> dict[str, Any]:
    if status not in VERB_STATUSES:
        raise ValueError(f"verb status must be one of {VERB_STATUSES}, got {status!r}")
    body: dict[str, Any] = {"status": status}
    if error is not None:
        body["error"] = error
    return body


def verb(started: Mapping[str, Any], finished: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """A custom verb's descriptor; ``finished`` is added on the result."""
    body: dict[str, Any] = {"verbStarted": dict(started)}
    if finished is not None:
        body["verbFinished"] = dict(finished)
    return body


def workflow_start(
    workflow: str, *, args: Any = _ABSENT, run_id: str | None = None
) -> dict[str, Any]:
    """The descriptor that starts ``workflow`` (a qualified name); ``args`` only if passed."""
    if not isinstance(workflow, str) or not workflow:
        raise ValueError("workflow must be a non-empty string")
    if run_id is not None and not (isinstance(run_id, str) and _RUN_ID.fullmatch(run_id)):
        raise ValueError(f"runId must match {RUN_ID_PATTERN}, got {run_id!r}")
    body: dict[str, Any] = {"workflow": workflow}
    if args is not _ABSENT:
        body["args"] = args
    if run_id is not None:
        body["runId"] = run_id
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


def offered_schedule(customization: Mapping[str, Any]) -> dict[str, str] | None:
    """The default schedule a workflow's customization offers, or ``None``.

    A host lists a workflow's ``meta.x.schedule`` as ``schedule`` in the descriptor
    under :data:`FLOW_META_KEY`; anything that is not ``{expression, timeZone}``
    strings is not an offer.
    """
    meta = customization.get("_meta")
    flow_meta = meta.get(FLOW_META_KEY) if isinstance(meta, Mapping) else None
    s = flow_meta.get("schedule") if isinstance(flow_meta, Mapping) else None
    if (
        isinstance(s, Mapping)
        and isinstance(s.get("expression"), str)
        and isinstance(s.get("timeZone"), str)
    ):
        return {"expression": s["expression"], "timeZone": s["timeZone"]}
    return None


def with_offered_schedule(
    definition: Mapping[str, Any], customization: Mapping[str, Any]
) -> dict[str, Any]:
    """A copy of an automation ``definition`` with the offered schedule added as a trigger.

    Calling it is how a client accepts the default; the host never applies it itself.
    A definition that already has a schedule trigger, or a customization that offers
    none, gets its triggers back unchanged. ``definition`` is not mutated.
    """
    out = dict(definition)
    raw = definition.get("triggers")
    # Not `or []`: invariant 5. Any sequence of triggers (a list, a tuple) is extended;
    # anything else (a string, bytes, a mapping, None) is replaced.
    triggers: list[Any] = (
        list(raw)
        if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes, bytearray))
        else []
    )
    offered = offered_schedule(customization)
    if offered is not None and not any(
        isinstance(t, Mapping) and t.get("kind") == "schedule" for t in triggers
    ):
        triggers.append(
            {
                "id": SCHEDULE_TRIGGER_ID,
                "kind": "schedule",
                "schedule": {"expression": offered["expression"], "timeZone": offered["timeZone"]},
            }
        )
    out["triggers"] = triggers
    return out


@dataclass(frozen=True)
class WorkflowStart:
    """A parsed typed start; ``has_args`` tells absent ``args`` from ``None``."""

    workflow: str
    args: Any
    has_args: bool
    run_id: str | None


def parse_workflow_start(meta: Any) -> WorkflowStart | None:
    """The typed start under :data:`FLOW_META_KEY`, or ``None`` if ``meta`` holds none.

    A customization marker or a progress descriptor has no ``workflow`` key and is not
    a start. ``runId`` is only checked to be a string: the pattern is the host's call,
    which mints a fresh id for one it cannot use.
    """
    d = descriptor_of(meta)
    if d is None or "workflow" not in d:
        return None
    workflow = d["workflow"]
    if not isinstance(workflow, str) or not workflow:
        raise ValueError("workflow must be a non-empty string")
    run_id = d.get("runId")
    if "runId" in d and not isinstance(run_id, str):
        raise ValueError("runId must be a string")
    return WorkflowStart(workflow, d.get("args"), "args" in d, run_id)


@dataclass(frozen=True)
class WorkflowInfo:
    name: str
    qualified_name: str
    description: str
    phases: tuple[str, ...]
    args_schema: Mapping[str, Any] | None
    source: str
    language: str


def workflows_of(customizations: Any) -> tuple[WorkflowInfo, ...]:
    """The workflows a host lists among its customizations, in the order met.

    Each item and each item of its ``children`` (one level: how ahp-host-flow nests
    prompts in plugin and directory containers) counts when it is a ``prompt`` whose
    ``_meta`` holds a ``workflow`` marker with a ``source``. Malformed input is skipped.
    """
    if not isinstance(customizations, list):
        return ()
    found: list[WorkflowInfo] = []
    for item in customizations:
        if not isinstance(item, Mapping):
            continue
        children = item.get("children")
        candidates = [item, *(children if isinstance(children, list) else [])]
        for c in candidates:
            if not isinstance(c, Mapping) or c.get("type") != "prompt":
                continue
            name = c.get("name")
            marker = descriptor_of(c.get("_meta"))
            if (
                not isinstance(name, str)
                or marker is None
                or marker.get("kind") != "workflow"
                or not isinstance(marker.get("source"), str)
            ):
                continue
            source = marker["source"]
            description = c.get("description")
            raw_phases = marker.get("phases")
            raw_schema = marker.get("argsSchema")
            language = marker.get("language")
            found.append(
                WorkflowInfo(
                    name=name,
                    qualified_name=f"{source}:{name}",
                    description=description if isinstance(description, str) else "",
                    phases=tuple(
                        p["title"]
                        for p in (raw_phases if isinstance(raw_phases, list) else [])
                        if isinstance(p, Mapping) and isinstance(p.get("title"), str)
                    ),
                    args_schema=dict(raw_schema) if isinstance(raw_schema, Mapping) else None,
                    source=source,
                    language=language if isinstance(language, str) else "js",
                )
            )
    return tuple(found)


@dataclass(frozen=True)
class WaitingView:
    """One request a running step is blocked on; ``kind`` is ``None`` if the host did not say."""

    ordinal: int
    tool_call_id: str
    kind: str | None
    chat: str | None


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
    #: One of :data:`STEP_KINDS`.
    kind: str = "agent"
    #: A verb's name; ``None`` for an agent step.
    name: str | None = None
    #: What the step is blocked on; empty unless ``awaiting``.
    waiting: tuple[WaitingView, ...] = ()


@dataclass(frozen=True)
class PhaseView:
    title: str | None
    #: ``running`` or ``finished``.
    status: str
    steps: tuple[StepView, ...]
    verbs: tuple[StepView, ...] = ()


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

    @property
    def verbs(self) -> tuple[StepView, ...]:
        """Every custom verb of every phase, by ordinal."""
        return tuple(sorted((s for p in self.phases for s in p.verbs), key=lambda s: s.ordinal))

    @property
    def waiting(self) -> tuple[WaitingView, ...]:
        """Everything the awaiting steps are blocked on, by ordinal."""
        found: list[WaitingView] = []
        for s in self.steps:
            if s.awaiting:
                found.extend(s.waiting or (WaitingView(s.ordinal, s.tool_call_id, None, None),))
        return tuple(found)


EMPTY_RUN: Final = RunView(
    None, None, None, "none", None, (), {"inputTokens": 0, "outputTokens": 0}
)


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _waiting_entries(value: Any) -> tuple[tuple[str, str | None], ...]:
    if not isinstance(value, list):
        return ()
    return tuple(
        (w["kind"], _str(w.get("chat")))
        for w in value
        if isinstance(w, Mapping) and isinstance(w.get("kind"), str)
    )


class _Raw:
    """What one step's latest part said, before the run's status is known."""

    __slots__ = ("awaiting", "finished", "started", "status", "tool_call_id", "waiting")

    def __init__(
        self,
        started: Mapping[str, Any],
        finished: Any,
        awaiting: bool,
        tool_call_id: str,
        status: Any,
        waiting: tuple[tuple[str, str | None], ...] = (),
    ) -> None:
        self.started = started
        self.finished = finished
        self.awaiting = awaiting
        self.tool_call_id = tool_call_id
        self.status = status
        self.waiting = waiting


def _fold_turn(turn: Mapping[str, Any], active: bool) -> RunView | None:
    is_run = False
    run_id: str | None = None
    workflow: str | None = None
    finished_status: str | None = None
    finished_error: str | None = None
    phase_order: list[str | None] = []
    phase_state: dict[str, str] = {}
    steps: dict[int, _Raw] = {}
    verbs: dict[int, _Raw] = {}

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
            if d is None:
                continue
            started = d.get("stepStarted")
            target = steps
            if not isinstance(started, Mapping):
                started = d.get("verbStarted")
                target = verbs
                if not isinstance(started, Mapping):
                    continue
            ordinal = started.get("ordinal")
            if not isinstance(ordinal, int) or isinstance(ordinal, bool):
                continue
            target[ordinal] = _Raw(
                started,
                d.get("stepFinished" if target is steps else "verbFinished"),
                d.get("awaitingInput") is True,
                _str(tc.get("toolCallId")) or "",
                tc.get("status"),
                _waiting_entries(d.get("waiting")),
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
        awaiting = raw.awaiting and step_status == "running"
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
                awaiting=awaiting,
                waiting=(
                    tuple(WaitingView(ordinal, raw.tool_call_id, k, c) for k, c in raw.waiting)
                    if awaiting
                    else ()
                ),
            )
        )
    verbs_by_phase: dict[str | None, list[StepView]] = {title: [] for title in phase_order}
    for ordinal in sorted(verbs):
        raw = verbs[ordinal]
        fin = raw.finished
        error = None
        if isinstance(fin, Mapping) and fin.get("status") in VERB_STATUSES:
            verb_status = fin["status"]
            error = _str(fin.get("error"))
        elif raw.status == "cancelled" or status != "running":
            verb_status = "cancelled"
        else:
            verb_status = "running"
        s = raw.started
        verbs_by_phase[_str(s.get("phase"))].append(
            StepView(
                tool_call_id=raw.tool_call_id,
                ordinal=ordinal,
                label=None,
                phase=_str(s.get("phase")),
                chat=None,
                model=None,
                provider=None,
                status=verb_status,
                error=error,
                kind="verb",
                name=_str(s.get("name")),
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
            verbs=tuple(verbs_by_phase[title]),
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
