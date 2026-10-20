"""The reference agent against a real OpenAI-compatible endpoint; opt-in, never run by default."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from ahp_host.provider.base import UserMessage

from ahp_host_acp.provider import AcpProvider, AgentSpec
from ahp_host_acp.reference import command

from .fakes import RecordingSink, context

pytestmark = pytest.mark.skipif(
    not (os.environ.get("AHP_OPENAI_BASE_URL") and os.environ.get("AHP_OPENAI_MODEL")),
    reason="set AHP_OPENAI_BASE_URL and AHP_OPENAI_MODEL to run against a live endpoint",
)


async def test_refhosted_live_says_something(tmp_path: Path) -> None:
    # No env override: the agent inherits the environment (and AHP_OPENAI_API_KEY if set).
    session = await AcpProvider(tmp_path, AgentSpec(command())).create_session(context(tmp_path))
    sink = RecordingSink()
    try:
        await asyncio.wait_for(
            session.send_user_message(UserMessage(text="Reply with one word: ready"), sink), 180
        )
    finally:
        await session.aclose()
    assert "".join(str(e[1]) for e in sink.events if e[0] == "text").strip()
    assert not [e for e in sink.events if e[0] == "failed"]
