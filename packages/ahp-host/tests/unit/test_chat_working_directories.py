"""A chat's own folders: `chat/workingDirectorySet` and `...Removed` (1.0.0).

"`directory` MUST be one of the owning session's
`SessionState.workingDirectories`; a host MUST reject a directory that is not.
Only valid when the agent advertises `AgentCapabilities.multipleWorkingDirectories`."
Nothing routed either action, so the reducer applied any directory a client
named: a chat could claim a folder its session was never granted, and the
agent -- which decides what the chat's tools may touch -- never heard of it.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest

from ahp_host.core import Host, LoopbackSingleUserPolicy
from ahp_host.core.store import InMemorySessionStore, StoredSession
from ahp_host.provider import EchoProvider, EchoSession
from ahp_host.provider.base import AgentSessionContext, FollowsChatWorkingDirectories

from .hosting import assert_frames_valid, connect, open_session, shut, state

pytestmark = pytest.mark.anyio

_MULTIROOT: dict[str, Any] = {"multipleChats": {}, "multipleWorkingDirectories": {}}
_A, _B, _C = "file:///work/a", "file:///work/b", "file:///work/c"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class Following(EchoSession):
    def __init__(self, context: AgentSessionContext) -> None:
        super().__init__(context)
        self.chat_directories: list[tuple[str, list[str]]] = []

    async def chat_working_directories_changed(
        self, chat_uri: str, directories: Sequence[str]
    ) -> None:
        self.chat_directories.append((chat_uri, list(directories)))


class FollowingProvider(EchoProvider):
    def __init__(self, capabilities: dict[str, Any] | None = None) -> None:
        super().__init__(capabilities=_MULTIROOT if capabilities is None else capabilities)
        self.sessions: list[Following] = []

    async def create_session(self, context: AgentSessionContext) -> Following:
        session = Following(context)
        self.sessions.append(session)
        return session


class Saving(InMemorySessionStore):
    def __init__(self) -> None:
        self.saved: list[StoredSession] = []

    async def save_soon(self, session: StoredSession) -> None:
        self.saved.append(session)


def test_the_protocol_is_feature_detected() -> None:
    assert isinstance(Following.__new__(Following), FollowsChatWorkingDirectories)
    assert not isinstance(EchoSession.__new__(EchoSession), FollowsChatWorkingDirectories)


async def test_a_directory_outside_the_session_is_rejected() -> None:
    provider = FollowingProvider()
    host = Host(provider, LoopbackSingleUserPolicy())
    wire, serving = await connect(host)
    try:
        chat = await open_session(wire, "echo:/cwd-1", workingDirectories=[_A, _B])
        seq = await wire.dispatch(chat, {"type": "chat/workingDirectorySet", "directory": _C})
        echo = await wire.echoed(chat, seq)
        # Invariant 12: echoed with a reason, so the client reverts its guess.
        assert echo["rejectionReason"] == (
            "that directory is not one of the session's working directories"
        )
        assert "workingDirectories" not in state(host, chat)
        assert provider.sessions[0].chat_directories == []
    finally:
        await shut(host, wire, serving)


async def test_a_session_directory_narrows_the_chat_and_reaches_the_agent() -> None:
    provider = FollowingProvider()
    store = Saving()
    host = Host(provider, LoopbackSingleUserPolicy(), store=store)
    wire, serving = await connect(host)
    try:
        chat = await open_session(wire, "echo:/cwd-2", workingDirectories=[_A, _B])
        saved = len(store.saved)
        seq = await wire.dispatch(chat, {"type": "chat/workingDirectorySet", "directory": _B})
        assert "rejectionReason" not in await wire.echoed(chat, seq)
        await wire.until(lambda: bool(provider.sessions[0].chat_directories))
        assert state(host, chat)["workingDirectories"] == [_B]
        assert provider.sessions[0].chat_directories == [(chat, [_B])]
        assert len(store.saved) > saved, "the change was not persisted"

        seq = await wire.dispatch(chat, {"type": "chat/workingDirectoryRemoved", "directory": _B})
        assert "rejectionReason" not in await wire.echoed(chat, seq)
        await wire.until(lambda: len(provider.sessions[0].chat_directories) == 2)
        # Present but empty: "the chat has no working-directory tool access".
        assert state(host, chat)["workingDirectories"] == []
        assert provider.sessions[0].chat_directories[-1] == (chat, [])
    finally:
        await shut(host, wire, serving)


async def test_both_actions_need_the_capability() -> None:
    """ "When absent, clients MUST NOT mutate a session's or chat's
    working-directory set" -- removal included."""
    provider = FollowingProvider(capabilities={"multipleChats": {}})
    host = Host(provider, LoopbackSingleUserPolicy())
    wire, serving = await connect(host)
    try:
        chat = await open_session(wire, "echo:/cwd-3", workingDirectories=[_A])
        for kind in ("chat/workingDirectorySet", "chat/workingDirectoryRemoved"):
            seq = await wire.dispatch(chat, {"type": kind, "directory": _A})
            echo = await wire.echoed(chat, seq)
            assert echo["rejectionReason"] == (
                "this agent does not advertise multipleWorkingDirectories"
            ), kind
    finally:
        await shut(host, wire, serving)


async def test_a_directory_must_be_a_string() -> None:
    host = Host(FollowingProvider(), LoopbackSingleUserPolicy())
    wire, serving = await connect(host)
    try:
        chat = await open_session(wire, "echo:/cwd-4", workingDirectories=[_A])
        seq = await wire.dispatch(chat, {"type": "chat/workingDirectoryRemoved", "directory": 7})
        assert (await wire.echoed(chat, seq))["rejectionReason"] == "directory must be a string"
    finally:
        await shut(host, wire, serving)


async def test_a_folder_the_session_loses_leaves_every_chat_subset() -> None:
    """A subset entry "MUST be present in the owning session's
    `workingDirectories`" -- so it cannot outlive the session's own grant."""
    provider = FollowingProvider()
    host = Host(provider, LoopbackSingleUserPolicy())
    wire, serving = await connect(host)
    try:
        uri = "echo:/cwd-5"
        chat = await open_session(wire, uri, workingDirectories=[_A, _B])
        for directory in (_A, _B):
            await wire.dispatch(chat, {"type": "chat/workingDirectorySet", "directory": directory})
        await wire.until(lambda: state(host, chat).get("workingDirectories") == [_A, _B])

        await wire.dispatch(uri, {"type": "session/workingDirectoryRemoved", "directory": _B})
        await wire.until(lambda: state(host, chat).get("workingDirectories") == [_A])
        assert state(host, chat)["workingDirectories"] == [_A]
        assert provider.sessions[0].chat_directories[-1] == (chat, [_A])
    finally:
        await shut(host, wire, serving)


async def test_a_replaced_folder_is_replaced_in_the_chat_too() -> None:
    """A chat narrowed to the old checkout follows it to the new one, rather
    than silently ending up with no folder at all."""
    provider = FollowingProvider()
    host = Host(provider, LoopbackSingleUserPolicy())
    wire, serving = await connect(host)
    try:
        uri = "echo:/cwd-6"
        chat = await open_session(wire, uri, workingDirectories=[_A, _B])
        await wire.dispatch(chat, {"type": "chat/workingDirectorySet", "directory": _B})
        await wire.until(lambda: state(host, chat).get("workingDirectories") == [_B])

        await wire.dispatch(
            uri,
            {"type": "session/workingDirectoryReplaced", "directory": _B, "replacement": _C},
        )
        await wire.until(lambda: state(host, chat).get("workingDirectories") == [_C])
        assert state(host, uri)["workingDirectories"] == [_A, _C]
        assert state(host, chat)["workingDirectories"] == [_C]
        assert provider.sessions[0].chat_directories[-1] == (chat, [_C])
        assert_frames_valid(wire, ("chat", chat), ("session", uri))
    finally:
        await shut(host, wire, serving)
