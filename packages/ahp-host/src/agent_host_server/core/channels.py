"""Channel URIs and reducer routing.

Every channel is a URI-identified subscribable resource, and every command and
notification carries its URI as ``params.channel`` -- the routing key the whole
protocol is built on.

Two rules that are easy to get wrong:

* The root URI is matched by **exact string equality**. The reference client
  compares against the literal ``'ahp-root://'`` with ``===`` in three separate
  places: no normalisation, no trailing-slash tolerance, no scheme aliasing.
* Session and chat URIs are matched by **scheme prefix only**, and the rest is
  opaque. A host must never parse or validate the remainder: the client chooses
  it, and real clients use forms other than a bare UUID -- ``ahpx`` sends
  ``<provider>:/<uuid>``.
"""

from __future__ import annotations

from enum import Enum
from typing import Final

__all__ = ["ROOT_URI", "ChannelKind", "chat_uri", "classify", "reducer_name_for", "session_uri"]

ROOT_URI: Final = "ahp-root://"

_SESSION_PREFIX: Final = "ahp-session:"
_CHAT_PREFIX: Final = "ahp-chat:"
_TERMINAL_PREFIX: Final = "ahp-terminal:"
_CHANGESET_PREFIX: Final = "ahp-changeset:"
_RESOURCE_WATCH_PREFIX: Final = "ahp-resource-watch:"
_OTLP_PREFIX: Final = "ahp-otlp:"


class ChannelKind(Enum):
    ROOT = "root"
    SESSION = "session"
    CHAT = "chat"
    #: Recognised but not implemented in v0.1 -- see docs/plan.md §1.
    TERMINAL = "terminal"
    CHANGESET = "changeset"
    RESOURCE_WATCH = "resourceWatch"
    OTLP = "otlp"
    #: A scheme we do not know. Clients MUST NOT subscribe to one, but a host
    #: must still answer rather than crash.
    UNKNOWN = "unknown"


def classify(uri: str) -> ChannelKind:
    if uri == ROOT_URI:
        return ChannelKind.ROOT
    if uri.startswith(_SESSION_PREFIX):
        return ChannelKind.SESSION
    if uri.startswith(_CHAT_PREFIX):
        return ChannelKind.CHAT
    if uri.startswith(_TERMINAL_PREFIX):
        return ChannelKind.TERMINAL
    if uri.startswith(_CHANGESET_PREFIX):
        return ChannelKind.CHANGESET
    if uri.startswith(_RESOURCE_WATCH_PREFIX):
        return ChannelKind.RESOURCE_WATCH
    if uri.startswith(_OTLP_PREFIX):
        return ChannelKind.OTLP
    return ChannelKind.UNKNOWN


#: Channel kind -> the key in `agent_host_server.reducers.REDUCERS`.
_REDUCERS: Final[dict[ChannelKind, str]] = {
    ChannelKind.ROOT: "root",
    ChannelKind.SESSION: "session",
    ChannelKind.CHAT: "chat",
}


def reducer_name_for(uri: str) -> str | None:
    """The reducer that owns *uri*, or ``None`` if we do not implement it."""
    return _REDUCERS.get(classify(uri))


def session_uri(session_id: str) -> str:
    return f"{_SESSION_PREFIX}/{session_id}"


def chat_uri(chat_id: str) -> str:
    return f"{_CHAT_PREFIX}/{chat_id}"
