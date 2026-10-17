"""Open input requests a restart withdraws, and the notice that names them.

A client was shown each of a session's open input requests (confirmations,
questions, client tools, sign-ins) as a `session/inputNeeded` entry. Nothing in
the next process can answer one: the turn that asked is failed on restore, and a
turn resumed afterwards runs with a fresh sink and raises fresh requests of its
own. So on restore every one is withdrawn -- its entry removed, and a
`systemNotification` naming it added to the interrupted turn -- never left to
hold the session at "input needed" and never dropped without a word.

Pure: no I/O, and nothing here publishes; the host does, with the text and the
`_meta` descriptor below. A client that knows the key can render the notice
specially; one that does not still shows the text.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

__all__ = [
    "RESTART_META_KEY",
    "RestoredInputRequest",
    "request_label",
    "restored_requests",
    "withdrawn_meta",
    "withdrawn_notice",
    "withdrawn_text",
]

#: The `_meta` key a withdrawal notice carries.
RESTART_META_KEY: Final = "ahp-py/restart"

#: `session/inputNeeded` entry kind -> the pending registry's kind.
_ENTRY_KINDS: Final = {
    "toolConfirmation": "confirm",
    "chatInput": "input",
    "toolClientExecution": "clienttool",
    "toolAuthentication": "auth",
}
#: The prefixes the turn sink namespaces a registry key with (see `turn.py`).
_KEY_PREFIXES: Final = {"input": "input:", "auth": "auth:"}


@dataclass(frozen=True)
class RestoredInputRequest:
    """An input request a client was shown before the host stopped.

    Rebuilt from the stored record (`StoredSession.input_requests`) where there
    is one, else from the restored `session/inputNeeded` entry alone.
    """

    #: The host-minted id the client was shown.
    id: str
    #: The pending registry's kind: ``confirm`` | ``input`` | ``clienttool`` |
    #: ``auth``.
    kind: str
    chat_uri: str
    turn_id: str
    #: ``confirm`` / ``clienttool`` / ``auth``: the toolCallId. ``input``: the
    #: provider's own `InputRequest.key`, or ``None``.
    key: str | None = None
    #: The `session/inputNeeded` entry as published.
    entry: Mapping[str, Any] = field(default_factory=dict)


def withdrawn_meta(requests: Sequence[RestoredInputRequest]) -> dict[str, Any]:
    return {RESTART_META_KEY: {"event": "inputWithdrawn", "requests": [r.id for r in requests]}}


def request_label(r: RestoredInputRequest) -> str:
    """What a user would call this request, for the withdrawal notice."""
    raw = r.entry.get("toolCall")
    tool: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}
    name = next(
        (
            v
            for v in (tool.get("displayName"), tool.get("toolName"), r.key, r.id)
            if isinstance(v, str)
        ),
        r.id,
    )
    if r.kind == "confirm":
        return f'approval of "{name}"'
    if r.kind == "clienttool":
        return f'client tool "{name}"'
    if r.kind == "auth":
        return f'sign-in for "{name}"'
    if r.kind == "input":
        request = r.entry.get("request")
        message = request.get("message") if isinstance(request, Mapping) else None
        return f'question "{message}"' if isinstance(message, str) else f"input request {r.id}"
    return f"request {r.id}"


def withdrawn_text(requests: Sequence[RestoredInputRequest]) -> str:
    return (
        "Withdrawn after a host restart, because nothing can answer them any more: "
        + "; ".join(request_label(r) for r in requests)
        + "."
    )


def withdrawn_notice(requests: Sequence[RestoredInputRequest]) -> dict[str, Any]:
    """The `systemNotification` response part that names *requests*."""
    return {
        "kind": "systemNotification",
        "content": withdrawn_text(requests),
        "_meta": withdrawn_meta(requests),
    }


def _provider_key(kind: str, stored: str | None) -> str | None:
    """A stored registry key as the provider named it (the sink's prefix removed)."""
    if stored is None:
        return None
    prefix = _KEY_PREFIXES.get(kind)
    if prefix is not None and stored.startswith(prefix):
        return stored[len(prefix) :]
    return stored


def _entry_requests(input_needed: Any) -> dict[str, Mapping[str, Any]]:
    entries = input_needed if isinstance(input_needed, list) else []
    return {e["id"]: e for e in entries if isinstance(e, Mapping) and isinstance(e.get("id"), str)}


def restored_requests(
    stored: Sequence[Mapping[str, Any]] | None,
    input_needed: Any,
    *,
    turns: Mapping[str, str],
) -> list[RestoredInputRequest]:
    """Every restored `inputNeeded` entry, matched to its stored record where it has one.

    *turns* maps a chat URI to the turn that was active in it, for an entry with
    no record (a store written before records were kept) and no `turnId` of its
    own. A record whose entry is gone was answered before the stop and is
    dropped. Entries with a record come first, in record order.
    """
    entries = _entry_requests(input_needed)
    found: list[RestoredInputRequest] = []
    seen: set[str] = set()
    for record in stored or ():
        if not isinstance(record, Mapping):
            continue
        request_id = record.get("id")
        if not isinstance(request_id, str) or request_id not in entries or request_id in seen:
            continue
        kind = record.get("kind")
        kind = kind if isinstance(kind, str) else "unknown"
        key = record.get("key")
        chat = record.get("chat")
        turn_id = record.get("turnId")
        seen.add(request_id)
        found.append(
            RestoredInputRequest(
                id=request_id,
                kind=kind,
                chat_uri=chat if isinstance(chat, str) else "",
                turn_id=turn_id if isinstance(turn_id, str) else "",
                key=_provider_key(kind, key if isinstance(key, str) else None),
                entry=entries[request_id],
            )
        )
    for request_id, entry in entries.items():
        # `mcp:` entries are the session's MCP servers asking for a credential,
        # not a turn's request: no turn owns them, and the agent reports its
        # servers' state again once it is back.
        if request_id in seen or request_id.startswith("mcp:"):
            continue
        raw_kind = entry.get("kind")
        kind = _ENTRY_KINDS.get(raw_kind, raw_kind) if isinstance(raw_kind, str) else "unknown"
        chat = entry.get("chat")
        chat_uri = chat if isinstance(chat, str) else ""
        turn_id = entry.get("turnId")
        tool = entry.get("toolCall")
        call_id = tool.get("toolCallId") if isinstance(tool, Mapping) else None
        found.append(
            RestoredInputRequest(
                id=request_id,
                kind=kind,
                chat_uri=chat_uri,
                turn_id=turn_id if isinstance(turn_id, str) else turns.get(chat_uri, ""),
                key=call_id if isinstance(call_id, str) else None,
                entry=entry,
            )
        )
    return found
