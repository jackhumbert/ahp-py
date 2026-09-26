"""Protocol types: ``TypedDict`` views plus the ``Spec`` registry that validates them.

The ``TypedDict``s give ``mypy --strict`` coverage over plain dicts (ADR 0001);
the ``Spec``s are the runtime half, applied where the protocol says a host MUST
validate. Both are hand-written from the vendored TypeScript source of truth
(``vendor/upstream/ts``), never from the published JSON Schemas -- those are a
derived artifact and have shipped a malformed ``$ref`` since ``spec/v0.5.0``
(``docs/research.md`` §5).

Scope: the types reachable from the round-trip corpus and the v0.1 command set.
Channel state and the full action union arrive with the reducers.
"""

from __future__ import annotations

from typing import Any, Final, NotRequired, TypedDict

from ahp_protocol.types.spec import (
    Field,
    ObjectSpec,
    ScalarSpec,
    Spec,
    UnionSpec,
    any_value,
    array_of,
    is_bool,
    is_int,
    is_number,
    is_object,
    is_string,
    one_of,
    ref,
)

__all__ = [
    "SPECS",
    "URI",
    "ActionEnvelope",
    "ActionOrigin",
    "Implementation",
    "InitializeResult",
    "SessionStatus",
    "SessionSummary",
    "Snapshot",
    "StateAction",
    "session_status_flags",
]

URI = str

ROOT_CHANNEL: Final = "ahp-root://"


# ─── SessionStatus ───────────────────────────────────────────────────────────
#
# A BITSET, not an enum. Upstream's own state.schema.json publishes it as
# `enum: [1,2,8,24,32,64]`, which rejects six of the eight status values in its
# own fixture corpora -- one of the reasons we do not generate from the schemas.
#
# JavaScript coerces bitwise operands to SIGNED int32, while the Go, Rust,
# Kotlin and Swift clients all use UNSIGNED 32-bit. For a status with bit 31 set
# the bits agree but the emitted number's sign does not. We mask to u32, which
# agrees with four of the five runtime clients; see docs/research.md §2f.


class SessionStatus:
    """Named bits, from ``types/channels-session/state.ts``.

    Values outside this set are legal and must round-trip. Note ``INPUT_NEEDED``
    is a *combination* -- ``(1 << 3) | (1 << 4)`` -- so it shares a bit with
    ``IN_PROGRESS``; a turn awaiting input is still in progress. Test against
    these with bitwise checks, never equality.
    """

    IDLE: Final = 1  # 1 << 0
    ERROR: Final = 2  # 1 << 1
    IN_PROGRESS: Final = 8  # 1 << 3
    INPUT_NEEDED: Final = 24  # (1 << 3) | (1 << 4)
    IS_READ: Final = 32  # 1 << 5
    IS_ARCHIVED: Final = 64  # 1 << 6

    #: The low five bits are the activity portion the reducers rewrite
    #: wholesale; the flags above it are sticky.
    ACTIVITY_MASK: Final = (1 << 5) - 1

    MASK: Final = 0xFFFFFFFF


def session_status_flags(status: int) -> int:
    """Normalise a status to unsigned 32-bit, preserving unknown high bits."""
    return status & SessionStatus.MASK


# ─── TypedDict views ─────────────────────────────────────────────────────────
#
# `total=False` throughout with NotRequired on the optionals, so a value read
# from the wire type-checks even when it carries keys we do not model.


class ActionOrigin(TypedDict):
    clientId: str
    clientSeq: int


class StateAction(TypedDict):
    """Open by design: every action carries `type`, the rest is variant-specific."""

    type: str


class ActionEnvelope(TypedDict):
    channel: URI
    action: StateAction
    serverSeq: int
    origin: NotRequired[ActionOrigin | None]
    rejectionReason: NotRequired[str]


class Snapshot(TypedDict):
    resource: URI
    state: dict[str, Any]
    fromSeq: int


class Implementation(TypedDict):
    name: str
    version: NotRequired[str]
    title: NotRequired[str]


class SessionSummary(TypedDict):
    resource: URI
    provider: str
    title: str
    status: int
    createdAt: str
    modifiedAt: str


class InitializeResult(TypedDict):
    protocolVersion: str
    serverSeq: int
    snapshots: list[Snapshot]
    serverInfo: NotRequired[Implementation]
    defaultDirectory: NotRequired[URI]
    completionTriggerCharacters: NotRequired[list[str]]
    terminalCommandPrefix: NotRequired[str]
    telemetry: NotRequired[dict[str, Any]]


# ─── Specs ───────────────────────────────────────────────────────────────────


def _is_response_id(value: Any, path: str) -> list[str]:
    """A response id is a string, a number, or null (JSON-RPC 2.0 §5)."""
    if value is None or isinstance(value, str):
        return []
    return is_int(value, path)


_POSITION = ObjectSpec(
    "Position",
    (Field("line", True, is_int), Field("character", True, is_int)),
)

_RANGE = ObjectSpec(
    "Range",
    (Field("start", True, ref("Position")), Field("end", True, ref("Position"))),
)

_ACTION_ORIGIN = ObjectSpec(
    "ActionOrigin",
    (Field("clientId", True, is_string), Field("clientSeq", True, is_int)),
)

#: Every action carries `type`; payloads are variant-specific and open. The
#: reducers -- not this spec -- know the per-action shapes.
_STATE_ACTION = ObjectSpec("StateAction", (Field("type", True, is_string),))

_ACTION_ENVELOPE = ObjectSpec(
    "ActionEnvelope",
    (
        Field("channel", True, is_string),
        Field("action", True, ref("StateAction")),
        # `serverSeq` routinely exceeds int32: fixture 016 carries 2148131814.
        Field("serverSeq", True, is_int),
        Field("origin", False, ref("ActionOrigin")),
        Field("rejectionReason", False, is_string),
    ),
)

_SNAPSHOT = ObjectSpec(
    "Snapshot",
    (
        Field("resource", True, is_string),
        Field("state", True, is_object),
        Field("fromSeq", True, is_int),
    ),
)

_IMPLEMENTATION = ObjectSpec(
    "Implementation",
    (
        Field("name", True, is_string),
        Field("version", False, is_string),
        Field("title", False, is_string),
    ),
)

_SESSION_SUMMARY_FIELDS = (
    Field("resource", True, is_string),
    Field("provider", True, is_string),
    Field("title", True, is_string),
    Field("status", True, is_int),
    Field("createdAt", True, is_string),
    Field("modifiedAt", True, is_string),
)

_SESSION_SUMMARY = ObjectSpec("SessionSummary", _SESSION_SUMMARY_FIELDS)

#: `Partial<SessionSummary>` -- identity fields are omitted by senders, and
#: fixture 020 is the fully-empty object, so nothing is required.
_PARTIAL_SESSION_SUMMARY = ObjectSpec(
    "PartialSessionSummary",
    tuple(Field(f.name, False, f.check) for f in _SESSION_SUMMARY_FIELDS),
)

_SESSION_ADDED_PARAMS = ObjectSpec(
    "SessionAddedParams",
    (Field("channel", True, is_string), Field("summary", True, ref("SessionSummary"))),
)

_INITIALIZE_RESULT = ObjectSpec(
    "InitializeResult",
    (
        Field("protocolVersion", True, is_string),
        Field("serverSeq", True, is_int),
        # MUST always be an array: MultiHostClient calls .find() on it without a
        # guard, so omitting it puts the client in an endless reconnect loop.
        Field("snapshots", True, array_of(ref("Snapshot"))),
        Field("serverInfo", False, ref("Implementation")),
        Field("defaultDirectory", False, is_string),
        Field("completionTriggerCharacters", False, array_of(is_string)),
        Field("terminalCommandPrefix", False, is_string),
        Field("telemetry", False, is_object),
    ),
)

#: `string | { markdown: string }`.
_STRING_OR_MARKDOWN = ScalarSpec(
    "StringOrMarkdown",
    one_of(is_string, ObjectSpec("Markdown", (Field("markdown", True, is_string),)).validate),
)

#: A bitset. Unknown bits are legal and MUST survive (fixture 005: 2147483720).
_SESSION_STATUS = ScalarSpec("SessionStatus", is_int)

_CHANGESET_OPERATION_TARGET = UnionSpec(
    "ChangesetOperationTarget",
    key="kind",
    common=(Field("kind", True, is_string),),
    variants={
        "resource": ObjectSpec("Target.resource", (Field("resource", True, is_string),)),
        "range": ObjectSpec(
            "Target.range",
            (Field("resource", True, is_string), Field("range", True, ref("Range"))),
        ),
    },
)

_CHAT_INPUT_QUESTION = UnionSpec(
    "ChatInputQuestion",
    key="kind",
    common=(
        Field("kind", True, is_string),
        Field("id", True, is_string),
        Field("message", True, is_string),
    ),
    variants={
        "number": ObjectSpec(
            "Question.number",
            (
                Field("min", False, is_number),
                Field("max", False, is_number),
                Field("defaultValue", False, is_number),
            ),
        ),
        "integer": ObjectSpec(
            "Question.integer",
            (
                Field("min", False, is_int),
                Field("max", False, is_int),
                Field("defaultValue", False, is_int),
            ),
        ),
    },
)

_CHAT_SOURCE = UnionSpec(
    "ChatSource",
    key="kind",
    common=(Field("kind", True, is_string),),
    variants={
        "sideChat": ObjectSpec(
            "ChatSource.sideChat",
            (Field("chat", True, is_string), Field("turnId", False, is_string)),
        ),
        "fork": ObjectSpec(
            "ChatSource.fork",
            (Field("chat", True, is_string), Field("turnId", False, is_string)),
        ),
    },
)

#: One scoped decision (0.8.0). `kind` is `global`, `workspace` or `session`;
#: only a `workspace` decision carries a `uri`.
_CUSTOMIZATION_ENABLEMENT = ObjectSpec(
    "CustomizationEnablement",
    (
        Field("kind", True, is_string),
        Field("enabled", True, is_bool),
        Field("uri", False, is_string),
    ),
)

#: Customizations are an open union keyed by `type`; fixture 003 carries an
#: unknown one that must survive verbatim.
_CUSTOMIZATION = UnionSpec(
    "Customization",
    key="type",
    common=(Field("type", True, is_string),),
    variants={
        "plugin": ObjectSpec(
            "Customization.plugin",
            (
                Field("id", True, is_string),
                Field("name", False, is_string),
                Field("uri", False, is_string),
                # Since 0.8.0 a plugin carries scoped decisions, not `enabled`.
                Field("enablement", False, array_of(_CUSTOMIZATION_ENABLEMENT.validate)),
                Field("children", False, array_of(ref("Customization"))),
            ),
        ),
        "agent": ObjectSpec(
            "Customization.agent",
            (
                Field("id", True, is_string),
                Field("name", False, is_string),
                Field("uri", False, is_string),
                Field("description", False, is_string),
                Field("model", False, is_string),
                Field("enabled", False, is_bool),
            ),
        ),
    },
)

#: JSON-RPC 2.0 framing. Classified by key presence, not by a discriminator:
#: a request has id+method, a notification method only, a response id plus
#: exactly one of result/error.
_JSON_RPC_MESSAGE = ScalarSpec(
    "JsonRpcMessage",
    one_of(
        ObjectSpec(
            "JsonRpcRequest",
            (
                Field("jsonrpc", True, is_string),
                Field("id", True, one_of(is_int, is_string)),
                Field("method", True, is_string),
                Field("params", False, any_value),
            ),
        ).validate,
        ObjectSpec(
            "JsonRpcNotification",
            (Field("jsonrpc", True, is_string), Field("method", True, is_string)),
        ).validate,
        ObjectSpec(
            "JsonRpcSuccess",
            (
                Field("jsonrpc", True, is_string),
                Field("id", True, one_of(is_int, is_string)),
                Field("result", True, any_value),
            ),
        ).validate,
        ObjectSpec(
            "JsonRpcError",
            (
                Field("jsonrpc", True, is_string),
                # A parse-error response carries a null id, per JSON-RPC 2.0.
                Field("id", True, _is_response_id),
                Field(
                    "error",
                    True,
                    ObjectSpec(
                        "ErrorObject",
                        (
                            Field("code", True, is_int),
                            Field("message", True, is_string),
                            Field("data", False, any_value),
                        ),
                    ).validate,
                ),
            ),
        ).validate,
    ),
)


SPECS: Final[dict[str, Spec]] = {
    spec.name: spec
    for spec in (
        _ACTION_ENVELOPE,
        _ACTION_ORIGIN,
        _CHANGESET_OPERATION_TARGET,
        _CHAT_INPUT_QUESTION,
        _CHAT_SOURCE,
        _CUSTOMIZATION,
        _IMPLEMENTATION,
        _INITIALIZE_RESULT,
        _JSON_RPC_MESSAGE,
        _PARTIAL_SESSION_SUMMARY,
        _POSITION,
        _RANGE,
        _SESSION_ADDED_PARAMS,
        _SESSION_STATUS,
        _SESSION_SUMMARY,
        _SNAPSHOT,
        _STATE_ACTION,
        _STRING_OR_MARKDOWN,
    )
}
