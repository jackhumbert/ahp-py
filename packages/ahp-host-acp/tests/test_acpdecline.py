"""Decline reasons reach the agent on the chat's next prompt; confirmation hints."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from ahp_host.provider.base import UserMessage
from ahp_host.provider.policy import ToolPolicy

from ahp_host_acp import permissions
from ahp_host_acp.agent import provider_from_options
from ahp_host_acp.config import ConfigError
from ahp_host_acp.provider import AcpProvider, AcpSession, AgentSpec
from ahp_host_acp.roots import Roots

from .fakes import FAKE_AGENT, RecordingSink, context

POLICY = ToolPolicy(deny_paths=("**/.env",))
EDIT = {"toolCallId": "e1", "title": "Edit notes", "kind": "edit", "status": "pending"}


async def _session(
    root: Path, policy: ToolPolicy | None = POLICY, *, decline_feedback: bool = True
) -> AcpSession:
    spec = AgentSpec(
        FAKE_AGENT,
        env={"FAKE_ACP_LOG": str(root / "agent.log")},
        policy=policy,
        decline_feedback=decline_feedback,
    )
    return await AcpProvider(root, spec).create_session(context(root))


async def _ask(session: AcpSession, call: dict[str, Any], sink: RecordingSink) -> None:
    await session.send_user_message(UserMessage(text="permission " + json.dumps(call)), sink)


def _prompts(root: Path) -> list[list[dict[str, Any]]]:
    lines = (root / "agent.log").read_text(encoding="utf-8").splitlines()
    entries = [json.loads(line) for line in lines if line.strip()]
    return [e["params"]["prompt"] for e in entries if e["method"] == "session/prompt"]


async def test_acpdecline_reason_reaches_the_next_prompt(tmp_path: Path) -> None:
    session = await _session(tmp_path)
    try:
        await _ask(session, EDIT, RecordingSink(approve=False, reason_message="use the other file"))
        await session.send_user_message(UserMessage(text="hello"), RecordingSink())
        await session.send_user_message(UserMessage(text="again"), RecordingSink())
    finally:
        await session.aclose()
    prompts = _prompts(tmp_path)
    assert prompts[1][0] == {"type": "text", "text": "hello"}
    assert prompts[1][-1] == {"type": "text", "text": "[declined: Edit notes] use the other file"}
    assert all("[declined" not in str(block) for block in prompts[2])


async def test_acpdecline_reason_survives_an_agent_that_exits_on_the_prompt(
    tmp_path: Path,
) -> None:
    session = await _session(tmp_path)
    try:
        await _ask(session, EDIT, RecordingSink(approve=False, reason_message="use the other file"))
        crashed = RecordingSink()
        await session.send_user_message(UserMessage(text="crash"), crashed)
        await session.send_user_message(UserMessage(text="hello"), RecordingSink())
        await session.send_user_message(UserMessage(text="again"), RecordingSink())
    finally:
        await session.aclose()
    line = {"type": "text", "text": "[declined: Edit notes] use the other file"}
    prompts = _prompts(tmp_path)
    crash = next(p for p in prompts if p[0] == {"type": "text", "text": "crash"})
    retry = next(p for p in prompts if p[0] == {"type": "text", "text": "hello"})
    after = next(p for p in prompts if p[0] == {"type": "text", "text": "again"})
    assert crash[-1] == line
    # The agent died on that prompt, so the retry carries the reason again; then it is gone.
    assert retry[-1] == line
    assert all("[declined" not in str(block) for block in after)


async def test_acpdecline_policy_deny_reason_is_sent(tmp_path: Path) -> None:
    session = await _session(tmp_path)
    call = {
        "toolCallId": "p1",
        "title": "Read .env",
        "kind": "read",
        "status": "pending",
        "locations": [{"path": str(tmp_path / ".env")}],
    }
    try:
        await _ask(session, call, RecordingSink())
        await session.send_user_message(UserMessage(text="hello"), RecordingSink())
    finally:
        await session.aclose()
    last = _prompts(tmp_path)[1][-1]
    assert last["text"].startswith("[declined: Read .env] denied by policy")


async def test_acpdecline_off(tmp_path: Path) -> None:
    session = await _session(tmp_path, decline_feedback=False)
    try:
        await _ask(session, EDIT, RecordingSink(approve=False, reason_message="no"))
        await session.send_user_message(UserMessage(text="hello"), RecordingSink())
    finally:
        await session.aclose()
    assert len(_prompts(tmp_path)[1]) == 1


def test_acpdecline_config_key(tmp_path: Path) -> None:
    roots = Roots.single(tmp_path)
    off = provider_from_options({"command": ["x"], "decline_feedback": False}, roots, tmp_path)
    assert off.spec.decline_feedback is False
    assert provider_from_options({"command": ["x"]}, roots, tmp_path).spec.decline_feedback
    with pytest.raises(ConfigError, match="decline_feedback"):
        provider_from_options({"command": ["x"], "decline_feedback": "no"}, roots, tmp_path)


async def test_acpdecline_hints(tmp_path: Path) -> None:
    expected = {
        "read": True,
        "search": True,
        "edit": False,
        "delete": False,
        "move": False,
        "execute": False,
    }
    assert set(permissions.POLICY_KINDS) == set(expected)
    for kind, read_only in expected.items():
        assert permissions.read_only_hint(kind) is read_only
    assert permissions.read_only_hint("fetch") is None
    session = await _session(tmp_path, ToolPolicy(ask_paths=("**/notes.txt",)))
    sink, asked = RecordingSink(), RecordingSink()
    # A located read: the policy asks about a call with no path to check
    # ("policy asks: no path to check"), so a pathless read would be asked too.
    read = {
        "toolCallId": "r1",
        "title": "Read",
        "kind": "read",
        "status": "pending",
        "locations": [{"path": str(tmp_path / "other.txt")}],
    }
    ask = {
        "toolCallId": "r2",
        "title": "Read notes",
        "kind": "read",
        "status": "pending",
        "locations": [{"path": str(tmp_path / "notes.txt")}],
    }
    try:
        await _ask(session, read, sink)
        await _ask(session, ask, asked)
    finally:
        await session.aclose()
    assert sink.confirmations[0].read_only is True
    # The kind is the agent's own claim, and its paths are passed on for a policy upstream.
    assert sink.confirmations[0].read_only_from_agent is True
    assert sink.confirmations[0].locations == (str(tmp_path / "other.txt"),)
    assert sink.confirmations[0].policy_asked is False
    assert asked.confirmations[0].policy_asked is True


def test_acpdecline_line_truncates() -> None:
    line = permissions.decline_line("x", "a" * 400)
    assert len(line) == len("[declined: x] ") + 300
    assert line.endswith("…")
    assert permissions.decline_line("x", "a\n  b") == "[declined: x] a b"
