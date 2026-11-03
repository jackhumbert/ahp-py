"""Per-chat config values (AHP 1.1.0): what a chat starts with, and what a restore keeps.

Since upstream `c301d494` a chat's config does not inherit from its session:
``ChatConfigState.values`` holds the chat's own value for each ``chatMutable``
property of the effective schema, a key it leaves out takes the property's
schema ``default`` (never the session's value), and "the host sets the initial
`values` when it creates the chat" -- on every chat, the default chat included,
whenever the session has a config. A chat's values then change only through
``chat/configChanged`` on that chat.

So the session's value matters exactly once per chat: when it is created. Seeding
from the session's *current* value is what keeps every new chat behaving as it
did when chats inherited; materialising once on restore does the same for the
chats that already exist. After that a session-level change moves no chat.

Nothing here touches state: these return the ``config`` to put on a chat, and
the host's creation paths all call :func:`seed_chat_config`.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

__all__ = [
    "chat_mutable_properties",
    "effective_schema",
    "materialise_chat_config",
    "seed_chat_config",
]


def effective_schema(chat_state: Any, session_state: Any) -> Mapping[str, Any] | None:
    """The schema governing a chat's config: its own if it has one, else the session's.

    "A chat with its own `schema` does not use the session's config at all."
    """
    own = _config(chat_state)
    if own is not None and isinstance(own.get("schema"), Mapping):
        return own["schema"]  # type: ignore[no-any-return]
    session = _config(session_state)
    schema = session.get("schema") if session is not None else None
    return schema if isinstance(schema, Mapping) else None


def chat_mutable_properties(schema: Mapping[str, Any] | None) -> dict[str, Mapping[str, Any]]:
    """The properties of *schema* a single chat may set (``chatMutable is True``)."""
    properties = schema.get("properties") if schema is not None else None
    if not isinstance(properties, Mapping):
        return {}
    return {
        key: prop
        for key, prop in properties.items()
        if isinstance(key, str) and isinstance(prop, Mapping) and prop.get("chatMutable") is True
    }


def seed_chat_config(session_state: Any, source_chat_state: Any = None) -> dict[str, Any] | None:
    """The ``ChatState.config`` a newly created chat starts with, or ``None``.

    ``None`` when the session has no config: a chat then has none either. A fork
    or side chat copies its source chat's config (values, and the schema if the
    source has one). Any other chat takes, for each ``chatMutable`` property, the
    session's current value, else the property's ``default``; a property with
    neither is left out.
    """
    session = _config(session_state)
    if session is None:
        return None
    source = _config(source_chat_state)
    if source is not None:
        return copy.deepcopy(dict(source))
    return {"values": _initial_values(chat_mutable_properties(session.get("schema")), session)}


def materialise_chat_config(chat_state: Any, session_state: Any) -> dict[str, Any] | None:
    """The config a restored chat is given once, so its behaviour does not change.

    Before this rule a chat's values were an overlay on the session's. For each
    ``chatMutable`` key: the chat's own value if it set one, else the session's
    value, else the default. The result has the same effective value for every
    key the chat could have been using, and never follows the session again.

    A chat with its own ``schema`` never used the session's config, so only its
    own values and defaults apply. ``None`` when the session has no config (and
    the chat has no schema of its own to materialise against).
    """
    own = _config(chat_state)
    if own is not None and isinstance(own.get("schema"), Mapping):
        properties = chat_mutable_properties(own["schema"])
        values = _initial_values(properties, None)
    else:
        session = _config(session_state)
        if session is None:
            return dict(own) if own is not None else None
        properties = chat_mutable_properties(session.get("schema"))
        values = _initial_values(properties, session)
    chat_values = own.get("values") if own is not None else None
    if isinstance(chat_values, Mapping):
        for key in properties:
            if key in chat_values:
                values[key] = copy.deepcopy(chat_values[key])
    return {**(own or {}), "values": values}


def _initial_values(
    properties: Mapping[str, Mapping[str, Any]], session: Mapping[str, Any] | None
) -> dict[str, Any]:
    session_values = session.get("values") if session is not None else None
    values: dict[str, Any] = {}
    for key, prop in properties.items():
        if isinstance(session_values, Mapping) and key in session_values:
            values[key] = copy.deepcopy(session_values[key])
        elif "default" in prop:
            values[key] = copy.deepcopy(prop["default"])
    return values


def _config(state: Any) -> Mapping[str, Any] | None:
    config = state.get("config") if isinstance(state, Mapping) else None
    return config if isinstance(config, Mapping) else None
