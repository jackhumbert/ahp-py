"""Session channel reducer.

Ported from ``types/channels-session/reducer.ts`` (28 actions). Unlike the chat
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
  through :func:`session_status_flags` so we agree with Go/Rust/Kotlin/Swift;
* **the action is read with ``.get`` / :func:`_obj`, never indexed.** A missing
  property is ``undefined`` upstream, not a throw: ``session/titleChanged`` with
  no ``title`` sets ``title: undefined`` (reducer.ts:165) and returns a state.
  Nine of these actions are client-dispatchable, so ``action["title"]`` would
  hand any untrusted peer a ``KeyError`` out of the reducer.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from ahp_protocol.reducers.js import (
    UNDEFINED,
    assign,
    get,
    index_of,
    index_of_value,
    strict_equal,
    truthy,
)
from ahp_protocol.types.protocol import SessionStatus, session_status_flags
from ahp_protocol.types.wire import coalesce

__all__ = ["session_reducer"]

# ─── Status bits ─────────────────────────────────────────────────────────────
#
# `SessionStatus` is a bitset, not an enum. Note `INPUT_NEEDED` is a combination
# -- `InProgress | (1 << 4)` -- so a session awaiting input is still in
# progress, and the two cannot be compared by equality.

_IN_PROGRESS = SessionStatus.IN_PROGRESS
_INPUT_NEEDED = SessionStatus.INPUT_NEEDED
_IS_READ = SessionStatus.IS_READ
_IS_ARCHIVED = SessionStatus.IS_ARCHIVED
#: Bitmask covering the mutually-exclusive activity bits (bits 0-4).
_STATUS_ACTIVITY_MASK = SessionStatus.ACTIVITY_MASK

_MCP_SERVER = "mcpServer"
_PLUGIN = "plugin"
_TOOL_CLIENT_EXECUTION = "toolClientExecution"

#: Returned by :func:`_apply_customization_enablement` where upstream throws.
_THROWS: Any = object()


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _obj(value: Any) -> Mapping[str, Any]:
    """A wire object read the way JavaScript reads one.

    Covers both shapes upstream relies on and Python does not give for free:
    ``x.y`` on a non-object is ``undefined`` rather than an ``AttributeError``,
    and ``{...x}`` on a non-object is ``{}`` rather than a ``TypeError``. Used
    for every nested read of an *action*, whose payload comes from a peer that
    may be newer than us or simply wrong (ADR 0001).
    """
    return value if isinstance(value, Mapping) else {}


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


def _awaits_user(request: Any) -> bool:
    """``request.kind !== SessionInputRequestKind.ToolClientExecution``.

    A client-execution entry is work delegated to a client, not a prompt: the
    call has already cleared its confirmation gate. Since 0.8.0 it does not
    count toward `InputNeeded`. A non-object entry reads `kind` as `undefined`
    (a `null` one would throw upstream) and so counts, as any unknown kind does.
    """
    return not strict_equal(get(request, "kind"), _TOOL_CLIENT_EXECUTION)


def _with_input_needed_status(status: int, input_needed: Sequence[Any]) -> int:
    """Reflect the session-level input queue into the activity bits of `status`.

    A queue holding any user-blocking entry (:func:`_awaits_user`) promotes the
    activity to `InputNeeded`; draining those entries clears only the
    input-needed-specific bit (1 << 4), so an unblocked turn falls back to
    `InProgress` and an already-idle session stays idle. The orthogonal
    `IsRead` / `IsArchived` flags survive either way.
    """
    if any(_awaits_user(request) for request in input_needed):
        return session_status_flags((status & ~_STATUS_ACTIVITY_MASK) | _INPUT_NEEDED)
    return session_status_flags(status & ~(_INPUT_NEEDED & ~_IN_PROGRESS))


def _js_length_positive(value: Any) -> bool:
    """``value.length > 0`` for a value that is not an array or string.

    Only a mapping can carry a `length` at all; `undefined > 0` is false. JS
    relational coercion of a non-numeric `length` is approximated by parsing a
    string as a number and treating everything else as NaN.
    """
    length = get(value, "length")
    if isinstance(length, bool | int | float):
        return length > 0
    if isinstance(length, str):
        try:
            return float(length) > 0
        except ValueError:
            return False
    return False


def _apply_customization_enablement(customization: Any, enablement: Any) -> Any:
    """Port of `applyCustomizationEnablement` (0.8.0).

    Plugins and MCP servers take the decision list verbatim -- an empty one
    deletes the key -- while every other customization keeps the legacy
    `enabled` flag, derived as ``enablement[0]?.enabled ?? true``.

    Upstream reads ``enablement.length`` / ``enablement[0]`` unguarded, so an
    absent or null list throws; so does spreading a non-iterable. Those return
    :data:`_THROWS` and the caller degrades to the branch's no-op.
    """
    base = {**customization} if isinstance(customization, Mapping) else {}
    if enablement is UNDEFINED or enablement is None:
        return _THROWS
    kind = get(customization, "type")
    if strict_equal(kind, _PLUGIN) or strict_equal(kind, _MCP_SERVER):
        if isinstance(enablement, list | tuple | str):
            if len(enablement) > 0:
                # `[...enablement]` -- a string spreads into its characters.
                return {**base, "enablement": list(enablement)}
        elif _js_length_positive(enablement):
            return _THROWS
        base.pop("enablement", None)
        return base
    if isinstance(enablement, list | tuple):
        first = enablement[0] if len(enablement) > 0 else UNDEFINED
    elif isinstance(enablement, Mapping):
        first = get(enablement, "0")
    else:
        # A string's first element is a character, and a number or boolean has
        # no index; either way `.enabled` is `undefined`.
        first = UNDEFINED
    enabled = get(first, "enabled")
    return {**base, "enabled": True if enabled is UNDEFINED or enabled is None else enabled}


def _without(items: Sequence[Any], index: int) -> list[Any]:
    """``const copy = items.slice(); copy.splice(index, 1)``."""
    copy = list(items)
    del copy[index]
    return copy


def _update_mcp_server(
    state: Mapping[str, Any],
    target_id: Any,
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

    top_index = index_of(customizations, "id", target_id)
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
        child_index = index_of(children, "id", target_id)
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
        # `creationError: action.error` -- an omitted error is `undefined`
        # upstream, so the key stays ABSENT. `action.get` would hand `assign`
        # a `None` it writes through as `"creationError": null`.
        # The lifecycle value is `failed` since 0.9.0 (was `creationFailed`).
        return assign({**state, "lifecycle": "failed"}, "creationError", get(action, "error"))

    # ── Chat catalog ─────────────────────────────────────────────────────────

    if action_type == "session/chatAdded":
        summary = action.get("summary")
        chats = coalesce(state.get("chats"), [])
        index = index_of(chats, "resource", get(summary, "resource"))
        if index < 0:
            return {**state, "chats": [*chats, summary]}
        updated = list(chats)
        updated[index] = summary
        return {**state, "chats": updated}

    if action_type == "session/chatRemoved":
        # `c.resource === action.chat`: an absent `chat` is `undefined`, which
        # matches an entry whose own `resource` is absent and never matches an
        # explicit null -- `action.get` (None for both) inverts that pairing.
        chat = get(action, "chat")
        chats = coalesce(state.get("chats"), [])
        index = index_of(chats, "resource", chat)
        if index < 0:
            return state
        next_state = {**state, "chats": _without(chats, index)}
        # `state.defaultChat === action.chat` -- strict: a null defaultChat must
        # survive an absent `chat`, and an object-valued one compares by
        # reference, not structure.
        if strict_equal(get(state, "defaultChat"), chat):
            # Upstream `delete next.defaultChat` -- the routing hint cannot point
            # at a chat that no longer exists.
            next_state.pop("defaultChat", None)
        return next_state

    if action_type == "session/chatUpdated":
        chats = coalesce(state.get("chats"), [])
        index = index_of(chats, "resource", get(action, "chat"))
        if index < 0:
            return state
        # Upstream destructures `resource` out of `changes`: identity fields are
        # ignored even when a sender wrongly carries them.
        changes = {k: v for k, v in _obj(get(action, "changes")).items() if k != "resource"}
        updated = list(chats)
        updated[index] = {**chats[index], **changes}
        return {**state, "chats": updated}

    if action_type == "session/defaultChatChanged":
        return assign({**state}, "defaultChat", get(action, "defaultChat"))

    # ── Metadata ─────────────────────────────────────────────────────────────

    if action_type == "session/titleChanged":
        # `title: action.title` -- an omitted title is `undefined` upstream, so
        # it clears the title and returns a state. Client-dispatchable.
        return assign({**state}, "title", get(action, "title"))

    if action_type == "session/isReadChanged":
        # `isRead` is a declared boolean; plain truthiness matches JS here.
        return {
            **state,
            "status": _with_status_flag(_status_of(state), _IS_READ, bool(get(action, "isRead"))),
        }

    if action_type == "session/isArchivedChanged":
        return {
            **state,
            "status": _with_status_flag(
                _status_of(state), _IS_ARCHIVED, bool(get(action, "isArchived"))
            ),
        }

    if action_type == "session/activityChanged":
        return assign({**state}, "activity", get(action, "activity"))

    if action_type == "session/changesetsChanged":
        # Upstream destructures the key out and re-adds it only when
        # `action.changesets` is truthy, so an explicit null CLEARS the key
        # (fixture 146 pins that: `"changesets": null` in, no key out) --
        # `assign` would write the null through. JS truthiness, not Python's:
        # an empty array is truthy there and sets an empty catalogue.
        changesets = get(action, "changesets")
        next_state = {**state}
        next_state.pop("changesets", None)
        if truthy(changesets):
            next_state["changesets"] = changesets
        return next_state

    if action_type == "session/configChanged":
        config = state.get("config")
        # A config change against a session that publishes no config schema is
        # dropped, not created (fixture 114).
        if config is None:
            return state
        # `{...action.config}` -- spreading an absent config is `{}` upstream,
        # not a throw, so a payload-less change is an empty replace or a no-op
        # merge. Client-dispatchable.
        patch = _obj(get(action, "config"))
        values = (
            {**patch} if action.get("replace") else {**coalesce(config.get("values"), {}), **patch}
        )
        return {**state, "config": {**config, "values": values}}

    if action_type == "session/metaChanged":
        return assign({**state}, "_meta", get(action, "_meta"))

    if action_type == "session/serverToolsChanged":
        return assign({**state}, "serverTools", get(action, "tools"))

    # ── Active clients ───────────────────────────────────────────────────────

    if action_type == "session/activeClientSet":
        active_client = action.get("activeClient")
        clients = coalesce(state.get("activeClients"), [])
        # `action.activeClient.clientId` -- a property read off a missing or
        # non-object payload is `undefined` in JS, never a throw. The lookup then
        # misses and the payload is appended verbatim. Client-dispatchable.
        index = index_of(clients, "clientId", get(active_client, "clientId"))
        if index < 0:
            return {**state, "activeClients": [*clients, active_client]}
        updated = list(clients)
        updated[index] = active_client
        return {**state, "activeClients": updated}

    if action_type == "session/activeClientRemoved":
        clients = coalesce(state.get("activeClients"), [])
        index = index_of(clients, "clientId", get(action, "clientId"))
        if index < 0:
            return state
        return {**state, "activeClients": _without(clients, index)}

    # ── Working directories ──────────────────────────────────────────────────

    if action_type == "session/workingDirectorySet":
        # `list.indexOf(action.directory)` -- strict: an absent `directory` is
        # `undefined`, which matches nothing (a parsed array cannot hold one),
        # so upstream appends even when the list already holds an explicit null.
        directory = get(action, "directory")
        directories = coalesce(state.get("workingDirectories"), [])
        if index_of_value(directories, directory) >= 0:
            return state
        # `JSON.stringify` writes an `undefined` ARRAY ELEMENT as `null` --
        # unlike an object member, which it drops -- so the appended image of an
        # absent directory is null.
        appended = None if directory is UNDEFINED else directory
        return {**state, "workingDirectories": [*directories, appended]}

    if action_type == "session/workingDirectoryRemoved":
        directories = state.get("workingDirectories")
        if directories is None:
            return state
        index = index_of_value(directories, get(action, "directory"))
        if index < 0:
            return state
        return {**state, "workingDirectories": _without(directories, index)}

    if action_type == "session/workingDirectoryReplaced":
        # A compare-and-swap: a no-op unless `directory` is present. The result
        # is deduplicated against `replacement`, which keeps its own position
        # when it already sits earlier (`[A, B, C]` with `C -> A` is `[A, B]`)
        # and otherwise lands in the target's slot (`B -> C` is `[A, C]`).
        directories = state.get("workingDirectories")
        if directories is None:
            return state
        index = index_of_value(directories, get(action, "directory"))
        if index < 0:
            return state
        replacement = get(action, "replacement")
        replacement_index = index_of_value(directories, replacement)
        if 0 <= replacement_index < index:
            return {**state, "workingDirectories": _without(directories, index)}
        # An absent replacement is an `undefined` array element, which
        # `JSON.stringify` writes as null -- and which `!==` no parsed entry.
        image = None if replacement is UNDEFINED else replacement
        return {
            **state,
            "workingDirectories": [
                image if position == index else directory
                for position, directory in enumerate(directories)
                if position == index or not strict_equal(directory, replacement)
            ],
        }

    # ── Input needed ─────────────────────────────────────────────────────────

    if action_type == "session/inputNeededSet":
        request = action.get("request")
        requests = coalesce(state.get("inputNeeded"), [])
        index = index_of(requests, "id", get(request, "id"))
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
        index = index_of(requests, "id", get(action, "id"))
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
        return assign({**state}, "customizations", get(action, "customizations"))

    if action_type == "session/customizationToggled":
        customizations = state.get("customizations")
        if customizations is None:
            return state
        # `c.id === action.id`: an absent id is `undefined`, which matches an
        # entry with no `id` key and never one carrying an explicit null.
        target_id = get(action, "id")
        # Since 0.8.0 the action carries the complete `enablement` decision
        # list, which replaces the previous one outright. Client-dispatchable,
        # so a malformed list degrades to a no-op instead of raising.
        enablement = get(action, "enablement")
        top_index = index_of(customizations, "id", target_id)
        if top_index >= 0:
            entry = _apply_customization_enablement(customizations[top_index], enablement)
            if entry is _THROWS:
                return state
            updated = list(customizations)
            updated[top_index] = entry
            return {**state, "customizations": updated}
        for index, container in enumerate(customizations):
            if not isinstance(container, Mapping) or container.get("type") == _MCP_SERVER:
                continue
            children = container.get("children")
            if children is None:
                continue
            child_index = index_of(children, "id", target_id)
            if child_index < 0:
                continue
            child = _apply_customization_enablement(children[child_index], enablement)
            if child is _THROWS:
                return state
            new_children = list(children)
            new_children[child_index] = child
            updated = list(customizations)
            updated[index] = {**container, "children": new_children}
            return {**state, "customizations": updated}
        return state

    if action_type == "session/customizationUpdated":
        customization = action.get("customization")
        customizations = coalesce(state.get("customizations"), [])
        index = index_of(customizations, "id", get(customization, "id"))
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
        # `c.id === action.id` again: absent is `undefined`, not None.
        target_id = get(action, "id")
        top_index = index_of(customizations, "id", target_id)
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
            child_index = index_of(children, "id", target_id)
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
        # The actions doc calls `channel` "full-replacement: omit to clear an
        # existing channel (typical when leaving Ready)", so the omit path runs
        # on every well-formed shutdown. `action.get` would turn it into
        # `"channel": null` -- schema-invalid, and `!== undefined` in a
        # reference client -- where `get` lets `assign` drop the key.
        state_value = get(action, "state")
        channel = get(action, "channel")
        return _update_mcp_server(
            state,
            get(action, "id"),
            # Full replacement of both runtime fields: an omitted `channel` --
            # or `state` -- is written as `undefined` upstream, so it clears an
            # existing one rather than leaving it in place.
            lambda entry: assign(assign({**entry}, "state", state_value), "channel", channel),
        )

    if action_type == "session/mcpServerStartRequested":
        # A missing `id` matches nothing and no-ops. Client-dispatchable.
        return _update_mcp_server(
            state,
            get(action, "id"),
            lambda entry: assign({**entry, "state": {"kind": "starting"}}, "channel", UNDEFINED),
        )

    if action_type == "session/mcpServerStopRequested":
        return _update_mcp_server(
            state,
            get(action, "id"),
            lambda entry: assign({**entry, "state": {"kind": "stopped"}}, "channel", UNDEFINED),
        )

    # Unknown action: return the state unchanged. Never raise -- a host replaying
    # actions from a newer peer must degrade gracefully (softAssertNever).
    return state
