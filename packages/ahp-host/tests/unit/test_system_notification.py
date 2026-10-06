"""`TurnSink.system_notification`: a note from the harness in the transcript.

Only steering produced a `systemNotification` part, so a provider had no way to
say "the conversation was compacted" or "this message was sent from another
device" -- things both the user and the agent should see -- except as prose the
agent did not write.
"""

from __future__ import annotations

import pytest

from ahp_host.core import Host, LoopbackSingleUserPolicy
from ahp_host.provider import EchoProvider, EchoSession
from ahp_host.provider.base import AgentSessionContext, TurnSink, UserMessage

from .hosting import assert_frames_valid, connect, finished, open_session, shut, state, turn_started

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class Noting(EchoSession):
    async def send_user_message(self, message: UserMessage, sink: TurnSink) -> None:
        await sink.text_delta("Before. ")
        await sink.system_notification("Conversation compacted", meta={"kind": "compaction"})
        await sink.system_notification("Sent from **another device**", markdown=True)
        await sink.text_delta("After.")


class NotingProvider(EchoProvider):
    async def create_session(self, context: AgentSessionContext) -> Noting:
        return Noting(context)


async def test_a_notification_is_its_own_part_in_the_declared_shape() -> None:
    host = Host(NotingProvider(), LoopbackSingleUserPolicy())
    wire, serving = await connect(host)
    try:
        chat = await open_session(wire, "echo:/note-1")
        await wire.dispatch(chat, turn_started("t1"))
        assert await wire.until(lambda: finished(host, chat, "t1"))
        parts = state(host, chat)["turns"][-1]["responseParts"]
        assert [p["kind"] for p in parts] == [
            "markdown",
            "systemNotification",
            "systemNotification",
            "markdown",
        ]
        # `kind`, `content`, `_meta` -- and no invented `id`, which the part
        # does not declare.
        assert parts[1] == {
            "kind": "systemNotification",
            "content": "Conversation compacted",
            "_meta": {"kind": "compaction"},
        }
        # `StringOrMarkdown`: a plain string is rendered as-is, so Markdown
        # is asked for explicitly.
        assert parts[2] == {
            "kind": "systemNotification",
            "content": {"markdown": "Sent from **another device**"},
        }
        # A segment boundary: what follows starts below the note.
        assert parts[0]["content"] == "Before. "
        assert parts[3]["content"] == "After."
        assert_frames_valid(wire, ("chat", chat))
    finally:
        await shut(host, wire, serving)
