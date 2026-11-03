"""A chat's effective configuration (upstream `c301d494`).

A chat's config **does not inherit from the session**. Its ``values`` hold the
chat's own values for the ``chatMutable`` properties of the effective schema,
and a missing key takes that property's schema ``default`` -- never the
session's value. Every other property is session-wide and read from the
session's config. A chat that carries its own ``schema`` (it is backed by a
different agent than its session) does not use the session's config at all.

That is why this is a function of two states and not a merge: the older overlay
reading ("a key the chat leaves out falls back to the session's value") gives a
different answer for exactly the keys that matter, such as an approval mode a
user loosened on the session after the chat was created.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ahp_protocol.types import JsonObject

__all__ = ["effective_config"]


def _object(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def effective_config(session_state: Mapping[str, Any], chat_state: Mapping[str, Any]) -> JsonObject:
    """The values in force for a chat, keyed by property id.

    * ``chatMutable`` properties: the chat's ``config.values`` entry, else the
      schema ``default``, else absent. The session's value is never consulted.
    * Other properties: the session's ``config.values`` entry -- or, for a chat
      with its own schema, the schema ``default``, because that chat does not
      use the session's config at all.
    * With no schema anywhere there is nothing to say which keys are
      ``chatMutable``, so the chat's own values are returned as they stand.
    """
    session_config = _object(session_state.get("config"))
    chat_config = _object(chat_state.get("config"))
    own_schema = chat_config.get("schema")
    schema = _object(own_schema if own_schema is not None else session_config.get("schema"))
    chat_values = _object(chat_config.get("values"))
    session_values = _object(session_config.get("values"))
    properties = _object(schema.get("properties"))
    if not properties:
        return dict(chat_values)

    result: JsonObject = {}
    for key, raw in properties.items():
        prop = _object(raw)
        # `chatMutable?: boolean` -- an explicit `is True`, not truthiness.
        if prop.get("chatMutable") is True:
            if key in chat_values:
                result[key] = chat_values[key]
            elif "default" in prop:
                result[key] = prop["default"]
        elif own_schema is None:
            if key in session_values:
                result[key] = session_values[key]
        elif "default" in prop:
            result[key] = prop["default"]
    return result
