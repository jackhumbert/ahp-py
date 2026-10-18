"""A provider can ask for its restored sessions' agents at once (`ResumesOnRestore`),
and a restored session no longer lists changesets whose channels are gone."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from ahp_host.core import Host, LoopbackSingleUserPolicy
from ahp_host.core.changesets import Changeset, FileChange
from ahp_host.core.store import FileSessionStore
from ahp_host.provider import EchoProvider
from ahp_host.provider.base import AgentSession, AgentSessionContext, ResumesOnRestore
from ahp_host.provider.echo import EchoSession
from tests.unit.hosting import connect, open_session, shut, state

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


_EDIT = FileChange(uri="file:///work/main.py", before=b"a\n", after=b"b\n")


class Eager(EchoProvider):
    """Persists `{"eager": ...}` per session and counts `resume_session` calls."""

    def __init__(self) -> None:
        super().__init__()
        self.resumed: list[str] = []
        self.contexts: dict[str, AgentSessionContext] = {}
        self.eager_for: dict[str, bool] = {}

    async def create_session(self, context: AgentSessionContext) -> EchoSession:
        self.contexts[context.session_uri] = context
        return await super().create_session(context)

    async def resume_session(self, context: AgentSessionContext) -> EchoSession:
        self.resumed.append(context.session_uri)
        return await super().resume_session(context)

    async def resume_state_of(self, session: AgentSession) -> Mapping[str, Any] | None:
        uri = session.context.session_uri  # type: ignore[attr-defined]
        return {"eager": self.eager_for.get(uri, False)}

    def resume_on_restore(self, resume_state: Mapping[str, Any]) -> bool:
        return bool(resume_state.get("eager"))


def _host(tmp_path: Path, provider: EchoProvider) -> Host:
    return Host(
        provider,
        LoopbackSingleUserPolicy(),
        store=FileSessionStore(tmp_path / "sessions", debounce=0.05),
        sequence_file=tmp_path / "seq",
    )


async def test_resume_on_restore_resumes_eagerly(tmp_path: Path) -> None:
    first = Eager()
    assert isinstance(first, ResumesOnRestore)
    first.eager_for = {"echo:/eager": True, "echo:/lazy": False}
    host = _host(tmp_path, first)
    wire, serving = await connect(host)
    for uri in first.eager_for:
        await open_session(wire, uri)
    await shut(host, wire, serving)

    second = Eager()
    host = _host(tmp_path, second)
    try:
        assert await host.restore() == 2
        assert second.resumed == ["echo:/eager"]
    finally:
        await host.aclose()


async def test_restore_prunes_unrestored_changesets(tmp_path: Path) -> None:
    provider = Eager()
    host = _host(tmp_path, provider)
    wire, serving = await connect(host)
    uri = "echo:/pruned"
    chat = await open_session(wire, uri)
    publisher = provider.contexts[uri].publisher
    assert publisher is not None
    chat_channel = await publisher.changes_published(
        Changeset(label="chat", uri="ahp-changeset:/chat-one"), [_EDIT], chat=chat
    )
    session_channel = await publisher.changes_published(
        Changeset(label="session", uri="ahp-changeset:/session-one"), [_EDIT]
    )
    assert state(host, chat).get("changesets")
    assert state(host, uri).get("changesets")
    await shut(host, wire, serving)

    restarted = _host(tmp_path, Eager())
    try:
        assert await restarted.restore() == 1
        assert "changesets" not in state(restarted, chat)
        assert "changesets" not in state(restarted, uri)
        for old in (chat_channel, session_channel):
            assert not restarted.sequencer.has_channel(old)
    finally:
        await restarted.aclose()
