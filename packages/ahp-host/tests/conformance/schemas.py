"""The vendored upstream JSON Schemas, as an assertion.

The conformance corpora prove our REDUCERS agree with upstream's. Nothing
proved that what we PUT ON THE WIRE matches what the spec declares, and that is
where this project's defects have actually lived:

* `chat/error` without the required `errorType` -- every failed turn rendered
  `Error: (undefined) ...`
* `TerminalInfo` without the required `title` and `claim`
* `terminal/commandExecuted.timestamp` as an ISO string where `number` is
  declared
* tool result content keyed `kind` where the discriminant is `type`
* `Changeset.kind` where the field is `changeKind`
* `CompletionItem.label`/`detail`, invented and read by nobody

Every one is a required field absent or a declared type wrong, and every one is
mechanically catchable against schemas that were already sitting in this
repository. 247/247 reducer fixtures were green through all of them, because a
fixture hands the reducer a ready-made action and proves only that we CONSUME
the shape -- never that we produce it.

The index is built FROM the schema: each action definition carries
``type: {const: "chat/error"}``, so the mapping from wire type to definition is
derived rather than hand-kept. A hand-kept map would not contain the action
someone just added.
"""

from __future__ import annotations

import json
from functools import cache
from typing import Any

import pytest
from agent_host_protocol.conformance.corpus import CORPUS_ROOT

jsonschema = pytest.importorskip("jsonschema")

#: From the DEPENDENCY's copy of the corpus, not from a `vendor/` tree of our
#: own. There is exactly one vendored spec now and it lives in
#: `agent-host-protocol`; a second copy here could pin a different tag and this
#: gate would then be asserting against a spec the reducers do not implement.
SCHEMA_DIR = CORPUS_ROOT / "schema"

__all__ = [
    "action_definition_for",
    "assert_valid_action",
    "assert_valid_result",
    "assert_valid_state",
    "load",
    "validate_against",
]


@cache
def load(name: str) -> dict[str, Any]:
    """One vendored schema document, by file stem."""
    document: dict[str, Any] = json.loads((SCHEMA_DIR / f"{name}.schema.json").read_text())
    return document


@cache
def _by_action_type(schema_name: str) -> dict[str, str]:
    """Wire `type` -> definition name, read out of the schema.

    Only definitions whose `type` property is a `const` qualify; that const IS
    the wire discriminant, so the map cannot drift from the document it came
    from.
    """
    definitions = load(schema_name).get("$defs", {})
    index: dict[str, str] = {}
    for name, definition in definitions.items():
        const = (definition.get("properties") or {}).get("type", {}).get("const")
        if isinstance(const, str):
            index[const] = name
    return index


def action_definition_for(action_type: str) -> str | None:
    return _by_action_type("actions").get(action_type)


def validate_against(document: str, definition: str, value: Any) -> list[str]:
    """Validation errors for *value* against `#/$defs/<definition>`.

    The whole `$defs` block rides along so `$ref`s inside the definition
    resolve -- the schemas are one document each, not a bundle of files.
    """
    schema = load(document)
    wrapper = {
        "$schema": schema.get("$schema", "https://json-schema.org/draft/2020-12/schema"),
        "$ref": f"#/$defs/{definition}",
        "$defs": schema["$defs"],
    }
    validator = jsonschema.Draft202012Validator(wrapper)
    return [
        f"{'/'.join(str(p) for p in error.absolute_path) or '<root>'}: {error.message}"
        for error in validator.iter_errors(value)
    ]


def assert_valid_action(action: Any) -> None:
    """Fail unless *action* matches its own declared shape."""
    assert isinstance(action, dict), f"an action must be an object, got {type(action)}"
    action_type = action.get("type")
    assert isinstance(action_type, str), f"an action needs a `type`: {action!r}"
    definition = action_definition_for(action_type)
    assert definition is not None, (
        f"{action_type!r} is not an action the vendored spec declares. Either it "
        f"is misspelled, or the pin is older than the code."
    )
    problems = validate_against("actions", definition, action)
    assert not problems, (
        f"{action_type} does not match {definition}:\n  "
        + "\n  ".join(problems)
        + f"\n\nframe: {json.dumps(action, default=str)[:400]}"
    )


def assert_valid_state(channel_kind: str, state: Any) -> None:
    """Fail unless a channel's published state matches its declared shape."""
    definition = {
        "root": "RootState",
        "session": "SessionState",
        "chat": "ChatState",
        "terminal": "TerminalState",
        "changeset": "ChangesetState",
        "annotations": "AnnotationsState",
    }[channel_kind]
    problems = validate_against("state", definition, state)
    assert not problems, f"{channel_kind} state does not match {definition}:\n  " + "\n  ".join(
        problems
    )


#: Commands whose result definition is not simply `<Method>Result`, or which
#: declare no result body at all. Kept short and explicit: an unknown method
#: is skipped rather than silently passing, so a new command shows up here as
#: a deliberate decision.
_RESULTLESS = frozenset(
    {
        "dispatchAction",
        "unsubscribe",
        "createSession",
        "disposeSession",
        "disposeChat",
        "disposeTerminal",
        "authenticate",
    }
)


def assert_valid_result(method: str, result: Any) -> bool:
    """Validate a command result. Returns whether a definition was found.

    Results are where `CompletionItem` carried an invented `label`/`detail`
    and an attachment keyed `kind` instead of `type` -- neither reachable by
    validating actions alone, because a result is not an action.
    """
    if method in _RESULTLESS:
        return False
    definition = f"{method[0].upper()}{method[1:]}Result"
    if definition not in load("commands").get("$defs", {}):
        return False
    problems = validate_against("commands", definition, result)
    assert not problems, f"{method} result does not match {definition}:\n  " + "\n  ".join(problems)
    return True
