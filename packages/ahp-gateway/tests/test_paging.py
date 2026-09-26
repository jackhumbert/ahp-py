"""The merged `listSessions`: exact across page boundaries, stateless cursor."""

from collections.abc import Mapping
from typing import Any

import pytest
from agent_host_protocol import AhpError

from agent_host_broker.core.paging import (
    check_limit,
    decode_cursor,
    encode_cursor,
    has_more,
    merge_pages,
)


def _summaries(node: str, times: list[str]) -> list[dict[str, Any]]:
    return [{"resource": f"{node}:/{t}", "modifiedAt": t} for t in times]


class FakeNodes:
    """Nodes that page their sessions newest-first, `page_cap` at most per call."""

    def __init__(self, sessions: dict[str, list[dict[str, Any]]], page_cap: int = 1000) -> None:
        self.sessions = {
            node: sorted(items, key=lambda s: (s["modifiedAt"], s["resource"]), reverse=True)
            for node, items in sessions.items()
        }
        self.page_cap = page_cap

    async def fetch(self, node: str, cursor: str | None, limit: int) -> Mapping[str, Any]:
        start = int(cursor) if cursor is not None else 0
        size = min(limit, self.page_cap)
        items = self.sessions[node][start : start + size]
        result: dict[str, Any] = {"items": items}
        if start + size < len(self.sessions[node]):
            result["nextCursor"] = str(start + size)
        return result


async def _walk(nodes: FakeNodes, limit: int) -> list[str]:
    seen: list[str] = []
    positions: dict[str, Any] = {}
    while True:
        page, positions = await merge_pages(list(nodes.sessions), positions, limit, nodes.fetch)
        seen.extend(item["resource"] for item in page)
        if not has_more(positions):
            return seen
        positions = decode_cursor(encode_cursor(positions))


@pytest.mark.parametrize("limit", [1, 2, 3, 7, 50])
@pytest.mark.parametrize("page_cap", [1, 2, 1000])
async def test_every_session_appears_once_newest_first(limit: int, page_cap: int) -> None:
    nodes = FakeNodes(
        {
            "a": _summaries("a", ["01", "04", "05", "09"]),
            "b": _summaries("b", ["02", "03", "06"]),
            "c": _summaries("c", ["07", "08"]),
        },
        page_cap=page_cap,
    )
    seen = await _walk(nodes, limit)
    expected = sorted(
        (s for items in nodes.sessions.values() for s in items),
        key=lambda s: s["modifiedAt"],
        reverse=True,
    )
    assert seen == [s["resource"] for s in expected]


async def test_no_nodes_is_an_empty_last_page() -> None:
    page, positions = await merge_pages([], {}, 10, FakeNodes({}).fetch)
    assert page == []
    assert not has_more(positions)


def test_a_cursor_round_trips() -> None:
    positions: Any = {"a": None, "b": None}
    assert decode_cursor(encode_cursor(positions)) == positions


@pytest.mark.parametrize("cursor", ["not-base64!!", "bnVsbA", "eyJhIjpbMSwyXX0"])
def test_a_forged_cursor_is_invalid_params(cursor: str) -> None:
    with pytest.raises(AhpError) as caught:
        decode_cursor(cursor)
    assert caught.value.code == -32602


@pytest.mark.parametrize("limit", [0, -1, True, "5"])
def test_a_bad_limit_is_invalid_params(limit: Any) -> None:
    with pytest.raises(AhpError):
        check_limit(limit)
