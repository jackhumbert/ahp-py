"""The aggregated namespace: which node a URI belongs to.

Two kinds of URI cross the broker, and they need opposite treatment.

**Channel URIs** (sessions, chats, terminals, annotations) are client-chosen
and opaque - `agent_host_protocol.channels` forbids parsing them, and VS Code
expects the exact string it minted back. They pass through verbatim and the
broker remembers which node owns each one (:class:`ChannelOwners`).

**File URIs** are the node's own path space, and two nodes both have
`file:///home/dev`. A `resourceRead` carries nothing but the URI, so the node
has to be in the URI itself: the broker shows the surfaces
`file://<node>/home/dev` and strips the authority again on the way back in
(docs/plan.md §6, fork 2: the working-directory authority). Only whole string
values with a `file:` prefix are rewritten; a path quoted inside prose is left
alone, because rewriting free text would corrupt what the agent said.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from agent_host_protocol import ROOT_URI

__all__ = [
    "ChannelOwners",
    "ForeignUriError",
    "file_authority",
    "learn_owned_channels",
    "qualify_file_uris",
    "unqualify_file_uris",
]

_LOCAL_PREFIX: Final = "file:///"
_FILE_PREFIX: Final = "file://"

#: Keys whose string value names a channel the sender owns. Read off the
#: shapes the host emits: summaries and snapshots carry `resource`, action
#: envelopes `channel`, chat catalogue entries `resource`, terminal and
#: annotations references `resource`.
_OWNED_KEYS: Final = frozenset({"resource", "channel"})


class ForeignUriError(ValueError):
    """A request routed to one node named a file on another."""


def file_authority(value: Any) -> str | None:
    """The node a qualified file URI names, or ``None`` for anything else."""
    if not isinstance(value, str) or not value.startswith(_FILE_PREFIX):
        return None
    rest = value[len(_FILE_PREFIX) :]
    authority, _, _ = rest.partition("/")
    return authority or None


def qualify_file_uris(value: Any, node_id: str) -> Any:
    """Node -> surface: `file:///p` becomes `file://<node>/p`, everywhere."""
    if isinstance(value, str):
        if value.startswith(_LOCAL_PREFIX):
            return f"{_FILE_PREFIX}{node_id}/{value[len(_LOCAL_PREFIX) :]}"
        return value
    if isinstance(value, Mapping):
        return {key: qualify_file_uris(item, node_id) for key, item in value.items()}
    if isinstance(value, list):
        return [qualify_file_uris(item, node_id) for item in value]
    return value


def unqualify_file_uris(value: Any, node_id: str) -> Any:
    """Surface -> node: `file://<node>/p` becomes `file:///p`.

    A file URI qualified with a *different* node raises :class:`ForeignUriError`:
    forwarding it would ask this node about a path on another machine, and the
    node would answer about its own file of the same name.
    """
    if isinstance(value, str):
        authority = file_authority(value)
        if authority is None:
            return value
        if authority != node_id:
            raise ForeignUriError(f"{value} is on node {authority!r}, not {node_id!r}")
        return f"{_LOCAL_PREFIX}{value[len(_FILE_PREFIX) + len(authority) + 1 :]}"
    if isinstance(value, Mapping):
        return {key: unqualify_file_uris(item, node_id) for key, item in value.items()}
    if isinstance(value, list):
        return [unqualify_file_uris(item, node_id) for item in value]
    return value


def learn_owned_channels(value: Any) -> set[str]:
    """Every channel URI a node's payload names as its own.

    A walk rather than a per-shape table, so a chat or terminal the broker has
    never seen a command for is still routable the moment it appears in any
    state the node sent - which is how a surface usually finds out about it.
    """
    found: set[str] = set()
    _walk(value, found)
    found.discard(ROOT_URI)
    return {uri for uri in found if not uri.startswith(_FILE_PREFIX)}


def _walk(value: Any, found: set[str]) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key in _OWNED_KEYS and isinstance(item, str) and item:
                found.add(item)
            else:
                _walk(item, found)
    elif isinstance(value, list):
        for item in value:
            _walk(item, found)


class ChannelOwners:
    """Channel URI -> owning node, learned from what each node sends.

    First writer wins. Two nodes claiming one URI would mean two clients minted
    the same UUID, or a node echoing a URI it does not own; either way the
    first route is kept rather than silently re-pointed mid-session. Nothing
    is ever forgotten: a node that drops keeps its channels, so a request for
    one is refused as "not connected" instead of falling through to another.
    """

    def __init__(self) -> None:
        self._owner: dict[str, str] = {}

    def claim(self, node_id: str, uris: set[str]) -> None:
        for uri in uris:
            self._owner.setdefault(uri, node_id)

    def owner_of(self, uri: str) -> str | None:
        return self._owner.get(uri)
