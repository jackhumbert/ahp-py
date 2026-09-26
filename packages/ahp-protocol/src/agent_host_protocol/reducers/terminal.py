"""Terminal channel reducer.

Ported from ``types/channels-terminal/reducer.ts`` (11 actions). Nine of them are
single-field assignments; the substance is ``terminal/data``, which dispatches
three ways on the tail content part, and ``terminal/commandFinished``, which
completes the part carrying the matching ``commandId``.

Three things about this port are load-bearing:

* **Every assignment here is an unconditional JS spread.** ``{...state, cwd:
  action.cwd}`` on an action with no ``cwd`` leaves the property
  present-but-``undefined``, which ``JSON.stringify`` drops -- so a cwd already
  in the state is *cleared*. On an action carrying an explicit ``null`` it
  writes ``"cwd": null`` through. Those are different documents, and
  :func:`~agent_host_protocol.reducers.js.assign` is what keeps them apart. No
  fixture in the corpus carries a ``null`` for any of these fields, so nothing
  but this comment and the hazard tests defends it.
* **``!tail.isComplete`` is JavaScript truthiness, deliberately.** A command part
  carrying no ``isComplete`` at all counts as still running and keeps collecting
  output, which is what upstream does. It is the documented exception to "never
  bare-truthiness an optional field"; do not "fix" it to ``is False``.
* **``commandId`` matching is ``===``.** An object-valued id therefore matches
  nothing -- two separately-parsed objects are never ``===`` -- and an absent id
  does not match an explicit ``null``. ``terminal/titleChanged``,
  ``terminal/claimed`` and ``terminal/resized`` are client-dispatchable, so
  these payloads are peer-controlled.

``isPty`` is part of ``TerminalState`` in 0.7.0, but no action touches it: it
describes how the resource is backed, is established when the channel's initial
state is built, and never mutates.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from agent_host_protocol.reducers.js import UNDEFINED, assign, get, strict_equal, to_string

__all__ = ["terminal_reducer"]

# ─── Content part discriminants ──────────────────────────────────────────────

_COMMAND = "command"
_UNCLASSIFIED = "unclassified"


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _content(state: Any) -> list[Any]:
    """``[...state.content]`` -- a fresh list, empty for anything that is not an array.

    ``content`` is required by the state type, but a snapshot that lost it would
    make the spread throw upstream; degrading to an empty buffer keeps a single
    malformed snapshot from faulting every action on the channel (invariant 3).
    """
    content = get(state, "content")
    if isinstance(content, Sequence) and not isinstance(content, str | bytes):
        return list(content)
    return []


# ─── Terminal Reducer ────────────────────────────────────────────────────────


def terminal_reducer(state: Any, action: Mapping[str, Any]) -> Any:
    action_type = action.get("type")

    # ── Output ───────────────────────────────────────────────────────────────

    if action_type == "terminal/data":
        content = _content(state)
        # Annotated `Any`, not inferred: the discriminant tests below prove the
        # tail is an object, but only to a reader.
        tail: Any = content[-1] if len(content) > 0 else UNDEFINED
        tail_type = get(tail, "type")
        data = get(action, "data")
        # `!tail.isComplete` is JS truthiness and stays that way: a command part
        # with no `isComplete` at all is still running, so it keeps the output.
        if tail_type == _COMMAND and not get(tail, "isComplete"):
            content[-1] = {**tail, "output": to_string(get(tail, "output")) + to_string(data)}
        elif tail_type == _UNCLASSIFIED:
            content[-1] = {**tail, "value": to_string(get(tail, "value")) + to_string(data)}
        else:
            # A finished command, or an empty buffer: this output belongs to no
            # command, so it starts a new unclassified run.
            content.append(assign({"type": _UNCLASSIFIED}, "value", data))
        return {**state, "content": content}

    if action_type == "terminal/input":
        # Side-effect-only: the host forwards the keystrokes to the pty. Echoing
        # them into `content` here would double them against the `terminal/data`
        # the pty sends back.
        return state

    # ── Metadata ─────────────────────────────────────────────────────────────

    if action_type == "terminal/resized":
        return assign(assign({**state}, "cols", get(action, "cols")), "rows", get(action, "rows"))

    if action_type == "terminal/claimed":
        return assign({**state}, "claim", get(action, "claim"))

    if action_type == "terminal/titleChanged":
        return assign({**state}, "title", get(action, "title"))

    if action_type == "terminal/cwdChanged":
        return assign({**state}, "cwd", get(action, "cwd"))

    if action_type == "terminal/exited":
        # Since 0.9.0 an exit is an explicit lifecycle, so an exit without a
        # code is still an exit. A process killed without one omits
        # `exitCode`, which `JSON.stringify` then drops from the lifecycle
        # object; any top-level `exitCode` a replayed pre-0.9.0 snapshot
        # carries is left as it was, exactly as the spread leaves it upstream.
        return {
            **state,
            "lifecycle": assign({"status": "exited"}, "exitCode", get(action, "exitCode")),
        }

    if action_type == "terminal/cleared":
        return {**state, "content": []}

    # ── Command detection ────────────────────────────────────────────────────

    if action_type == "terminal/commandDetectionAvailable":
        return {**state, "supportsCommandDetection": True}

    if action_type == "terminal/commandExecuted":
        part: dict[str, Any] = {"type": _COMMAND}
        assign(part, "commandId", get(action, "commandId"))
        assign(part, "commandLine", get(action, "commandLine"))
        part["output"] = ""
        assign(part, "timestamp", get(action, "timestamp"))
        part["isComplete"] = False
        return {
            **state,
            "content": [*_content(state), part],
            # A command arriving at all proves shell integration is loaded, so
            # the flag is set here too rather than only on the announcement.
            "supportsCommandDetection": True,
        }

    if action_type == "terminal/commandFinished":
        command_id = get(action, "commandId")
        parts: list[Any] = []
        for existing in _content(state):
            matches = get(existing, "type") == _COMMAND and strict_equal(
                get(existing, "commandId"), command_id
            )
            if not matches:
                parts.append(existing)
                continue
            finished = {**existing, "isComplete": True}
            assign(finished, "exitCode", get(action, "exitCode"))
            assign(finished, "durationMs", get(action, "durationMs"))
            parts.append(finished)
        return {**state, "content": parts}

    # Unknown action: return the state unchanged. Never raise -- a host replaying
    # actions from a newer peer must degrade gracefully (softAssertNever).
    return state
