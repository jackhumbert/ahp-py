"""Typed `session/update`.

`parse_update` turns a `session/update` notification's params into one of the
frozen dataclasses below. Field names are the schema's (camelCase on the wire,
snake_case here); every update also keeps `raw`, the `update` object exactly as
the agent sent it, so a field this module does not model is never lost.

Parsing never raises. A discriminator this module has no type for comes back as
`UnknownUpdate`, as does a known one missing a required field (with `problem`
saying which). That is the point of this module: a protocol revision that adds a
kind reaches the caller at once, instead of vanishing until a library catches up.

`tests/test_updates.py` checks the vendored schema's discriminators against
`PARSERS` and `DELIBERATELY_UNTYPED`, so a schema bump that adds one fails until
someone decides which it is.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final


@dataclass(frozen=True, kw_only=True)
class SessionUpdate:
    """What every update carries."""

    session_id: str
    #: The `update` object as received (it includes `sessionUpdate`).
    raw: dict[str, Any]
    #: The update's `_meta`, if any.
    meta: dict[str, Any] | None = None


@dataclass(frozen=True, kw_only=True)
class MessageChunk(SessionUpdate):
    #: A content block (`{"type": "text", "text": ...}` and friends).
    content: dict[str, Any]
    message_id: str | None = None


@dataclass(frozen=True, kw_only=True)
class UserMessageChunk(MessageChunk):
    pass


@dataclass(frozen=True, kw_only=True)
class AgentMessageChunk(MessageChunk):
    pass


@dataclass(frozen=True, kw_only=True)
class AgentThoughtChunk(MessageChunk):
    pass


@dataclass(frozen=True, kw_only=True)
class ToolCall(SessionUpdate):
    tool_call_id: str
    title: str
    name: str | None = None
    kind: str | None = None
    status: str | None = None
    content: list[Any]
    locations: list[Any]
    raw_input: Any = None
    raw_output: Any = None


@dataclass(frozen=True, kw_only=True)
class ToolCallUpdate(SessionUpdate):
    """Only what changed: every field but the id may be `None`."""

    tool_call_id: str
    title: str | None = None
    name: str | None = None
    kind: str | None = None
    status: str | None = None
    content: list[Any] | None = None
    locations: list[Any] | None = None
    raw_input: Any = None
    raw_output: Any = None


@dataclass(frozen=True, kw_only=True)
class PlanUpdate(SessionUpdate):
    entries: list[Any]


@dataclass(frozen=True, kw_only=True)
class AvailableCommandsUpdate(SessionUpdate):
    commands: list[Any]


@dataclass(frozen=True, kw_only=True)
class CurrentModeUpdate(SessionUpdate):
    mode_id: str


@dataclass(frozen=True, kw_only=True)
class ConfigOptionUpdate(SessionUpdate):
    config_options: list[Any]


@dataclass(frozen=True, kw_only=True)
class SessionInfoUpdate(SessionUpdate):
    title: str | None = None
    updated_at: str | None = None


@dataclass(frozen=True, kw_only=True)
class UsageUpdate(SessionUpdate):
    used: int
    size: int
    cost: dict[str, Any] | None = None


@dataclass(frozen=True, kw_only=True)
class NoticeUpdate(SessionUpdate):
    """Information for the user that is not part of session history."""

    severity: str
    title: str
    description: str | None = None


@dataclass(frozen=True, kw_only=True)
class CompactionUpdate(SessionUpdate):
    compaction_id: str
    status: str
    summary: list[Any] | None = None
    error: str | None = None


@dataclass(frozen=True, kw_only=True)
class CompactionSummaryChunk(SessionUpdate):
    compaction_id: str
    content: dict[str, Any]


@dataclass(frozen=True, kw_only=True)
class UnknownUpdate(SessionUpdate):
    """An update this module cannot type: a new kind, or a known one malformed."""

    kind: str
    problem: str | None = None


class _MissingError(Exception):
    def __init__(self, field: str) -> None:
        super().__init__(f"missing required field {field!r}")


def _req(raw: Mapping[str, Any], key: str) -> Any:
    if raw.get(key) is None:
        raise _MissingError(key)
    return raw[key]


def _str(raw: Mapping[str, Any], key: str) -> str | None:
    value = raw.get(key)
    return value if isinstance(value, str) else None


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _opt_list(value: Any) -> list[Any] | None:
    return value if isinstance(value, list) else None


def _dict(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _MissingError("content")
    return value


def _chunk(cls: type[MessageChunk]) -> Callable[[str, dict[str, Any], dict[str, Any] | None], Any]:
    def parse(sid: str, raw: dict[str, Any], meta: dict[str, Any] | None) -> SessionUpdate:
        return cls(
            session_id=sid,
            raw=raw,
            meta=meta,
            content=_dict(raw.get("content")),
            message_id=_str(raw, "messageId"),
        )

    return parse


def _tool_call(sid: str, raw: dict[str, Any], meta: dict[str, Any] | None) -> SessionUpdate:
    return ToolCall(
        session_id=sid,
        raw=raw,
        meta=meta,
        tool_call_id=str(_req(raw, "toolCallId")),
        title=str(_req(raw, "title")),
        name=_str(raw, "name"),
        kind=_str(raw, "kind"),
        status=_str(raw, "status"),
        content=_list(raw.get("content")),
        locations=_list(raw.get("locations")),
        raw_input=raw.get("rawInput"),
        raw_output=raw.get("rawOutput"),
    )


def _tool_call_update(sid: str, raw: dict[str, Any], meta: dict[str, Any] | None) -> SessionUpdate:
    return ToolCallUpdate(
        session_id=sid,
        raw=raw,
        meta=meta,
        tool_call_id=str(_req(raw, "toolCallId")),
        title=_str(raw, "title"),
        name=_str(raw, "name"),
        kind=_str(raw, "kind"),
        status=_str(raw, "status"),
        content=_opt_list(raw.get("content")),
        locations=_opt_list(raw.get("locations")),
        raw_input=raw.get("rawInput"),
        raw_output=raw.get("rawOutput"),
    )


def _plan(sid: str, raw: dict[str, Any], meta: dict[str, Any] | None) -> SessionUpdate:
    return PlanUpdate(session_id=sid, raw=raw, meta=meta, entries=_list(_req(raw, "entries")))


def _commands(sid: str, raw: dict[str, Any], meta: dict[str, Any] | None) -> SessionUpdate:
    return AvailableCommandsUpdate(
        session_id=sid, raw=raw, meta=meta, commands=_list(_req(raw, "availableCommands"))
    )


def _mode(sid: str, raw: dict[str, Any], meta: dict[str, Any] | None) -> SessionUpdate:
    return CurrentModeUpdate(
        session_id=sid, raw=raw, meta=meta, mode_id=str(_req(raw, "currentModeId"))
    )


def _config(sid: str, raw: dict[str, Any], meta: dict[str, Any] | None) -> SessionUpdate:
    return ConfigOptionUpdate(
        session_id=sid, raw=raw, meta=meta, config_options=_list(_req(raw, "configOptions"))
    )


def _info(sid: str, raw: dict[str, Any], meta: dict[str, Any] | None) -> SessionUpdate:
    return SessionInfoUpdate(
        session_id=sid,
        raw=raw,
        meta=meta,
        title=_str(raw, "title"),
        updated_at=_str(raw, "updatedAt"),
    )


def _usage(sid: str, raw: dict[str, Any], meta: dict[str, Any] | None) -> SessionUpdate:
    cost = raw.get("cost")
    return UsageUpdate(
        session_id=sid,
        raw=raw,
        meta=meta,
        used=int(_req(raw, "used")),
        size=int(_req(raw, "size")),
        cost=cost if isinstance(cost, dict) else None,
    )


def _notice(sid: str, raw: dict[str, Any], meta: dict[str, Any] | None) -> SessionUpdate:
    return NoticeUpdate(
        session_id=sid,
        raw=raw,
        meta=meta,
        severity=str(_req(raw, "severity")),
        title=str(_req(raw, "title")),
        description=_str(raw, "description"),
    )


def _compaction(sid: str, raw: dict[str, Any], meta: dict[str, Any] | None) -> SessionUpdate:
    return CompactionUpdate(
        session_id=sid,
        raw=raw,
        meta=meta,
        compaction_id=str(_req(raw, "compactionId")),
        status=str(_req(raw, "status")),
        summary=_opt_list(raw.get("summary")),
        error=_str(raw, "error"),
    )


def _compaction_chunk(sid: str, raw: dict[str, Any], meta: dict[str, Any] | None) -> SessionUpdate:
    return CompactionSummaryChunk(
        session_id=sid,
        raw=raw,
        meta=meta,
        compaction_id=str(_req(raw, "compactionId")),
        content=_dict(_req(raw, "content")),
    )


_Parser = Callable[[str, dict[str, Any], dict[str, Any] | None], SessionUpdate]

#: `sessionUpdate` value -> parser. Every kind in the stable schema is here.
PARSERS: Final[Mapping[str, _Parser]] = {
    "user_message_chunk": _chunk(UserMessageChunk),
    "agent_message_chunk": _chunk(AgentMessageChunk),
    "agent_thought_chunk": _chunk(AgentThoughtChunk),
    "tool_call": _tool_call,
    "tool_call_update": _tool_call_update,
    "plan": _plan,
    "available_commands_update": _commands,
    "current_mode_update": _mode,
    "config_option_update": _config,
    "session_info_update": _info,
    "usage_update": _usage,
    "notice": _notice,
    "compaction_update": _compaction,
    "compaction_summary_chunk": _compaction_chunk,
}

#: Kinds the schema defines (only its unstable track, today) that we have chosen
#: not to type yet. They still arrive, as `UnknownUpdate`. Add a type, or leave
#: the kind here, when the schema test fails on a new one.
DELIBERATELY_UNTYPED: Final[tuple[str, ...]] = (
    "plan_update",
    "plan_removed",
    "subagent_update",
    "session_message",
    "session_message_chunk",
)


def parse_update(params: Any) -> SessionUpdate:
    """Parse a `session/update` notification's params. Never raises."""
    if not isinstance(params, dict):
        return UnknownUpdate(session_id="", raw={}, kind="", problem="params is not an object")
    session_id = params.get("sessionId")
    sid = session_id if isinstance(session_id, str) else ""
    update = params.get("update")
    if not isinstance(update, dict):
        return UnknownUpdate(
            session_id=sid, raw={}, kind="", problem="params has no 'update' object"
        )
    kind = update.get("sessionUpdate")
    kind_name = kind if isinstance(kind, str) else ""
    raw_meta = update.get("_meta")
    meta = raw_meta if isinstance(raw_meta, dict) else None
    parser = PARSERS.get(kind_name)
    if parser is None:
        problem = None if kind_name else "update has no 'sessionUpdate' discriminator"
        return UnknownUpdate(session_id=sid, raw=update, meta=meta, kind=kind_name, problem=problem)
    try:
        return parser(sid, update, meta)
    except _MissingError as exc:
        problem = str(exc)
    except (TypeError, ValueError) as exc:
        problem = f"malformed field: {exc}"
    return UnknownUpdate(session_id=sid, raw=update, meta=meta, kind=kind_name, problem=problem)
