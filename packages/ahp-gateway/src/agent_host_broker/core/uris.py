"""The aggregated namespace: which node a URI belongs to.

Two kinds of URI cross the broker, and they need opposite treatment.

**Channel URIs** (sessions, chats, terminals, annotations) are client-chosen
and opaque - `agent_host_protocol.channels` forbids parsing them, and VS Code
expects the exact string it minted back. They pass through verbatim and the
broker remembers which node owns each one (:class:`ChannelOwners`).

**File URIs** are each node's own path space, and two nodes both have
`file:///home/dev`. A `resourceRead` carries nothing but the URI, so the node
has to be in the URI itself (docs/plan.md §6, fork 2). The surfaces see them
under the broker's own scheme, never as `file:`, so a client can never mistake
another machine's file for one of its own, and a client's genuine `file:` URI
is never mistaken for a node's:

* ``ahp-file:///<node>/<rel>`` - a path under the node's root (the
  `defaultDirectory` it advertised). This is one browsable tree:
  ``ahp-file:///`` lists the nodes (answered by the broker itself), and
  ``ahp-file:///<node>`` *is* that node's root, so a folder picker walks from
  "which machine" straight into its projects.
* ``ahp-file://<node>/<absolute path>`` - anything on the node outside its root
  (a node without a root puts everything here). Routable, never browsed to.

The nodes keep speaking plain `file:`; the broker translates at the edge. Only
whole string values are rewritten; a path quoted inside prose is left alone,
because rewriting free text would corrupt what the agent said.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final
from urllib.parse import quote, unquote

from agent_host_protocol import ROOT_URI

__all__ = [
    "SCHEME",
    "VIRTUAL_ROOT",
    "ChannelOwners",
    "ForeignUriError",
    "is_virtual_root",
    "learn_owned_channels",
    "node_of",
    "qualify_file_uris",
    "root_of",
    "unqualify_file_uris",
]

SCHEME: Final = "ahp-file"
_PREFIX: Final = f"{SCHEME}://"
#: The top of the tree: one directory per node.
VIRTUAL_ROOT: Final = f"{SCHEME}:///"
_LOCAL_PREFIX: Final = "file:///"
_FILE_PREFIX: Final = "file://"
#: Characters kept literal when a decoded path is written back into a URI.
_PATH_SAFE: Final = "/:@!$&'()*+,;=-._~"

#: Keys whose string value names a channel the sender owns. Read off the
#: shapes the host emits: summaries and snapshots carry `resource`, action
#: envelopes `channel`, chat catalogue entries `resource`, terminal and
#: annotations references `resource`.
_OWNED_KEYS: Final = frozenset({"resource", "channel"})


class ForeignUriError(ValueError):
    """A request routed to one node named a file on another, or no node at all."""


def root_of(default_directory: Any) -> str | None:
    """A node's root as a decoded path ("home/dev/src", "C:/work"), from the
    `defaultDirectory` it advertised; None when it advertised none."""
    if not isinstance(default_directory, str) or not default_directory.startswith(_LOCAL_PREFIX):
        return None
    root = unquote(default_directory[len(_LOCAL_PREFIX) :]).rstrip("/")
    return root or None


def is_virtual_root(value: Any) -> bool:
    return isinstance(value, str) and value.rstrip("/") == f"{SCHEME}:"


def node_of(value: Any) -> str | None:
    """The node an `ahp-file` URI names, or None for anything else (and for the
    virtual root, which names every node)."""
    if not isinstance(value, str) or not value.startswith(_PREFIX):
        return None
    rest = value[len(_PREFIX) :]
    if rest.startswith("/"):
        node, _, _ = rest[1:].partition("/")
    else:
        node, _, _ = rest.partition("/")
    return node or None


def _drive_folded(path: str) -> str:
    """`c:/x` and `C:/x` are one Windows path; clients spell the drive both ways."""
    return path[0].lower() + path[1:] if len(path) > 1 and path[1] == ":" else path


def _qualify_one(value: str, node_id: str, root: str | None) -> str:
    absolute = unquote(value[len(_LOCAL_PREFIX) :])
    folded, folded_root = _drive_folded(absolute), _drive_folded(root) if root else None
    if (
        root is not None
        and folded_root is not None
        and (folded == folded_root or folded.startswith(folded_root + "/"))
    ):
        rel = absolute[len(root) :].strip("/")
        return f"{_PREFIX}/{node_id}" + (f"/{quote(rel, safe=_PATH_SAFE)}" if rel else "")
    if root is None:
        rel = absolute.strip("/")
        return f"{_PREFIX}/{node_id}" + (f"/{quote(rel, safe=_PATH_SAFE)}" if rel else "")
    return f"{_PREFIX}{node_id}/{quote(absolute.lstrip('/'), safe=_PATH_SAFE)}"


def _unqualify_one(value: str, node_id: str, root: str | None) -> str:
    rest = value[len(_PREFIX) :]
    if rest.startswith("/"):
        node, _, rel = rest[1:].partition("/")
        if not node:
            raise ForeignUriError(f"{value} is the list of nodes, not a file on one")
        if node != node_id:
            raise ForeignUriError(f"{value} is on node {node!r}, not {node_id!r}")
        rel = rel.strip("/")
        if ".." in unquote(rel).split("/"):
            # Relative to a root, `..` would climb out of it; a legitimate
            # client never sends one (the host refuses them too).
            raise ForeignUriError(f"{value} climbs out of {node_id}'s root")
        base = quote(root, safe=_PATH_SAFE) if root is not None else ""
        joined = "/".join(part for part in (base, rel) if part)
        return f"{_LOCAL_PREFIX}{joined}"
    node, _, absolute = rest.partition("/")
    if node != node_id:
        raise ForeignUriError(f"{value} is on node {node!r}, not {node_id!r}")
    return f"{_LOCAL_PREFIX}{absolute}"


def qualify_file_uris(value: Any, node_id: str, root: str | None = None) -> Any:
    """Node -> surface: `file:///p` becomes an `ahp-file` URI, everywhere."""
    if isinstance(value, str):
        return _qualify_one(value, node_id, root) if value.startswith(_LOCAL_PREFIX) else value
    if isinstance(value, Mapping):
        return {key: qualify_file_uris(item, node_id, root) for key, item in value.items()}
    if isinstance(value, list):
        return [qualify_file_uris(item, node_id, root) for item in value]
    return value


def unqualify_file_uris(value: Any, node_id: str, root: str | None = None) -> Any:
    """Surface -> node: an `ahp-file` URI on this node becomes its `file:///p`.

    One naming a *different* node raises :class:`ForeignUriError`: forwarding it
    would ask this node about a path on another machine, and the node would
    answer about its own file of the same name. A plain `file:` URI is the
    client's own and passes through untouched.
    """
    if isinstance(value, str):
        return _unqualify_one(value, node_id, root) if value.startswith(_PREFIX) else value
    if isinstance(value, Mapping):
        return {key: unqualify_file_uris(item, node_id, root) for key, item in value.items()}
    if isinstance(value, list):
        return [unqualify_file_uris(item, node_id, root) for item in value]
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
    return {uri for uri in found if not uri.startswith((_FILE_PREFIX, _PREFIX))}


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
