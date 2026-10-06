"""Attachments and attached chats as ACP prompt blocks, and session-level forks."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pytest
from ahp_host.provider.base import AgentSessionContext, AttachedChat, ForkedFrom, UserMessage

from ahp_host_acp.prompts import MAX_EMBED, PromptBuilder
from ahp_host_acp.provider import AcpProvider, AcpSession, AgentSpec
from ahp_host_acp.roots import Roots

from .fakes import FAKE_AGENT, RecordingSink, context

RICH = {"image": True, "audio": True, "embeddedContext": True}
TURN = {
    "id": "t1",
    "message": {"text": "what is 2+2?"},
    "responseParts": [{"kind": "markdown", "id": "p", "content": "4"}],
}


def _message(*attachments: dict[str, Any], chats: tuple[AttachedChat, ...] = ()) -> UserMessage:
    return UserMessage(
        text="look", raw={"text": "look", "attachments": list(attachments)}, attached_chats=chats
    )


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def test_without_capabilities_files_are_links_and_the_rest_is_left_out(tmp_path: Path) -> None:
    (tmp_path / "notes.md").write_text("hello\n")
    builder = PromptBuilder(Roots.single(tmp_path), {})
    blocks = builder.blocks(
        _message(
            {
                "type": "resource",
                "label": "notes.md",
                "uri": (tmp_path / "notes.md").as_uri(),
                "selection": {"range": {"start": {"line": 2}, "end": {"line": 4}}},
            },
            {"type": "resource", "label": "docs", "uri": "https://example.com/docs"},
            {"type": "resource", "label": "secret", "uri": "file:///etc/passwd"},
            {
                "type": "embeddedResource",
                "label": "shot",
                "data": _b64(b"x"),
                "contentType": "image/png",
            },
            {"type": "simple", "label": "/web", "_meta": {"acpCommand": "web"}},
            {"type": "simple", "label": "note", "modelRepresentation": "remember this"},
            {"type": "annotations", "label": "notes", "resource": "ahp-session:/1/annotations"},
        ),
    )
    assert blocks == [
        {"type": "text", "text": "look"},
        {"type": "resource_link", "uri": (tmp_path / "notes.md").as_uri(), "name": "notes.md"},
        {"type": "text", "text": "(In notes.md, the user selected lines 3-5.)"},
        {"type": "resource_link", "uri": "https://example.com/docs", "name": "docs"},
        {"type": "text", "text": "remember this"},
    ]


def test_with_capabilities_content_is_embedded(tmp_path: Path) -> None:
    (tmp_path / "notes.md").write_text("hello\n")
    (tmp_path / "big.txt").write_bytes(b"x" * (MAX_EMBED + 1))
    (tmp_path / "bin.dat").write_bytes(b"\xff\xfe\x00")
    builder = PromptBuilder(Roots.named({"work": tmp_path}), RICH)
    blocks = builder.blocks(
        _message(
            # The folder-tree URI a client sends, read here: the agent could not.
            {"type": "resource", "label": "notes.md", "uri": "file:///work/notes.md"},
            {"type": "resource", "label": "big", "uri": "file:///work/big.txt"},
            {"type": "resource", "label": "bin", "uri": "file:///work/bin.dat"},
            {
                "type": "embeddedResource",
                "label": "shot",
                "data": _b64(b"x"),
                "contentType": "image/png",
            },
            {
                "type": "embeddedResource",
                "label": "a.txt",
                "data": _b64(b"hi"),
                "contentType": "text/plain",
            },
            {
                "type": "embeddedResource",
                "label": "a.pdf",
                "data": _b64(b"\xff"),
                "contentType": "application/pdf",
            },
        )
    )
    notes, big, binary, image, plain, pdf = blocks[1:]
    assert notes == {
        "type": "resource",
        "resource": {"uri": (tmp_path / "notes.md").as_uri(), "text": "hello\n"},
    }
    assert (big["type"], big["size"]) == ("resource_link", MAX_EMBED + 1)
    assert binary["type"] == "resource_link"
    assert image == {"type": "image", "data": _b64(b"x"), "mimeType": "image/png"}
    assert plain["resource"] == {"uri": "attachment:a.txt", "mimeType": "text/plain", "text": "hi"}
    assert pdf["resource"]["blob"] == _b64(b"\xff")


@pytest.mark.parametrize("embedded", [True, False])
def test_attached_chats_are_context(tmp_path: Path, embedded: bool) -> None:
    builder = PromptBuilder(Roots.single(tmp_path), RICH if embedded else {})
    chat = AttachedChat(resource="ahp-chat:/other", end_turn="t1", label="Maths", turns=(TURN,))
    (_, block) = builder.blocks(_message(chats=(chat,)))
    body = "**User:** what is 2+2?\n**Agent:** 4"
    if embedded:
        assert block == {
            "type": "resource",
            "resource": {
                "uri": "ahp-chat:/other",
                "mimeType": "text/markdown",
                "text": f"# Maths, through turn t1\n\n{body}",
            },
        }
    else:
        assert block == {
            "type": "text",
            "text": f"Context -- Maths, through turn t1 (ahp-chat:/other):\n\n{body}",
        }


async def test_the_agent_receives_them(tmp_path: Path) -> None:
    log = tmp_path / "agent.log"
    (tmp_path / "notes.md").write_text("hello\n")
    spec = AgentSpec(FAKE_AGENT, env={"FAKE_ACP_LOG": str(log), "FAKE_ACP_PROMPT_CAPS": "1"})
    session = await AcpProvider(tmp_path, spec).create_session(context(tmp_path))
    message = _message(
        {"type": "resource", "label": "notes.md", "uri": (tmp_path / "notes.md").as_uri()},
        chats=(AttachedChat(resource="ahp-chat:/other", turns=(TURN,)),),
    )
    try:
        await session.send_user_message(message, RecordingSink())
    finally:
        await session.aclose()
    (sent,) = [
        json.loads(line)["params"]
        for line in log.read_text().splitlines()
        if json.loads(line)["method"] == "session/prompt"
    ]
    assert [b["type"] for b in sent["prompt"]] == ["text", "resource", "resource"]


# -- createSession.fork ------------------------------------------------------------------


def _fork_context(root: Path, source: AcpSession, turns: int) -> AgentSessionContext:
    """A second session, forked from *source*'s default chat through *turns* turns."""
    fork = ForkedFrom(
        session_uri=source.context.session_uri,
        turns=tuple(dict(TURN, id=f"t{n}") for n in range(turns)),
        chat_uri=source.context.chat_uri,
        turn_id=f"t{turns - 1}",
    )
    return AgentSessionContext(
        session_uri="ahp-session:/2",
        chat_uri="ahp-chat:/2",
        provider_id="acp",
        working_directories=(root.as_uri(),),
        fork=fork,
    )


async def _who(session: AcpSession, sink: RecordingSink) -> dict[str, Any]:
    await session.send_user_message(UserMessage(text="whoami"), sink)
    result: dict[str, Any] = json.loads("".join(e[1] for e in sink.events if e[0] == "text"))
    return result


@pytest.mark.parametrize(("fork", "turns"), [(True, 1), (False, 1), (True, 2)])
async def test_a_forked_session_continues_the_conversation(
    tmp_path: Path, fork: bool, turns: int
) -> None:
    log = tmp_path / "agent.log"
    env = {"FAKE_ACP_LOG": str(log), "FAKE_ACP_STORE": str(tmp_path / "store.json")}
    if fork:
        env["FAKE_ACP_FORK"] = "1"
    provider = AcpProvider(tmp_path, AgentSpec(FAKE_AGENT, env=env))
    source = await provider.create_session(context(tmp_path))
    try:
        await source.send_user_message(UserMessage(text="hello"), RecordingSink())
        # A fork at the latest of one turn; or, with turns=2, at a turn the
        # source's agent session is not at.
        forked = await provider.create_session(_fork_context(tmp_path, source, turns))
        sink = RecordingSink()
        try:
            who = await _who(forked, sink)
        finally:
            await forked.aclose()
    finally:
        await source.aclose()
    notices = [e[1] for e in sink.events if e[0] == "notice"]
    if fork and turns == 1:
        # The agent's own fork, reopened in the new session's process.
        assert (who["opened"], who["history"], notices) == ("session/resume", ["hello"], [])
    else:
        # A fresh agent session, told the transcript -- and the user told so.
        assert who["opened"] == "session/new"
        assert len(notices) == 1
        (prompt,) = [
            json.loads(line)["params"]["prompt"]
            for line in log.read_text().splitlines()
            if json.loads(line)["method"] == "session/prompt"
            and json.loads(line)["params"]["sessionId"] == who["session"]
        ]
        assert prompt[0] == {"type": "text", "text": "whoami"}
        assert "**User:** what is 2+2?" in prompt[1]["text"]
