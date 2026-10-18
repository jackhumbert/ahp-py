"""The delegation ``SessionOrigin``: a session started on another session's behalf.

This is an ahp-py extension ahead of the spec (``ahp-host`` ``docs/requests.md``
entry 12). ``SessionOrigin.kind`` is nonexhaustive and clients preserve unknown
kinds verbatim, so a session can carry ``{kind: "ahp-py/delegation", session,
chat, toolCallId}``: the session, chat and tool call, possibly on another host,
on whose behalf it was started. The client that creates the session names it
under ``createSession`` ``_meta[ORIGIN_META_KEY]``; the host seeds it into the
session's immutable ``origin`` (a client's claim, not verified).

Pure and stdlib-only. :func:`parse_delegation` never raises: the value is data
written by a peer.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

__all__ = [
    "DELEGATION_KIND",
    "ORIGIN_META_KEY",
    "delegation_origin",
    "parse_delegation",
]

#: Key in ``createSession`` ``_meta``; the value is one delegation origin.
ORIGIN_META_KEY: Final = "ahp-py/origin"

#: ``SessionOrigin.kind`` of a delegated session.
DELEGATION_KIND: Final = "ahp-py/delegation"


def delegation_origin(session: str, chat: str, tool_call_id: str) -> dict[str, str]:
    """The origin of a session started by *tool_call_id* in *chat* of *session*."""
    for name, value in (("session", session), ("chat", chat), ("tool_call_id", tool_call_id)):
        if not isinstance(value, str) or not value:
            raise ValueError(f"{name} must be a non-empty string")
    return {"kind": DELEGATION_KIND, "session": session, "chat": chat, "toolCallId": tool_call_id}


def parse_delegation(value: object) -> dict[str, str] | None:
    """A fresh delegation origin from *value*, or ``None`` for anything else.

    Other kinds (``automation``, unknown ones) and a missing or empty field give
    ``None``; keys beyond the four are dropped.
    """
    if not isinstance(value, Mapping) or value.get("kind") != DELEGATION_KIND:
        return None
    session, chat, call = value.get("session"), value.get("chat"), value.get("toolCallId")
    if not (isinstance(session, str) and session):
        return None
    if not (isinstance(chat, str) and chat):
        return None
    if not (isinstance(call, str) and call):
        return None
    return {"kind": DELEGATION_KIND, "session": session, "chat": chat, "toolCallId": call}
