"""`ask()`, `approve()`, `canvas()` and custom verbs on the run's main chat (plan §6).

The engine's `GateRunner` numbers and journals the gates; this is the `Gates` it
asks: an `ask()` is an input request on the main chat (a select, text, number or
boolean form built from `choices` or a flat `schema`), an `approve()` a tool
confirmation with an edits preview, and `canvas()` goes out through `canvas_set`
(experimental in AHP 1.0.0). Everything published is masked first; the client's
answer reaches the script as it was typed (the engine masks it once, for the
journal and the script).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import itertools
import json
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Final

from ahp_host import TurnSink
from ahp_host.provider.base import (
    Canvas,
    InputQuestion,
    InputRequest,
    ResolvesInput,
    SessionPublisher,
    ToolConfirmation,
)
from ahp_host.provider.changes import FileChange
from ahp_protocol.ext import flow

from ahp_host_flow import catalog
from ahp_host_flow.engine import (
    AskOutcome,
    ConfirmOutcome,
    GateCall,
    Redactor,
    VerbCall,
    VerbContext,
    schema_error,
)
from ahp_host_flow.engine.gates import options_error

#: The same bound as `ahp_host.core.structured.MAX_ATTEMPTS`: invalid answers re-asked.
ASK_ATTEMPTS: Final = 3
CANVAS_EXTENSION_ID: Final = "ahp-py/flow"
CANVAS_ID: Final = "workflow"

_QUESTION_ID: Final = "answer"
_SCALAR_KINDS: Final = {
    "string": "text",
    "number": "number",
    "integer": "integer",
    "boolean": "boolean",
}


def _enum_of(prop: Mapping[str, Any]) -> list[str] | None:
    values = prop.get("enum")
    if isinstance(values, list) and values and all(isinstance(v, str) for v in values):
        return list(values)
    return None


def _flat_questions(
    schema: Mapping[str, Any],
) -> list[InputQuestion] | None:
    """One question per property, or None when the schema is not a flat form."""
    props = schema.get("properties")
    if schema.get("type") != "object" or not isinstance(props, Mapping) or not props:
        return None
    required = schema.get("required")
    required = required if isinstance(required, list) else []
    questions: list[InputQuestion] = []
    for name, prop in props.items():
        if not isinstance(name, str) or not isinstance(prop, Mapping):
            return None
        kind: str
        options: list[dict[str, Any]] = []
        type_ = prop.get("type")
        enum = _enum_of(prop)
        items = prop.get("items")
        if type_ == "string" and enum is not None:
            kind, options = "single-select", [{"id": v, "label": v} for v in enum]
        elif isinstance(type_, str) and type_ in _SCALAR_KINDS and "enum" not in prop:
            kind = _SCALAR_KINDS[type_]
        elif (
            type_ == "array"
            and isinstance(items, Mapping)
            and items.get("type") == "string"
            and (values := _enum_of(items)) is not None
        ):
            kind, options = "multi-select", [{"id": v, "label": v} for v in values]
        else:
            return None
        title = prop.get("title")
        extra: dict[str, Any] = {}
        if isinstance(title, str) and title:
            extra["title"] = title
        if name in required:
            extra["required"] = True
        description = prop.get("description")
        message = next((t for t in (description, title) if isinstance(t, str) and t), name)
        questions.append(InputQuestion(name, kind, message, options, extra))
    return questions


def gate_form(question: str, options: Mapping[str, Any]) -> tuple[str | None, list[InputQuestion]]:
    """The request's message and questions for an `ask()` call."""
    choices = options.get("choices")
    schema = options.get("schema")
    if choices is not None:
        return None, [
            InputQuestion(
                _QUESTION_ID,
                "single-select",
                question,
                [{"id": c, "label": c} for c in choices],
                {"required": True},
            )
        ]
    if isinstance(schema, Mapping):
        flat = _flat_questions(schema)
        if flat is not None:
            return question, flat
        text = (
            f"{question}\n\nAnswer with JSON matching this schema:\n{json.dumps(schema, indent=2)}"
        )
        return None, [InputQuestion(_QUESTION_ID, "text", text, extra={"required": True})]
    return None, [InputQuestion(_QUESTION_ID, "text", question, extra={"required": True})]


def _submitted(answers: Mapping[str, Any], qid: str) -> tuple[bool, Any]:
    a = answers.get(qid)
    if not isinstance(a, Mapping) or a.get("state") != "submitted":
        return False, None
    v = a.get("value")
    return True, (v.get("value") if isinstance(v, Mapping) else v)


def gate_answer(options: Mapping[str, Any], answers: Mapping[str, Any]) -> tuple[Any, str | None]:
    """The script's value from the submitted *answers*, and why it is unusable (or None)."""
    choices = options.get("choices")
    schema = options.get("schema")
    value: Any
    if choices is not None:
        _, value = _submitted(answers, _QUESTION_ID)
        if value not in choices:
            return value, f"{value!r} is not one of the choices"
        return value, None
    if isinstance(schema, Mapping):
        flat = _flat_questions(schema)
        if flat is not None:
            value = {}
            for q in flat:
                found, v = _submitted(answers, q.id)
                if found and v is not None:
                    value[q.id] = v
        else:
            _, text = _submitted(answers, _QUESTION_ID)
            try:
                value = json.loads(text if isinstance(text, str) else "")
            except ValueError as exc:
                return text, f"not JSON: {exc}"
        return value, schema_error(schema, value)
    _, value = _submitted(answers, _QUESTION_ID)
    return value, None


class MainChatGates:
    """The engine's `Gates` on the run's main chat."""

    def __init__(
        self,
        *,
        sink: TurnSink,
        publisher: SessionPublisher,
        chat: str,
        redactor: Redactor,
        on_log: Callable[[str], None] | None = None,
        call_id_prefix: str = "gate",
        working_directories: Sequence[str] = (),
    ) -> None:
        self._sink = sink
        self._publisher = publisher
        self.chat = chat
        self._redactor = redactor
        self._on_log = on_log
        self._prefix = call_id_prefix
        self._workspace = next(
            (p for d in working_directories if (p := catalog.directory_of(d))), None
        )

    def _log(self, message: str) -> None:
        if self._on_log is not None:
            self._on_log(message)

    def _masked_form(
        self, question: str, options: Mapping[str, Any]
    ) -> tuple[str | None, list[InputQuestion]]:
        mask = self._redactor.mask
        message, questions = gate_form(question, options)
        masked: list[InputQuestion] = []
        for q in questions:
            extra = (
                {**q.extra, "title": mask(str(q.extra["title"]))} if "title" in q.extra else q.extra
            )
            # Option ids stay as typed: an answer must match them.
            labelled = [{**o, "label": mask(str(o["label"]))} for o in q.options]
            masked.append(InputQuestion(q.id, q.kind, mask(q.message), labelled, extra))
        return mask(message) if message is not None else None, masked

    async def ask(self, call: GateCall) -> Any:
        return (await self.ask_outcome(call)).value

    async def ask_outcome(self, call: GateCall) -> AskOutcome:
        message, questions = self._masked_form(self._redactor.mask(call.question), call.options)
        # The validation form is built from the raw options; only what is shown is masked.
        key = ""
        reason: str | None = None
        answered_by: str | None = None
        try:
            for attempt in range(ASK_ATTEMPTS):
                key = f"{self._prefix}-{call.ordinal}-{attempt}"
                shown = message
                if attempt > 0 and reason is not None:
                    shown = f"{self._redactor.mask(reason)}\n\n{message or ''}".rstrip()
                outcome = await self._sink.request_input(
                    InputRequest(message=shown, questions=questions, key=key)
                )
                answered_by = outcome.answered_by
                if not outcome.accepted:
                    return AskOutcome(None, answered_by)
                value, reason = gate_answer(call.options, outcome.answers)
                if reason is None:
                    return AskOutcome(value, answered_by)
        except asyncio.CancelledError:
            if isinstance(self._sink, ResolvesInput) and key:
                with contextlib.suppress(Exception):
                    await self._sink.input_resolved(key, response="cancel")
            raise
        self._log(f"ask() #{call.ordinal}: no valid answer after {ASK_ATTEMPTS} attempts: {reason}")
        return AskOutcome(None, answered_by)

    def _edits(self, call: GateCall, diff: list[Any]) -> list[FileChange]:
        if self._workspace is None:
            self._log(
                f"approve() #{call.ordinal}: no local working directory; diff paths not previewed"
            )
            return []
        mask = self._redactor.mask

        def text(value: Any) -> bytes | None:
            return mask(str(value)).encode("utf-8") if value is not None else None

        edits: list[FileChange] = []
        for item in diff:
            if not isinstance(item, Mapping) or not isinstance(item.get("path"), str):
                continue
            path = Path(item["path"])
            if not path.is_absolute():
                path = self._workspace / path
            edits.append(
                FileChange(
                    uri=path.as_uri(),
                    before=text(item.get("before")),
                    after=text(item.get("after")),
                )
            )
        return edits

    async def approve(self, call: GateCall) -> bool:
        return (await self.approve_outcome(call)).approved

    async def approve_outcome(self, call: GateCall) -> ConfirmOutcome:
        call_id = f"{self._prefix}-{call.ordinal}"
        summary = self._redactor.mask(call.question)
        diff = call.options.get("diff")
        message = summary
        edits: list[FileChange] = []
        if isinstance(diff, str):
            message = f"{summary}\n\n```diff\n{self._redactor.mask(diff)}\n```"
        elif isinstance(diff, list):
            edits = self._edits(call, diff)
        try:
            await self._sink.tool_call_started(
                call_id, "approve", {"summary": summary}, display_name="Approve"
            )
            outcome = await self._sink.confirm_tool_call(
                ToolConfirmation(
                    call_id=call_id,
                    name="approve",
                    invocation_message=message,
                    display_name="Approve",
                    tool_input={"summary": summary},
                    confirmation_title="Approve this step?",
                    edits=edits,
                )
            )
            if not outcome.approved:
                return ConfirmOutcome(False, outcome.answered_by)
            await self._sink.tool_call_completed(
                call_id,
                {"content": [{"type": "text", "text": "approved"}]},
                success=True,
                past_tense_message="Approved",
            )
            return ConfirmOutcome(True, outcome.answered_by)
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await self._sink.tool_call_confirmed(
                    call_id, approved=False, reason_message="run aborted"
                )
            raise

    async def canvas(self, url: str, title: str | None) -> None:
        mask = self._redactor.mask
        await self._publisher.canvas_set(
            Canvas(
                instance_id="flow-canvas-" + hashlib.sha256(url.encode()).hexdigest()[:16],
                extension_id=CANVAS_EXTENSION_ID,
                canvas_id=CANVAS_ID,
                title=mask(title) if title else None,
                url=mask(url),
            ),
            chat=self.chat,
        )


class MainChatVerbs:
    """The engine's `VerbHost` on the run's main chat: each custom verb call is a tool call."""

    def __init__(
        self,
        *,
        sink: TurnSink,
        publisher: SessionPublisher,
        chat: str,
        session_uri: str,
        redactor: Redactor,
        on_log: Callable[[str], None] | None = None,
        call_id_prefix: str = "verb",
        working_directories: Sequence[str] = (),
        principal: str | None = None,
    ) -> None:
        self._principal = principal
        self._sink = sink
        self._publisher = publisher
        self.chat = chat
        self._session_uri = session_uri
        self._redactor = redactor
        self._on_log = on_log
        self._prefix = call_id_prefix
        self._working_directories = tuple(working_directories)

    def _log(self, message: str) -> None:
        if self._on_log is not None:
            self._on_log(message)

    def context(self, ordinal: int, call_id: str) -> VerbContext:
        # One gate runner per verb call, so its ids (`<prefix>-<ordinal>-<k>`) never meet the
        # verb's own tool call or the script's gates; these requests are not journaled.
        gates = MainChatGates(
            sink=self._sink,
            publisher=self._publisher,
            chat=self.chat,
            redactor=self._redactor,
            on_log=self._on_log,
            call_id_prefix=f"{self._prefix}-{ordinal}",
            working_directories=self._working_directories,
        )
        n = itertools.count()

        async def ask(question: str, options: Mapping[str, Any] | None = None) -> AskOutcome:
            opts = dict(options or {})
            if (reason := options_error("ask", opts)) is not None:
                raise ValueError(reason)
            return await gates.ask_outcome(GateCall(next(n), "ask", str(question), opts))

        async def confirm(summary: str, options: Mapping[str, Any] | None = None) -> ConfirmOutcome:
            opts = dict(options or {})
            if (reason := options_error("approve", opts)) is not None:
                raise ValueError(reason)
            return await gates.approve_outcome(GateCall(next(n), "approve", str(summary), opts))

        return VerbContext(
            session=self._session_uri,
            chat=self.chat,
            principal=self._principal,
            log=self._log,
            ask=ask,
            confirm=confirm,
        )

    @staticmethod
    def _started(call: VerbCall) -> dict[str, Any]:
        return flow.verb_started(name=call.name, ordinal=call.ordinal, phase=call.phase)

    async def started(self, call: VerbCall, args: Sequence[Any]) -> None:
        await self._sink.tool_call_started(
            f"{self._prefix}-{call.ordinal}",
            call.name,
            {"args": list(args)},
            display_name=call.name,
            meta=flow.meta(flow.verb(self._started(call))),
        )

    async def finished(self, call: VerbCall, *, ok: bool, value: Any, error: str | None) -> None:
        text = json.dumps(value, ensure_ascii=False) if ok else (error or "")
        finished = flow.verb_finished(status="ok" if ok else "error", error=None if ok else error)
        await self._sink.tool_call_completed(
            f"{self._prefix}-{call.ordinal}",
            {
                "content": [{"type": "text", "text": self._redactor.mask(text)}],
                "_meta": flow.meta(flow.verb(self._started(call), finished)),
            },
            success=ok,
            past_tense_message=f"{call.name}()" if ok else f"{call.name}() failed",
        )
