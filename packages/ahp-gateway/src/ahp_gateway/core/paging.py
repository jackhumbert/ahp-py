"""One flat, paged `listSessions` across every node.

Each node pages its own sessions newest-first. The gateway merges one page from
each into a single newest-first page, and the cursor it hands back records,
per node, where that node's stream stands: the node cursor of the page being
consumed and how many of its items the surface has already been shown. That
keeps the gateway stateless between pages (a cursor survives a gateway restart)
and exact (no item is skipped or repeated at a page boundary, which a
"everything older than the last item" cursor cannot promise across nodes whose
clocks disagree).
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from ahp_protocol.errors import invalid_params

__all__ = [
    "DEFAULT_PAGE_SIZE",
    "NodePosition",
    "Positions",
    "check_limit",
    "decode_cursor",
    "encode_cursor",
    "has_more",
    "merge_pages",
]

DEFAULT_PAGE_SIZE: Final = 50
#: Consecutive already-consumed pages one node may hand back in one listing.
_MAX_EMPTY_PAGES: Final = 64
_DONE: Final = "done"


@dataclass(frozen=True)
class NodePosition:
    """Where one node's session stream stands for this surface.

    `cursor` is the node's own cursor for the page being consumed (``None`` is
    its first page), `skip` how many of that page's items were already shown.
    """

    cursor: str | None = None
    skip: int = 0


Positions = dict[str, NodePosition | None]
"""Per node: its position, or ``None`` once the node has nothing left."""

FetchPage = Callable[[str, str | None, int], Awaitable[Mapping[str, Any]]]
"""(node id, node cursor, limit) -> that node's `listSessions` result."""


def encode_cursor(positions: Positions) -> str:
    wire = {
        node: _DONE if position is None else [position.cursor, position.skip]
        for node, position in sorted(positions.items())
    }
    raw = json.dumps(wire, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> Positions:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        wire = json.loads(base64.urlsafe_b64decode(padded.encode()))
    except (binascii.Error, ValueError) as exc:
        raise invalid_params("cursor is not one this gateway issued") from exc
    if not isinstance(wire, dict):
        raise invalid_params("cursor is not one this gateway issued")
    positions: Positions = {}
    for node, entry in wire.items():
        if entry == _DONE:
            positions[node] = None
        elif (
            isinstance(entry, list)
            and len(entry) == 2
            and (entry[0] is None or isinstance(entry[0], str))
            and isinstance(entry[1], int)
            and entry[1] >= 0
        ):
            positions[node] = NodePosition(entry[0], entry[1])
        else:
            raise invalid_params("cursor is not one this gateway issued")
    return positions


def _order_key(summary: Mapping[str, Any]) -> tuple[str, str]:
    # The same total order a node uses: most recently modified first, with
    # the URI breaking ties so the merge is deterministic.
    return (str(summary.get("modifiedAt", "")), str(summary.get("resource", "")))


async def merge_pages(
    nodes: Sequence[str],
    positions: Positions,
    limit: int,
    fetch: FetchPage,
) -> tuple[list[Mapping[str, Any]], Positions]:
    """One merged page, and the positions to resume from.

    A node may return fewer items than asked for and still have more. Its last
    returned item is then a **horizon**: nothing older than it may be emitted
    from any node yet, because that node's unseen sessions could be newer. So
    the merged page stops at the newest horizon, which can make it shorter
    than `limit` - a short page with a `nextCursor` is legal, and emitting a
    session ahead of a newer one on the next page is not.

    A node absent from `positions` (it joined after the first page) starts at
    its beginning. A node that fails is reported by `fetch` raising; the
    caller decides whether one dead node fails the whole listing.
    """
    candidates: list[tuple[str, Mapping[str, Any]]] = []
    pages: dict[str, tuple[NodePosition, int, str | None]] = {}
    horizons: list[tuple[str, str]] = []
    for node in nodes:
        position = positions.get(node, NodePosition())
        skipped = 0
        while position is not None:
            result = await fetch(node, position.cursor, position.skip + limit)
            raw = result.get("items")
            items = (
                [item for item in raw if isinstance(item, Mapping)] if isinstance(raw, list) else []
            )
            next_cursor = result.get("nextCursor")
            next_cursor = next_cursor if isinstance(next_cursor, str) else None
            if position.skip >= len(items) and next_cursor is not None:
                # Everything on this page was already shown: move on to the
                # next one rather than report a node with more as having none.
                # Bounded, so a node that keeps handing back a cursor with
                # nothing behind it ends its own listing instead of hanging
                # everyone's.
                skipped += 1
                if skipped > _MAX_EMPTY_PAGES or next_cursor == position.cursor:
                    break
                position = NodePosition(next_cursor, 0)
                continue
            pages[node] = (position, len(items), next_cursor)
            candidates.extend((node, item) for item in items[position.skip :])
            if next_cursor is not None and items:
                horizons.append(_order_key(items[-1]))
            break

    candidates.sort(key=lambda candidate: _order_key(candidate[1]), reverse=True)
    horizon = max(horizons, default=None)
    page: list[tuple[str, Mapping[str, Any]]] = []
    for candidate in candidates:
        if len(page) == limit or (horizon is not None and _order_key(candidate[1]) < horizon):
            break
        page.append(candidate)
    taken = dict.fromkeys(pages, 0)
    for node, _ in page:
        taken[node] += 1

    resumed: Positions = {node: None for node in nodes if node not in pages}
    for node, (position, count, next_cursor) in pages.items():
        consumed = position.skip + taken[node]
        if consumed < count:
            resumed[node] = NodePosition(position.cursor, consumed)
        elif next_cursor is not None:
            resumed[node] = NodePosition(next_cursor, 0)
        else:
            resumed[node] = None
    return [item for _, item in page], resumed


def has_more(positions: Positions) -> bool:
    return any(position is not None for position in positions.values())


def check_limit(value: Any) -> int:
    if value is None:
        return DEFAULT_PAGE_SIZE
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise invalid_params("limit must be a positive integer")
    return value
