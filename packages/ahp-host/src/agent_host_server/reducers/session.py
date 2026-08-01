"""Session channel reducer.

Ported from ``types/channels-session/reducer.ts`` (27 actions). Unlike the chat
reducer this one is **time-free** -- it never reads the clock. It touches
``status`` in exactly three places: the two orthogonal metadata flags
(``IsRead`` / ``IsArchived``) and the ``inputNeeded`` promotion, which reflects
the session-level input queue into the activity bits.

Two shapes recur and are worth naming up front:

* the customization tree is two levels deep -- top-level entries are containers
  (``plugin`` / ``directory``) or a bare ``mcpServer``, and containers hold
  leaves in ``children``. Every id lookup searches the top level first and only
  then the children, and a hit at the top level never falls through to the
  children search;
* ``status`` is a bitset, and JavaScript coerces bitwise operands to *signed*
  int32 while every other client port uses unsigned. Each bitwise result goes
  through :func:`session_status_flags` so we agree with Go/Rust/Kotlin/Swift.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from agent_host_server.types.protocol import session_status_flags
from agent_host_server.types.wire import coalesce

__all__ = ["session_reducer"]

# ─── Status bits ─────────────────────────────────────────────────────────────
#
# Transcribed from `SessionStatus` in `channels-session/state.ts`. Deliberately
# NOT bound to `types.protocol.SessionStatus`: its member names are rotated by
# one position against upstream (it calls 1 `IN_PROGRESS`, 8 `ERROR`, 24
# `IS_READ`, 32 `IS_ARCHIVED`, 64 `INPUT_NEEDED`, where upstream has 1 = Idle,
# 2 = Error, 8 = InProgress, 24 = InputNeeded, 32 = IsRead, 64 = IsArchived).
# The numbers below are the ones the reducer arithmetic depends on; only
# `session_status_flags`, which is name-free, is imported from there.

#: `SessionStatus.InProgress` -- 1 << 3.
_IN_PROGRESS = 1 << 3
#: `SessionStatus.InputNeeded` -- (1 << 3) | (1 << 4). Implies `InProgress`.
_INPUT_NEEDED = (1 << 3) | (1 << 4)
#: `SessionStatus.IsRead` -- 1 << 5.
_IS_READ = 1 << 5
#: `SessionStatus.IsArchived` -- 1 << 6.
_IS_ARCHIVED = 1 << 6
#: Bitmask covering the mutually-exclusive activity bits (bits 0-4).
_STATUS_ACTIVITY_MASK = (1 << 5) - 1

_MCP_SERVER = "mcpServer"


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _status_of(state: Mapping[str, Any]) -> int:
    """``state.status`` as an integer.

    `status` is a required field, but a missing one must not raise: JavaScript
    coerces `undefined` to 0 inside a bitwise expression, so 0 is the faithful
    fallback rather than a guess.
    """
    status = coalesce(state.get("status"), 0)
    # A non-numeric status would throw in JS too; 0 keeps a malformed snapshot
    # from taking the whole channel down. `bool` is deliberately allowed
    # through: `True | 32 == 33` in both languages.
    return status if isinstance(status, int) else 0


def _with_status_flag(status: int, flag: int, on: bool) -> int:
    """``set ? status | flag : status & ~flag``, normalised to unsigned 32-bit."""
    return session_status_flags(status | flag if on else status & ~flag)


def _with_input_needed_status(status: int, input_needed: Sequence[Any]) -> int:
    """Reflect the session-level input queue into the activity bits of `status`.

    A non-empty queue promotes the activity to `InputNeeded`; emptying it clears
    only the input-needed-specific bit (1 << 4), so an unblocked turn falls back
    to `InProgress` and an already-idle session stays idle. The orthogonal
    `IsRead` / `IsArchived` flags survive either way.
    """
    if len(input_needed) > 0:
        return session_status_flags((status & ~_STATUS_ACTIVITY_MASK) | _INPUT_NEEDED)
    return session_status_flags(status & ~(_INPUT_NEEDED & ~_IN_PROGRESS))


def _index_of(items: Sequence[Any], key: str, value: Any) -> int:
    """``items.findIndex(i => i[key] === value)``; -1 when absent."""
    for index, item in enumerate(items):
        if isinstance(item, Mapping) and item.get(key) == value:
            return index
    return -1


def _index_of_value(items: Sequence[Any], value: Any) -> int:
    """``items.indexOf(value)``; -1 when absent.

    Only ever used for the working-directory URI set, so ``==`` is safe here --
    it is string-to-string. It is written out rather than using ``in`` so the
    membership test and the removal share one scan and one equality rule.
    """
    for index, item in enumerate(items):
        if item == value:
            return index
    return -1


def _without(items: Sequence[Any], index: int) -> list[Any]:
    """``const copy = items.slice(); copy.splice(index, 1)``."""
    copy = list(items)
    del copy[index]
    return copy


def _with_optional(target: dict[str, Any], key: str, value: Any) -> dict[str, Any]:
    """Assign *key*, dropping it entirely when the value is a JS `undefined`.

    Upstream writes `{ ...entry, channel: action.channel }`, which leaves the key
    present-but-undefined. Python's equivalent of an undefined property is an
    absent key, so the fixtures' `"channel": null` normalises onto this.
    Mutates *target*, which is always a freshly-built copy at every call site.
    """
    if value is None:
        target.pop(key, None)
    else:
        target[key] = value
    return target


def _update_mcp_server(
    state: Mapping[str, Any],
    target_id: str,
    update: Callable[[Mapping[str, Any]], dict[str, Any]],
) -> Any:
    """Port of `updateMcpServerCustomization`.

    Searches the top level first, then every container's `children`. A top-level
    id hit that is *not* an `mcpServer` is a hard no-op -- it does not fall
    through to the children search (fixture 162 pins this).
    """
    customizations = state.get("customizations")
    # Upstream `if (!list)`: an empty array is truthy in JS and falls through to
    # a findIndex that returns -1, i.e. the same no-op. `is None` is the faithful
    # translation and behaves identically for `[]`.
    if customizations is None:
        return state

    top_index = _index_of(customizations, "id", target_id)
    if top_index >= 0:
        entry = customizations[top_index]
        if entry.get("type") != _MCP_SERVER:
            return state
        updated = list(customizations)
        updated[top_index] = update(entry)
        return {**state, "customizations": updated}

    changed = False
    containers: list[Any] = []
    for container in customizations:
        if not isinstance(container, Mapping) or container.get("type") == _MCP_SERVER:
            containers.append(container)
            continue
        # `if (!children)` upstream: an unparsed container has no `children` at
        # all, and an empty array behaves the same way via a -1 findIndex.
        children = container.get("children")
        if children is None:
            containers.append(container)
            continue
        child_index = _index_of(children, "id", target_id)
        if child_index < 0:
            containers.append(container)
            continue
        child = children[child_index]
        if child.get("type") != _MCP_SERVER:
            containers.append(container)
            continue
        changed = True
        new_children = list(children)
        new_children[child_index] = update(child)
        containers.append({**container, "children": new_children})

    if not changed:
        return state
    return {**state, "customizations": containers}


# ─── Session Reducer ─────────────────────────────────────────────────────────


def session_reducer(state: Any, action: Mapping[str, Any]) -> Any:
    action_type = action.get("type")

    # ── Lifecycle ────────────────────────────────────────────────────────────

    if action_type == "session/ready":
        # Purely a lifecycle transition. It must NOT touch `status`: a
        # provisional session's first turn can start before materialization
        # completes, so an in-progress status may already be set when this
        # arrives (fixture 147).
        return {**state, "lifecycle": "ready"}

    if action_type == "session/creationFailed":
        return {**state, "lifecycle": "creationFailed", "creationError": action["error"]}

    # ── Chat catalog ─────────────────────────────────────────────────────────

    if action_type == "session/chatAdded":
        summary = action["summary"]
        chats = coalesce(state.get("chats"), [])
        index = _index_of(chats, "resource", summary.get("resource"))
        if index < 0:
            return {**state, "chats": [*chats, summary]}
        updated = list(chats)
        updated[index] = summary
        return {**state, "chats": updated}

    if action_type == "session/chatRemoved":
        chats = coalesce(state.get("chats"), [])
        index = _index_of(chats, "resource", action["chat"])
        if index < 0:
            return state
        next_state = {**state, "chats": _without(chats, index)}
        if state.get("defaultChat") == action["chat"]:
            # Upstream `delete next.defaultChat` -- the routing hint cannot point
            # at a chat that no longer exists.
            next_state.pop("defaultChat", None)
        return next_state

    if action_type == "session/chatUpdated":
        chats = coalesce(state.get("chats"), [])
        index = _index_of(chats, "resource", action["chat"])
        if index < 0:
            return state
        # Upstream destructures `resource` out of `changes`: identity fields are
        # ignored even when a sender wrongly carries them.
        changes = {k: v for k, v in action["changes"].items() if k != "resource"}
        updated = list(chats)
        updated[index] = {**chats[index], **changes}
        return {**state, "chats": updated}

    if action_type == "session/defaultChatChanged":
        return _with_optional({**state}, "defaultChat", action.get("defaultChat"))

    # ── Metadata ─────────────────────────────────────────────────────────────

    if action_type == "session/titleChanged":
        return {**state, "title": action["title"]}

    if action_type == "session/isReadChanged":
        # `isRead` is a declared boolean; plain truthiness matches JS here.
        return {
            **state,
            "status": _with_status_flag(_status_of(state), _IS_READ, bool(action.get("isRead"))),
        }

    if action_type == "session/isArchivedChanged":
        return {
            **state,
            "status": _with_status_flag(
                _status_of(state), _IS_ARCHIVED, bool(action.get("isArchived"))
            ),
        }

    if action_type == "session/activityChanged":
        return _with_optional({**state}, "activity", action.get("activity"))

    if action_type == "session/changesetsChanged":
        # Upstream `action.changesets ? ... : ...`. An empty array is TRUTHY in
        # JS, so an explicit `[]` sets an empty catalogue rather than clearing
        # it; only null/undefined clears. `is not None`, never truthiness.
        return _with_optional({**state}, "changesets", action.get("changesets"))

    if action_type == "session/configChanged":
        config = state.get("config")
        # A config change against a session that publishes no config schema is
        # dropped, not created (fixture 114).
        if config is None:
            return state
        values = (
            {**action["config"]}
            if action.get("replace")
            else {**coalesce(config.get("values"), {}), **action["config"]}
        )
        return {**state, "config": {**config, "values": values}}

    if action_type == "session/metaChanged":
        return _with_optional({**state}, "_meta", action.get("_meta"))

    if action_type == "session/serverToolsChanged":
        return {**state, "serverTools": action["tools"]}

    # ── Active clients ───────────────────────────────────────────────────────

    if action_type == "session/activeClientSet":
        active_client = action["activeClient"]
        clients = coalesce(state.get("activeClients"), [])
        index = _index_of(clients, "clientId", active_client.get("clientId"))
        if index < 0:
            return {**state, "activeClients": [*clients, active_client]}
        updated = list(clients)
        updated[index] = active_client
        return {**state, "activeClients": updated}

    if action_type == "session/activeClientRemoved":
        clients = coalesce(state.get("activeClients"), [])
        index = _index_of(clients, "clientId", action["clientId"])
        if index < 0:
            return state
        return {**state, "activeClients": _without(clients, index)}

    # ── Working directories ──────────────────────────────────────────────────

    if action_type == "session/workingDirectorySet":
        # Membership, not truthiness: an existing empty set still appends.
        directories = coalesce(state.get("workingDirectories"), [])
        if _index_of_value(directories, action["directory"]) >= 0:
            return state
        return {**state, "workingDirectories": [*directories, action["directory"]]}

    if action_type == "session/workingDirectoryRemoved":
        directories = state.get("workingDirectories")
        if directories is None:
            return state
        index = _index_of_value(directories, action["directory"])
        if index < 0:
            return state
        return {**state, "workingDirectories": _without(directories, index)}

    # ── Input needed ─────────────────────────────────────────────────────────

    if action_type == "session/inputNeededSet":
        request = action["request"]
        requests = coalesce(state.get("inputNeeded"), [])
        index = _index_of(requests, "id", request.get("id"))
        if index < 0:
            input_needed = [*requests, request]
        else:
            input_needed = list(requests)
            input_needed[index] = request
        return {
            **state,
            "inputNeeded": input_needed,
            "status": _with_input_needed_status(_status_of(state), input_needed),
        }

    if action_type == "session/inputNeededRemoved":
        requests = state.get("inputNeeded")
        if requests is None:
            return state
        index = _index_of(requests, "id", action["id"])
        if index < 0:
            return state
        remaining = _without(requests, index)
        next_state = {
            **state,
            "status": _with_input_needed_status(_status_of(state), remaining),
        }
        # Upstream keeps the key only while the queue is non-empty and otherwise
        # `delete`s it, so an emptied queue is absent rather than `[]`.
        if len(remaining) > 0:
            next_state["inputNeeded"] = remaining
        else:
            next_state.pop("inputNeeded", None)
        return next_state

    # ── Customizations ───────────────────────────────────────────────────────

    if action_type == "session/customizationsChanged":
        return {**state, "customizations": action["customizations"]}

    if action_type == "session/customizationToggled":
        customizations = state.get("customizations")
        if customizations is None:
            return state
        top_index = _index_of(customizations, "id", action["id"])
        if top_index >= 0:
            updated = list(customizations)
            updated[top_index] = {**customizations[top_index], "enabled": action["enabled"]}
            return {**state, "customizations": updated}
        for index, container in enumerate(customizations):
            if not isinstance(container, Mapping) or container.get("type") == _MCP_SERVER:
                continue
            children = container.get("children")
            if children is None:
                continue
            child_index = _index_of(children, "id", action["id"])
            if child_index < 0:
                continue
            new_children = list(children)
            new_children[child_index] = {**children[child_index], "enabled": action["enabled"]}
            updated = list(customizations)
            updated[index] = {**container, "children": new_children}
            return {**state, "customizations": updated}
        return state

    if action_type == "session/customizationUpdated":
        customization = action["customization"]
        customizations = coalesce(state.get("customizations"), [])
        index = _index_of(customizations, "id", customization.get("id"))
        if index < 0:
            return {**state, "customizations": [*customizations, customization]}
        # Full replacement, `children` included: a host that wants to keep the
        # existing children must carry them on the payload.
        updated = list(customizations)
        updated[index] = customization
        return {**state, "customizations": updated}

    if action_type == "session/customizationRemoved":
        customizations = state.get("customizations")
        if customizations is None:
            return state
        top_index = _index_of(customizations, "id", action["id"])
        if top_index >= 0:
            # Removing a container removes its children with it.
            return {**state, "customizations": _without(customizations, top_index)}
        changed = False
        containers: list[Any] = []
        for container in customizations:
            if not isinstance(container, Mapping) or container.get("type") == _MCP_SERVER:
                containers.append(container)
                continue
            children = container.get("children")
            if children is None:
                containers.append(container)
                continue
            child_index = _index_of(children, "id", action["id"])
            if child_index < 0:
                containers.append(container)
                continue
            changed = True
            containers.append({**container, "children": _without(children, child_index)})
        if not changed:
            return state
        return {**state, "customizations": containers}

    # ── MCP servers ──────────────────────────────────────────────────────────

    if action_type == "session/mcpServerStateChanged":
        state_value = action["state"]
        channel = action.get("channel")
        return _update_mcp_server(
            state,
            action["id"],
            # Full replacement of both runtime fields: an omitted `channel`
            # clears an existing one.
            lambda entry: _with_optional({**entry, "state": state_value}, "channel", channel),
        )

    if action_type == "session/mcpServerStartRequested":
        return _update_mcp_server(
            state,
            action["id"],
            lambda entry: _with_optional({**entry, "state": {"kind": "starting"}}, "channel", None),
        )

    if action_type == "session/mcpServerStopRequested":
        return _update_mcp_server(
            state,
            action["id"],
            lambda entry: _with_optional({**entry, "state": {"kind": "stopped"}}, "channel", None),
        )

    # Unknown action: return the state unchanged. Never raise -- a host replaying
    # actions from a newer peer must degrade gracefully (softAssertNever).
    return state
