"""The aggregated namespace: which node a URI belongs to.

Two kinds of URI cross the gateway, and they need opposite treatment.

**Channel URIs** (sessions, chats, terminals, annotations) are client-chosen
and opaque - `ahp_protocol.channels` forbids parsing them, and VS Code
expects the exact string it minted back. They pass through verbatim and the
gateway remembers which node owns each one (:class:`ChannelOwners`).

**File URIs** are each node's own path space, and two nodes both have
`file:///home/dev`. A `resourceRead` carries nothing but the URI, so the node
has to be in the URI itself (docs/plan.md §6, fork 2). The surfaces see them
under the gateway's own scheme, never as `file:`, so a client can never mistake
another machine's file for one of its own, and a client's genuine `file:` URI
is never mistaken for a node's:

* ``ahp-file:///<node>/<rel>`` - a path under the node's root (the
  `defaultDirectory` it advertised). This is one browsable tree:
  ``ahp-file:///`` lists the nodes (answered by the gateway itself), and
  ``ahp-file:///<node>`` *is* that node's root, so a folder picker walks from
  "which machine" straight into its projects.
* ``ahp-file://<node>/<absolute path>`` - anything on the node outside its root
  (a node without a root puts everything here). Routable, never browsed to.

**VS Code's alias.** VS Code keeps only the *path* of a host's
`defaultDirectory` and browses the host as `file:` from there, so from it the
tree arrives as ``file:///<node>/<rel>`` (and ``file:///`` for the list of
nodes). Inbound, a `file:` URI whose first path segment is a node id is read
as that node's ``ahp-file`` URI (:func:`from_client_alias`). The cost: a
client's genuine local path that starts with ``/<node id>/`` would be misread.

**Flat places** (opt-in, ``flat_places``). An editor labels a folder by the
last segment of its URI, so under the nested layout a person who picks node
``node-a`` then root ``proj`` sees only ``proj``. Flat places fold the node into
that segment: ``file:///<root>/<rest>`` on node N is
``ahp-file:///<N>: <root>/<rest>`` (the top of a node, ``file:///``, stays the
bare ``ahp-file:///<N>``), and ``ahp-file:///`` lists one ``<N>: <root>`` entry per
root each node serves. A first segment without ``": "`` keeps the nested
meaning, so older URIs still route. Node ids never contain ``": "``.

**Passthrough** (opt-in, ``passthrough_node``, per connection). When a connection
is narrowed to one file-serving node, none of the above is needed: that node's
`file:` URIs cross verbatim in both directions, so a client sees its real
absolute paths, and every `file:` URI the client sends is that node's, so the
client's `file:///<node>/...` alias is not read there (it is a real path on the
passthrough node). `ahp-file` URIs still route, and other nodes on the connection
are reachable only by those explicit `ahp-file` URIs.

The nodes keep speaking plain `file:`; the gateway translates at the edge. Only
whole string values are rewritten; a path quoted inside prose is left alone,
because rewriting free text would corrupt what the agent said.

**Content refs** (a `ContentRef`'s `uri`: a diff's before and after, a large
tool result, an image) are host-minted and opaque, like channels - the
sibling host's `ahp-changeset-content:` scheme is private to it, and another
host mints its own. So they too pass verbatim, and the gateway remembers which
node named each one, so that a `resourceRead` of it goes back to that node.
They cannot be rewritten the way file URIs are: a `uri` key also carries URIs
the *client* published (a message's attachment), and the client must get back
the exact string it sent.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import Any, Final
from urllib.parse import quote, unquote

from ahp_protocol import ROOT_URI
from ahp_protocol.channels import AUTOMATIONS_URI, canonical_channel

__all__ = [
    "PLACE_SEPARATOR",
    "SCHEME",
    "VIRTUAL_ROOT",
    "ChannelOwners",
    "ForeignUriError",
    "canonical_uri",
    "from_client_alias",
    "is_virtual_root",
    "learn_owned",
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
#: Between node id and root name in a flat place's path segment.
PLACE_SEPARATOR: Final = ": "

#: Keys whose string value names a channel the sender owns. Read off the
#: shapes the host emits: summaries and snapshots carry `resource`, action
#: envelopes `channel`, chat catalogue entries `resource`, terminal and
#: annotations references `resource`.
_OWNED_KEYS: Final = frozenset({"resource", "channel"})
#: A changeset's catalogue entry names its channel by template, and a
#: template with no variable "is itself a subscribable URI".
_TEMPLATE_KEY: Final = "uriTemplate"
#: Where a content ref travels: `ContentRef.uri`, and the shapes that extend
#: it (`ToolResultResourceContent`, `ResourceResponsePart`, `ToolInput`).
_CONTENT_KEY: Final = "uri"


class ForeignUriError(ValueError):
    """A request routed to one node named a file on another, or no node at all."""


def canonical_uri(value: Any) -> Any:
    """`ahp-file:/<node>/p` as `ahp-file:///<node>/p`.

    A URI's `toString()` drops an empty authority for a non-`file` scheme, so a
    client sends back the single-slash spelling of what the gateway advertised.
    One with a real authority (`ahp-file://x/...`) is left as it is.
    """
    if isinstance(value, str) and value.startswith(f"{SCHEME}:/") and not value.startswith(_PREFIX):
        return f"{_PREFIX}/{value[len(SCHEME) + 2 :]}"
    return value


def root_of(default_directory: Any) -> str | None:
    """A node's root as a decoded path ("home/dev/src", "C:/work"), from the
    `defaultDirectory` it advertised; None when it advertised none."""
    if not isinstance(default_directory, str) or not default_directory.startswith(_LOCAL_PREFIX):
        return None
    root = unquote(default_directory[len(_LOCAL_PREFIX) :]).rstrip("/")
    return root or None


def is_virtual_root(value: Any) -> bool:
    return isinstance(value, str) and value.rstrip("/") == f"{SCHEME}:"


def split_place(segment: str) -> tuple[str, str | None]:
    """A decoded first path segment as (node, root): `node-a: proj` is
    ("node-a", "proj"); a bare node id is (id, None)."""
    node, separator, root = segment.partition(PLACE_SEPARATOR)
    return (node, root) if separator else (segment, None)


def place_segment(node_id: str, root: str) -> str:
    """The path segment (percent-encoded) of the flat place `<node>: <root>`."""
    return quote(f"{node_id}{PLACE_SEPARATOR}{root}", safe=_PATH_SAFE)


def node_of(value: Any, *, flat: bool = False) -> str | None:
    """The node an `ahp-file` URI names, or None for anything else (and for the
    virtual root, which names every node)."""
    value = canonical_uri(value)
    if not isinstance(value, str) or not value.startswith(_PREFIX):
        return None
    rest = value[len(_PREFIX) :]
    if rest.startswith("/"):
        node, _, _ = rest[1:].partition("/")
    else:
        node, _, _ = rest.partition("/")
    if flat and PLACE_SEPARATOR in unquote(node):
        node = split_place(unquote(node))[0]
    return node or None


def from_client_alias(
    value: Any, node_ids: Collection[str], *, root_uri: bool = False, flat: bool = False
) -> Any:
    """Read VS Code's `file:///<node>/...` spelling as `ahp-file:///<node>/...`, everywhere.

    With `root_uri`, a bare `file:///` is the list of nodes too; that is only
    meant for the `uri` of a resource command, where a client browsing the
    tree from its top would send it.
    """
    if isinstance(value, str):
        value = canonical_uri(value)
        if not value.startswith(_LOCAL_PREFIX):
            return value
        rest = value[len(_LOCAL_PREFIX) :]
        if not rest.strip("/"):
            return VIRTUAL_ROOT if root_uri else value
        first, _, _ = rest.partition("/")
        name = unquote(first)
        if flat:
            name = split_place(name)[0]
        return f"{_PREFIX}/{rest}" if name in node_ids else value
    if isinstance(value, Mapping):
        return {key: from_client_alias(item, node_ids, flat=flat) for key, item in value.items()}
    if isinstance(value, list):
        return [from_client_alias(item, node_ids, flat=flat) for item in value]
    return value


def _drive_folded(path: str) -> str:
    """`c:/x` and `C:/x` are one Windows path; clients spell the drive both ways."""
    return path[0].lower() + path[1:] if len(path) > 1 and path[1] == ":" else path


def _qualify_one(value: str, node_id: str, root: str | None, flat: bool = False) -> str:
    absolute = unquote(value[len(_LOCAL_PREFIX) :])
    if flat:
        place, _, rel = absolute.lstrip("/").partition("/")
        if not place:
            return f"{_PREFIX}/{node_id}"
        rel = rel.strip("/")
        return f"{_PREFIX}/{place_segment(node_id, place)}" + (
            f"/{quote(rel, safe=_PATH_SAFE)}" if rel else ""
        )
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


def _unqualify_one(value: str, node_id: str, root: str | None, flat: bool = False) -> str:
    rest = value[len(_PREFIX) :]
    if rest.startswith("/"):
        node, _, rel = rest[1:].partition("/")
        place: str | None = None
        if flat and PLACE_SEPARATOR in unquote(node):
            node, place = split_place(unquote(node))
        if not node:
            raise ForeignUriError(f"{value} is the list of nodes, not a file on one")
        if node != node_id:
            raise ForeignUriError(f"{value} is on node {node!r}, not {node_id!r}")
        rel = rel.strip("/")
        if ".." in unquote(rel).split("/"):
            # Relative to a root, `..` would climb out of it; a legitimate
            # client never sends one (the host refuses them too).
            raise ForeignUriError(f"{value} climbs out of {node_id}'s root")
        if place is not None:
            if not place or place == ".." or "/" in place:
                raise ForeignUriError(f"{value} names no root on {node_id}")
            joined = "/".join(part for part in (quote(place, safe=_PATH_SAFE), rel) if part)
            return f"{_LOCAL_PREFIX}{joined}"
        base = quote(root, safe=_PATH_SAFE) if root is not None else ""
        joined = "/".join(part for part in (base, rel) if part)
        return f"{_LOCAL_PREFIX}{joined}"
    node, _, absolute = rest.partition("/")
    if node != node_id:
        raise ForeignUriError(f"{value} is on node {node!r}, not {node_id!r}")
    return f"{_LOCAL_PREFIX}{absolute}"


def qualify_file_uris(
    value: Any, node_id: str, root: str | None = None, *, flat: bool = False
) -> Any:
    """Node -> surface: `file:///p` becomes an `ahp-file` URI, everywhere.

    With `flat`, the first path segment is a root and joins the node id into one
    segment (`ahp-file:///<node>: <root>/...`); `root` is not used.
    """
    if isinstance(value, str):
        if value.startswith(_LOCAL_PREFIX):
            return _qualify_one(value, node_id, root, flat)
        return value
    if isinstance(value, Mapping):
        return {
            key: qualify_file_uris(item, node_id, root, flat=flat) for key, item in value.items()
        }
    if isinstance(value, list):
        return [qualify_file_uris(item, node_id, root, flat=flat) for item in value]
    return value


def unqualify_file_uris(
    value: Any, node_id: str, root: str | None = None, *, flat: bool = False
) -> Any:
    """Surface -> node: an `ahp-file` URI on this node becomes its `file:///p`.

    One naming a *different* node raises :class:`ForeignUriError`: forwarding it
    would ask this node about a path on another machine, and the node would
    answer about its own file of the same name. A plain `file:` URI is the
    client's own and passes through untouched.
    """
    if isinstance(value, str):
        value = canonical_uri(value)
        return _unqualify_one(value, node_id, root, flat) if value.startswith(_PREFIX) else value
    if isinstance(value, Mapping):
        return {
            key: unqualify_file_uris(item, node_id, root, flat=flat) for key, item in value.items()
        }
    if isinstance(value, list):
        return [unqualify_file_uris(item, node_id, root, flat=flat) for item in value]
    return value


def learn_owned(value: Any) -> tuple[set[str], set[str]]:
    """Every channel, and every content ref, a node's payload names as its own.

    A walk rather than a per-shape table, so a chat or terminal the gateway has
    never seen a command for is still routable the moment it appears in any
    state the node sent - which is how a surface usually finds out about it.
    One walk for both, since a chat snapshot can be large.
    """
    channels: set[str] = set()
    content: set[str] = set()
    _walk(value, channels, content)
    # The two singletons every node has its own copy of. Claimed, the first
    # node to mention one would own it, and every request for the merged
    # channel would go there.
    channels -= {uri for uri in channels if canonical_channel(uri) in (ROOT_URI, AUTOMATIONS_URI)}
    # Files route by the node their URI names, never by who mentioned them.
    files = (_FILE_PREFIX, _PREFIX)
    return (
        {uri for uri in channels if not uri.startswith(files)},
        {uri for uri in content if not uri.startswith(files)},
    )


def learn_owned_channels(value: Any) -> set[str]:
    """Every channel URI a node's payload names as its own."""
    return learn_owned(value)[0]


def _walk(value: Any, channels: set[str], content: set[str]) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if isinstance(item, str):
                if not item:
                    continue
                if key in _OWNED_KEYS or (key == _TEMPLATE_KEY and "{" not in item):
                    channels.add(item)
                elif key == _CONTENT_KEY:
                    content.add(item)
            else:
                _walk(item, channels, content)
    elif isinstance(value, list):
        for item in value:
            _walk(item, channels, content)


class ChannelOwners:
    """Channel (or content) URI -> owning node, learned from what each node sends.

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
