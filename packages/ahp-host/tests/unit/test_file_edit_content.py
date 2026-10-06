"""`fileEdit` tool-result content, and the bounded store behind it.

1.0.0 has `ToolResultFileEditContent`: a tool call's result can BE a diff, with
`ContentRef`s a client reads to render it. No provider could produce one --
there was nowhere to put the bytes outside a changeset. `TurnSink.file_edit`
parks them in the session's content store, which `resourceRead` already
serves; and since that store now grows with every tool call rather than only
with every changeset refresh, it is bounded.
"""

from __future__ import annotations

from typing import Any

import pytest
from ahp_protocol.channels import ROOT_URI

from ahp_host.core import ConnectionInfo, Host, LoopbackSingleUserPolicy
from ahp_host.core.changesets import CONTENT_SCHEME, ContentStore, content_uris
from ahp_host.provider import EchoProvider, EchoSession
from ahp_host.provider.base import AgentSessionContext, TurnSink, UserMessage
from ahp_host.provider.changes import FileChange

from .hosting import assert_frames_valid, connect, finished, open_session, shut, state, turn_started

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class TestTheStoreIsBounded:
    def test_the_least_recently_used_content_goes_first(self) -> None:
        store = ContentStore(max_bytes=10)
        first = store.put(b"aaaa")["uri"]
        second = store.put(b"bbbb")["uri"]
        store.get(first)  # read: used, so the OTHER one is now oldest
        third = store.put(b"cccc")["uri"]
        assert store.owns(first)
        assert store.owns(third)
        assert not store.owns(second)
        assert store.size == 8

    def test_putting_unchanged_content_again_is_a_use_not_a_copy(self) -> None:
        store = ContentStore(max_bytes=8)
        first = store.put(b"aaaa")["uri"]
        store.put(b"bbbb")
        assert store.put(b"aaaa")["uri"] == first
        store.put(b"cccc")
        assert store.owns(first), "a republished file was evicted as if unused"
        assert len(store) == 2

    def test_one_blob_over_budget_is_still_served_once(self) -> None:
        """A ref the caller is about to publish must resolve."""
        store = ContentStore(max_bytes=2)
        uri = store.put(b"too big")["uri"]
        assert store.get(uri).data == b"too big"

    def test_no_bound_when_asked_for_none(self) -> None:
        store = ContentStore(max_bytes=None)
        uris = [store.put(bytes([n]) * 1024)["uri"] for n in range(64)]
        assert all(store.owns(u) for u in uris)

    def test_absorb_can_take_only_what_moves(self) -> None:
        source, target = ContentStore(), ContentStore()
        kept = source.put(b"moving")["uri"]
        left = source.put(b"staying")["uri"]
        target.absorb(source, {kept})
        assert target.owns(kept)
        assert not target.owns(left)
        assert source.owns(kept), "copied, not moved: the source may still use it"

    def test_content_uris_finds_refs_anywhere_in_a_state_tree(self) -> None:
        tree = {"a": [{"uri": f"{CONTENT_SCHEME}/x"}, ("y", f"{CONTENT_SCHEME}/z")], "n": 1}
        assert content_uris(tree) == {f"{CONTENT_SCHEME}/x", f"{CONTENT_SCHEME}/z"}


_CHANGE = FileChange(uri="file:///work/notes.md", before=b"# Notes\n", after=b"# Notes\nmore\n")


class Editing(EchoSession):
    async def send_user_message(self, message: UserMessage, sink: TurnSink) -> None:
        await sink.tool_call_started("edit-1", "write", display_name="Write")
        content = await sink.file_edit(_CHANGE)
        # A bare content list is the result's content.
        await sink.tool_call_completed("edit-1", [content], past_tense_message="Edited notes.md")


class EditingProvider(EchoProvider):
    async def create_session(self, context: AgentSessionContext) -> Editing:
        return Editing(context)


class SessionPrivate(LoopbackSingleUserPolicy):
    """`c2` may see nothing but the root channel."""

    def may_see_channel(self, info: ConnectionInfo, channel: str) -> bool:
        return info.client_id != "c2" or channel == ROOT_URI


def _completed_content(host: Host, chat: str) -> list[dict[str, Any]]:
    turns = state(host, chat).get("turns") or [{}]
    for part in turns[-1].get("responseParts", []):
        call = part.get("toolCall")
        if isinstance(call, dict) and call.get("toolCallId") == "edit-1":
            content = call.get("content")
            return list(content) if isinstance(content, list) else []
    return []


async def test_a_tool_result_can_be_a_diff_a_client_can_read() -> None:
    host = Host(EditingProvider(), SessionPrivate())
    wire, serving = await connect(host)
    other, other_serving = await connect(host, "c2")
    try:
        chat = await open_session(wire, "echo:/edit-1")
        await wire.dispatch(chat, turn_started("t1"))
        assert await wire.until(lambda: finished(host, chat, "t1"))

        [item] = _completed_content(host, chat)
        assert item["type"] == "fileEdit"
        assert item["before"]["uri"] == item["after"]["uri"] == _CHANGE.uri
        assert item["diff"] == {"added": 1, "removed": 0}
        after = item["after"]["content"]["uri"]
        response = await wire.request("resourceRead", {"channel": ROOT_URI, "uri": after})
        assert response["result"]["data"] == "# Notes\nmore\n"

        # Served under the SESSION's visibility, like a changeset's content:
        # a client that may not see the session may not read its diffs.
        refused = await other.request("resourceRead", {"channel": ROOT_URI, "uri": after})
        assert refused["error"]["code"] == -32009
        assert_frames_valid(wire, ("chat", chat))
    finally:
        await other.close()
        other_serving.cancel()
        await shut(host, wire, serving)


async def test_the_embedder_bounds_each_sessions_store() -> None:
    """Past `max_content_bytes` the oldest content goes, and a ref to it reads
    as missing -- what a diff cache honestly has to say."""
    host = Host(EditingProvider(), LoopbackSingleUserPolicy(), max_content_bytes=24)
    wire, serving = await connect(host)
    try:
        chat = await open_session(wire, "echo:/edit-2")
        await wire.dispatch(chat, turn_started("t1"))
        assert await wire.until(lambda: finished(host, chat, "t1"))
        [item] = _completed_content(host, chat)
        before = item["before"]["content"]["uri"]
        after = item["after"]["content"]["uri"]
        # 8 + 13 bytes fit; a third blob does not, and the oldest goes.
        session = host._sessions["echo:/edit-2"]
        session.content.put(b"0123456789")
        response = await wire.request("resourceRead", {"channel": ROOT_URI, "uri": before})
        assert response["error"]["code"] == -32008
        response = await wire.request("resourceRead", {"channel": ROOT_URI, "uri": after})
        assert "result" in response
    finally:
        await shut(host, wire, serving)
