"""Structured turns: validate an agent's result against the client's schema.

An ahp-py extension ahead of the spec (see ``ahp_protocol.ext.structured``).
This module holds the pure parts: finding the schema on a message, validating
against it, pulling a JSON value out of free text for a provider that cannot
take a host tool, and the prompt texts. The turn driver lives alongside them.
"""

from __future__ import annotations

import dataclasses
import json
import re
import uuid
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any, Final

import jsonschema
import jsonschema.exceptions
import jsonschema.protocols
import jsonschema.validators
import referencing
import referencing.exceptions
from ahp_protocol.ext.structured import (
    OUTPUT_SCHEMA_META_KEY,
    STRUCTURED_OUTPUT_ERROR_TYPE,
    STRUCTURED_OUTPUT_TOOL,
    STRUCTURED_OUTPUT_TOOL_META_KEY,
    needs_wrapping,
    tool_input_schema,
    unwrap,
    wrap,
)

from ahp_host.provider.base import (
    AgentSession,
    ClientToolCall,
    HostTool,
    ReturnsStructuredOutput,
    ToolResult,
    UserMessage,
)

if TYPE_CHECKING:
    from ahp_host.core.turn import ActionTurnSink

__all__ = [
    "FALLBACK_FOLLOW_UP",
    "FALLBACK_PROMPT",
    "FOLLOW_UP_NOTICE",
    "MAX_ATTEMPTS",
    "MAX_FOLLOW_UPS",
    "TOOL_FOLLOW_UP",
    "TOOL_PROMPT",
    "StructuredOutputFailed",
    "StructuredTurn",
    "last_json_block",
    "make_validator",
    "output_schema",
    "schema_errors",
    "schema_text",
]

#: Invalid StructuredOutput calls per turn before giving up (the M3 design's
#: default).
MAX_ATTEMPTS: Final = 3
#: Extra messages the host sends inside the same turn ("up to 2 follow-up
#: turns" in the M3 design).
MAX_FOLLOW_UPS: Final = 2

TOOL_PROMPT: Final = (
    "\n\nWhen you have the final result, call the `StructuredOutput` tool once, "
    "with the result as its arguments. They must match this JSON Schema:\n"
    "```json\n{schema}\n```"
)
TOOL_FOLLOW_UP: Final = (
    "You have not returned a valid result yet. Call the `StructuredOutput` tool "
    "now, with arguments matching this JSON Schema:\n```json\n{schema}\n```"
)
FALLBACK_PROMPT: Final = (
    "\n\nEnd your reply with the final result as one JSON value in a ```json "
    "code block, matching this JSON Schema:\n```json\n{schema}\n```"
)
FALLBACK_FOLLOW_UP: Final = (
    "Your last reply did not contain a valid result: {error}\n"
    "Reply with only JSON matching this JSON Schema, and nothing else:\n"
    "```json\n{schema}\n```"
)
FOLLOW_UP_NOTICE: Final = (
    "Asked the agent again for a result matching the output schema (attempt {attempt} of {total})."
)
NO_TOOL_FALLBACK_NOTICE: Final = (
    "The agent never called StructuredOutput, so it was asked for the result as JSON in its "
    "reply instead."
)

_FENCE: Final = re.compile(r"```[^\n`]*\n(.*?)```", re.DOTALL)


class StructuredOutputFailed(Exception):
    """The turn produced no valid structured result."""

    error_type: Final = STRUCTURED_OUTPUT_ERROR_TYPE


def output_schema(message: Mapping[str, Any]) -> dict[str, Any] | None:
    """The schema a wire ``Message`` asks for, or ``None`` if it asks for none."""
    meta = message.get("_meta")
    if not isinstance(meta, Mapping) or OUTPUT_SCHEMA_META_KEY not in meta:
        return None
    value = meta[OUTPUT_SCHEMA_META_KEY]
    if isinstance(value, Mapping):
        return dict(value)
    raise StructuredOutputFailed(f"_meta['{OUTPUT_SCHEMA_META_KEY}'] must be a JSON Schema object")


def _refs(node: Any) -> list[str]:
    """Every ``$ref`` / ``$dynamicRef`` string anywhere in *node*."""
    found: list[str] = []
    stack = [node]
    while stack:
        item = stack.pop()
        if isinstance(item, Mapping):
            for key, child in item.items():
                if key in ("$ref", "$dynamicRef") and isinstance(child, str):
                    found.append(child)
                else:
                    stack.append(child)
        elif isinstance(item, list):
            stack.extend(item)
    return found


def make_validator(schema: Mapping[str, Any]) -> jsonschema.protocols.Validator:
    """A validator for *schema*, checked to be a valid schema first.

    The draft comes from ``$schema`` (2020-12 when absent). No format checker:
    formats stay annotations, as the specification's default has it.

    The schema arrives from any connected client, so a reference may only point
    inside it (``#...``): a remote or file ``$ref`` is refused here, before the
    agent runs, rather than fetched. The validator also gets an empty
    ``referencing.Registry``, which retrieves nothing -- jsonschema before 4.18
    fetched remote references while validating, which is why the floor is 4.18.
    """
    cls = jsonschema.validators.validator_for(schema, default=jsonschema.Draft202012Validator)
    try:
        cls.check_schema(dict(schema))
    except jsonschema.exceptions.SchemaError as exc:
        raise StructuredOutputFailed(
            f"The output schema is not a valid JSON Schema: {exc.message}"
        ) from exc
    external = [ref for ref in _refs(schema) if not ref.startswith("#")]
    if external:
        raise StructuredOutputFailed(
            f"The output schema may only reference itself ('#...'), not {external[0]!r}."
        )
    return cls(schema, registry=referencing.Registry())


def schema_errors(validator: jsonschema.protocols.Validator, value: Any) -> str | None:
    """``None`` when *value* is valid, else up to five errors, one per line.

    A reference that does not resolve (``#/$defs/missing``) is a defect of the
    schema, not of the value, so it fails the turn rather than asking the agent
    to retry.
    """
    try:
        errors = sorted(
            validator.iter_errors(value), key=lambda e: [str(p) for p in e.absolute_path]
        )
    except referencing.exceptions.Unresolvable as exc:
        raise StructuredOutputFailed(
            f"The output schema has a reference that does not resolve: {exc}"
        ) from exc
    if not errors:
        return None
    return "\n".join(
        f"{'/'.join(str(p) for p in e.absolute_path) or '(root)'}: {e.message}" for e in errors[:5]
    )


def last_json_block(text: str) -> tuple[bool, Any]:
    """The last JSON value in *text*, as ``(found, value)``.

    A pair because JSON ``null`` is a valid result. Fenced blocks win, last
    first; failing that, the last value a left-to-right scan decodes (an outer
    value swallows what is inside it).
    """
    for body in reversed(_FENCE.findall(text)):
        try:
            return True, json.loads(body)
        except ValueError:
            continue
    decoder = json.JSONDecoder()
    found, last, i = False, None, 0
    while i < len(text):
        if text[i] in "{[":
            try:
                last, i = decoder.raw_decode(text, i)
            except ValueError:
                i += 1
                continue
            found = True
        else:
            i += 1
    return found, last


def schema_text(schema: Mapping[str, Any]) -> str:
    """The schema as pretty, stable JSON for a prompt."""
    return json.dumps(schema, indent=2, sort_keys=True)


def _accepted_result(value: Any, schema: Mapping[str, Any]) -> dict[str, Any]:
    """The ``ToolCallResult`` of an accepted StructuredOutput call."""
    return {
        "content": [{"type": "text", "text": "Accepted."}],
        "structuredContent": wrap(value, schema),
    }


class StructuredTurn:
    """One structured turn: ask the agent for a result, validate, retry.

    All the turn-level logic lives here so providers only have to expose a tool
    (:class:`~ahp_host.provider.base.ReturnsStructuredOutput`). A provider that
    cannot gets the fallback instead: the schema in the prompt, the reply's last
    JSON block parsed here.
    """

    def __init__(self, schema: Mapping[str, Any], *, max_attempts: int = MAX_ATTEMPTS) -> None:
        self.schema = schema
        self.max_attempts = max_attempts
        # Before the agent runs: a bad schema fails the turn without a message.
        self._validator = make_validator(schema)
        self.tool = HostTool(
            name=STRUCTURED_OUTPUT_TOOL,
            title="Structured output",
            description=(
                "Return the final result of this turn. Call it once, with the result "
                "as the arguments."
            ),
            input_schema=tool_input_schema(schema),
        )
        self.accepted = False
        self.value: Any = None
        self.failures = 0
        self.last_error: str | None = None

    @property
    def exhausted(self) -> bool:
        return self.failures >= self.max_attempts

    def check(self, arguments: Any) -> tuple[Any, str | None]:
        """The result value in *arguments*, and why it is not valid (or ``None``)."""
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except ValueError:
                return None, "The arguments are not JSON."
        if not isinstance(arguments, Mapping):
            return None, "The arguments must be a JSON object."
        if needs_wrapping(self.schema) and "value" not in arguments:
            return None, 'Put the result under "value".'
        value = unwrap(arguments, self.schema)
        return value, schema_errors(self._validator, value)

    async def handle(self, sink: ActionTurnSink, call: ClientToolCall) -> ToolResult:
        """Answer one StructuredOutput call from the agent."""
        if self.accepted:
            return ToolResult(
                response="decline", reason="A structured result was already accepted for this turn."
            )
        if self.exhausted:
            return ToolResult(response="decline", reason="No attempts left: the turn has failed.")
        await sink.tool_call_started(
            call.call_id,
            STRUCTURED_OUTPUT_TOOL,
            call.tool_input,
            display_name="Structured output",
            meta={STRUCTURED_OUTPUT_TOOL_META_KEY: True},
        )
        try:
            value, error = self.check(call.tool_input)
        except StructuredOutputFailed as exc:
            # A defect of the schema itself: no retry can fix it.
            self.failures = self.max_attempts
            self.last_error = str(exc)
            await sink.tool_call_completed(
                call.call_id,
                {"content": [{"type": "text", "text": str(exc)}], "error": {"message": str(exc)}},
                success=False,
                past_tense_message="The output schema could not be used",
            )
            return ToolResult(response="decline", reason=str(exc))
        if error is None:
            self.accepted = True
            self.value = value
            result = _accepted_result(value, self.schema)
            past = "Returned a structured result"
            await sink.tool_call_completed(
                call.call_id, result, success=True, past_tense_message=past
            )
            return ToolResult(value={**result, "success": True, "pastTenseMessage": past})
        self.failures += 1
        self.last_error = error
        left = self.max_attempts - self.failures
        reason = f"The arguments do not match the schema:\n{error}\n" + (
            f"Call StructuredOutput again with corrected arguments ({left} attempt(s) left)."
            if left > 0
            else "No attempts left."
        )
        await sink.tool_call_completed(
            call.call_id,
            {"content": [{"type": "text", "text": reason}], "error": {"message": reason}},
            success=False,
            past_tense_message="The result did not match the schema",
        )
        return ToolResult(response="decline", reason=reason)

    async def run(
        self,
        session: AgentSession,
        message: UserMessage,
        sink: ActionTurnSink,
        *,
        is_active: Callable[[], bool],
    ) -> None:
        """Drive the agent until it returns a valid result, or raise."""
        capable = isinstance(session, ReturnsStructuredOutput) and session.offers_host_tools()
        # The tool takes the (possibly wrapped) tool schema; the fallback reply is
        # the bare value, so its prompt shows the original.
        schema_json = schema_text(self.tool.input_schema if capable else self.schema)

        def follow_up(text: str, host_tools: tuple[HostTool, ...]) -> UserMessage:
            return dataclasses.replace(
                message,
                text=text,
                raw={"text": text, "origin": {"kind": "user"}},
                attached_chats=(),
                continues_turn=True,
                host_tools=host_tools,
            )

        tool_never_called = False
        if capable:
            sink.host_tools[STRUCTURED_OUTPUT_TOOL] = lambda call: self.handle(sink, call)
            first = dataclasses.replace(
                message,
                text=message.text + TOOL_PROMPT.format(schema=schema_json),
                host_tools=(self.tool,),
            )
            await session.send_user_message(first, sink)
            for n in range(MAX_FOLLOW_UPS):
                if self.accepted or self.exhausted or not is_active():
                    break
                await sink.system_notification(
                    FOLLOW_UP_NOTICE.format(attempt=n + 2, total=MAX_FOLLOW_UPS + 1)
                )
                await session.send_user_message(
                    follow_up(TOOL_FOLLOW_UP.format(schema=schema_json), (self.tool,)), sink
                )
            if not self.accepted and self.failures == 0 and is_active():
                # The agent never saw or never used the tool: an ACP agent that
                # cached its tool list before the turn added StructuredOutput
                # keeps the old list. Ask for the JSON in the reply instead,
                # once, rather than failing a turn the fallback can still save.
                tool_never_called = True
                await sink.system_notification(NO_TOOL_FALLBACK_NOTICE)
                fallback_schema = schema_text(self.schema)
                outgoing = follow_up(FALLBACK_PROMPT.format(schema=fallback_schema).strip(), ())
                reply = await self._send_collecting(session, outgoing, sink)
                if is_active():
                    await self._parse_reply(reply, sink)
        else:
            outgoing = dataclasses.replace(
                message, text=message.text + FALLBACK_PROMPT.format(schema=schema_json)
            )
            for n in range(MAX_FOLLOW_UPS + 1):
                if n:
                    await sink.system_notification(
                        FOLLOW_UP_NOTICE.format(attempt=n + 1, total=MAX_FOLLOW_UPS + 1)
                    )
                    outgoing = follow_up(
                        FALLBACK_FOLLOW_UP.format(error=self.last_error, schema=schema_json), ()
                    )
                reply = await self._send_collecting(session, outgoing, sink)
                if not is_active():
                    break
                await self._parse_reply(reply, sink)
                if self.accepted:
                    break

        if not is_active():
            # The provider already ended the turn (cancelled or failed).
            return
        if not self.accepted:
            if tool_never_called:
                raise StructuredOutputFailed(
                    "The agent did not call StructuredOutput, and its reply had no valid "
                    f"result either: {self.last_error}"
                )
            raise StructuredOutputFailed(
                f"No valid structured result after {self.failures} attempt(s): {self.last_error}"
            )

    @staticmethod
    async def _send_collecting(
        session: AgentSession, message: UserMessage, sink: ActionTurnSink
    ) -> str:
        """Send *message* and return the text the agent streamed in reply."""
        buffer: list[str] = []
        sink.on_text = buffer.append
        try:
            await session.send_user_message(message, sink)
        finally:
            sink.on_text = None
        return "".join(buffer)

    async def _parse_reply(self, reply: str, sink: ActionTurnSink) -> None:
        """Fallback: take the reply's last JSON block as the result, if valid."""
        found, value = last_json_block(reply)
        if not found:
            self.failures += 1
            self.last_error = "The reply contained no JSON."
            return
        error = schema_errors(self._validator, value)
        if error is not None:
            self.failures += 1
            self.last_error = error
            return
        self.accepted = True
        self.value = value
        call_id = f"structured-{uuid.uuid4().hex}"
        await sink.tool_call_started(
            call_id,
            STRUCTURED_OUTPUT_TOOL,
            wrap(value, self.schema),
            display_name="Structured output",
            meta={STRUCTURED_OUTPUT_TOOL_META_KEY: True},
        )
        await sink.tool_call_completed(
            call_id,
            _accepted_result(value, self.schema),
            success=True,
            past_tense_message="Returned a structured result",
        )
