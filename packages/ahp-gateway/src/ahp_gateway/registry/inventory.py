"""Node records and per-node admission.

The registry answers one question - "may this principal start sessions on this
node?" - and answers it before any AHP frame reaches the node (docs/plan.md
§4). It is deliberately offline: no transport, no socket, no AHP. A decision
that needs the network to make is routing policy that leaked below the floor.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final, Protocol, runtime_checkable

__all__ = [
    "NodeDirectory",
    "NodeRecord",
    "Principal",
    "StaticInventory",
    "is_valid_node_id",
]

# A node id becomes the authority of every file URI the surfaces see from that
# node (`file://<node>/path`), so it has to be a legal, unambiguous URI host:
# lowercase DNS labels. Uppercase is refused rather than folded, because URI
# authorities compare case-insensitively and two ids differing only in case
# would then route to the same place.
_LABEL: Final = r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?"
_NODE_ID: Final = re.compile(rf"{_LABEL}(?:\.{_LABEL})*")


def is_valid_node_id(node_id: str) -> bool:
    return _NODE_ID.fullmatch(node_id) is not None and node_id != "localhost"


@dataclass(frozen=True)
class Principal:
    """Who is asking, as the identity provider asserted it.

    `groups` carries whatever the identity provider gates on - directory groups
    or app roles. The registry never learns how the principal was established;
    that is the authenticator's job, one layer up.
    """

    subject: str
    groups: frozenset[str] = frozenset()


@dataclass(frozen=True)
class NodeRecord:
    """One node in the fleet.

    `url` is opaque to the registry: the connector above interprets it. `groups`
    are the identity groups admitted to start sessions here; an empty set admits
    nobody, so a record added without thought is closed rather than open.
    """

    node_id: str
    url: str
    groups: frozenset[str] = frozenset()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not is_valid_node_id(self.node_id):
            raise ValueError(
                f"invalid node id {self.node_id!r}: it becomes a file-URI authority, "
                "so it must be lowercase DNS labels (and not 'localhost')"
            )

    def admits(self, principal: Principal) -> bool:
        return not self.groups.isdisjoint(principal.groups)


@runtime_checkable
class NodeDirectory(Protocol):
    """The nodes a principal may use. Order is the fleet's display order."""

    def nodes_for(self, principal: Principal) -> list[NodeRecord]: ...


class StaticInventory:
    """A declarative node inventory: the records are the configuration."""

    def __init__(self, records: Iterable[NodeRecord]) -> None:
        ordered = sorted(records, key=lambda record: record.node_id)
        ids = [record.node_id for record in ordered]
        duplicates = sorted({node_id for node_id in ids if ids.count(node_id) > 1})
        if duplicates:
            raise ValueError(f"duplicate node ids: {', '.join(duplicates)}")
        self._records = ordered

    @property
    def records(self) -> list[NodeRecord]:
        return list(self._records)

    def nodes_for(self, principal: Principal) -> list[NodeRecord]:
        return [record for record in self._records if record.admits(principal)]
