"""Channel URIs and reducer routing.

Every channel is a URI-identified subscribable resource, and every command and
notification carries its URI as ``params.channel`` -- the routing key the whole
protocol is built on.

Two rules that are easy to get wrong:

* The root URI is matched by **exact string equality**. The reference client
  compares against the literal ``'ahp-root://'`` with ``===`` in three separate
  places: no normalisation, no trailing-slash tolerance, no scheme aliasing.
* Session and chat URIs are matched by **scheme prefix only**, and the rest is
  opaque. A peer must never parse or validate the remainder: the client chooses
  it, and real clients use forms other than a bare UUID -- ``ahpx`` sends
  ``<provider>:/<uuid>``.

**Never pick a reducer from a URI.** :func:`classify` is for display and
capability hints only. The scheme table below does not describe reality: VS Code
mints ``<provider>:/<uuid>`` for sessions, ``ahp-chat://<chatId>/<base64 session
uri>`` for chats, and three separate ``agenthost-terminal:`` forms for
terminals, none of which is ``ahp-terminal:``. Routing a reducer on a scheme
therefore applies *no* reducer at all, which freezes state silently while
actions keep arriving. Bind the reducer when the channel is registered; where
that is impossible, use :func:`reducer_for_state`, which reads the state's
shape.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import Enum
from typing import Any, Final

__all__ = [
    "AUTOMATIONS_URI",
    "ROOT_URI",
    "ChannelKind",
    "chat_uri",
    "classify",
    "reducer_for_state",
    "session_uri",
]

ROOT_URI: Final = "ahp-root://"

_SESSION_PREFIX: Final = "ahp-session:"
_CHAT_PREFIX: Final = "ahp-chat:"
_TERMINAL_PREFIX: Final = "ahp-terminal:"
_CHANGESET_PREFIX: Final = "ahp-changeset:"
_RESOURCE_WATCH_PREFIX: Final = "ahp-resource-watch:"
_OTLP_PREFIX: Final = "ahp-otlp:"
#: The automation catalogue is a singleton, like root (0.9.0).
AUTOMATIONS_URI: Final = "ahp-automations://"
_AUTOMATION_RUN_PREFIX: Final = "ahp-automation-run:"


class ChannelKind(Enum):
    ROOT = "root"
    SESSION = "session"
    CHAT = "chat"
    TERMINAL = "terminal"
    CHANGESET = "changeset"
    RESOURCE_WATCH = "resourceWatch"
    ANNOTATIONS = "annotations"
    OTLP = "otlp"
    AUTOMATION = "automation"
    AUTOMATION_RUN = "automationRun"
    #: A scheme we do not know. Clients MUST NOT subscribe to one, but a peer
    #: must still answer rather than crash.
    UNKNOWN = "unknown"


def classify(uri: str) -> ChannelKind:
    """The channel kind a URI *advertises*, for display and hints only.

    Not a routing function. See the module docstring: real clients mint URIs
    whose scheme does not appear in this table, so :data:`ChannelKind.UNKNOWN`
    here says nothing about what the channel actually carries.
    """
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
    if uri == AUTOMATIONS_URI:
        return ChannelKind.AUTOMATION
    if uri.startswith(_AUTOMATION_RUN_PREFIX):
        return ChannelKind.AUTOMATION_RUN
    return ChannelKind.UNKNOWN


#: Discriminating key -> reducer name, **in precedence order**.
#:
#: The order is load-bearing, not cosmetic. ``SessionState`` inlines
#: ``annotations`` and ``changesets`` onto itself, so an annotations-first table
#: would classify every session as an annotations channel; ``lifecycle`` is
#: checked before both for exactly that reason. Since 0.9.0 ``lifecycle`` is no
#: longer the session's alone -- ``TerminalState`` and ``AutomationRunState``
#: carry one too -- so ``claim`` and ``automation``, which only those two have,
#: are checked before it. ``agents`` comes first because ``RootState`` is the
#: only state carrying it, and ``root`` -- the watched directory of a
#: ``ResourceWatchState`` -- is checked after it so the two cannot collide.
#:
#: Verified against all 272 upstream reducer fixtures by
#: ``tests/conformance/test_state_shapes.py``: every one of them classifies to
#: the reducer the fixture itself declares, with no unclassifiable case --
#: root 7, session 79, chat 132, terminal 19, changeset 16, resourceWatch 2,
#: annotations 10, automation 5, automationRun 2. That is 272 correctness cases
#: from data neither peer wrote.
_SHAPE_ORDER: Final[tuple[tuple[str, str], ...]] = (
    ("agents", "root"),
    ("claim", "terminal"),
    ("automation", "automationRun"),
    ("lifecycle", "session"),
    ("turns", "chat"),
    ("files", "changeset"),
    ("root", "resourceWatch"),
    ("entries", "automation"),
    ("annotations", "annotations"),
)


def reducer_for_state(state: Any) -> str | None:
    """The reducer that owns a state object, read from its **shape**.

    Returns a key of ``ahp_protocol.reducers.REDUCERS``, or ``None`` when
    the shape is unrecognised or carries no discriminating key -- an empty
    snapshot is genuinely ambiguous, and guessing would be worse than declining.

    This is the fallback for a snapshot that arrived without the caller having
    said what it asked for. Prefer binding the reducer at registration: a peer
    that subscribed to a URI already knows what kind of channel it is.
    """
    if not isinstance(state, Mapping):
        return None
    for key, reducer in _SHAPE_ORDER:
        if key in state:
            return reducer
    return None


def session_uri(session_id: str) -> str:
    return f"{_SESSION_PREFIX}/{session_id}"


def chat_uri(chat_id: str) -> str:
    return f"{_CHAT_PREFIX}/{chat_id}"
