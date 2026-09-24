"""Attachments reach Claude as content; the model picker comes from Claude Code."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

from agent_host_server.provider.base import (
    AgentSessionContext,
    ModelInfo,
    ModelSelection,
    UserMessage,
)
from claude_agent_sdk import ClaudeAgentOptions, ResultMessage

from agent_host_server_claude.attachments import MAX_INLINE_TEXT, prompt_content
from agent_host_server_claude.provider import (
    ClaudeProvider,
    discover_models,
    models_from_server_info,
)
from tests.fakes import FakeClient, RecordingSink


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def _result() -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="s",
    )


def test_no_attachments_is_plain_text(tmp_path: Path) -> None:
    assert prompt_content("hi", {"text": "hi"}, tmp_path) == "hi"
    assert prompt_content("hi", {"attachments": []}, tmp_path) == "hi"


def test_a_local_file_is_handed_over_as_its_path_with_the_selection(tmp_path: Path) -> None:
    target = tmp_path / "src" / "app.py"
    raw = {
        "attachments": [
            {
                "type": "resource",
                "label": "app.py",
                "uri": target.as_uri(),
                "selection": {
                    "range": {
                        "start": {"line": 9, "character": 0},
                        "end": {"line": 19, "character": 4},
                    }
                },
            }
        ]
    }
    content = prompt_content("explain", raw, tmp_path)
    assert content == [
        {"type": "text", "text": "explain"},
        {"type": "text", "text": f"[Attached file app.py: {target}, lines 10-20]"},
    ]


def test_a_folder_says_so(tmp_path: Path) -> None:
    raw = {
        "attachments": [
            {
                "type": "resource",
                "label": "src",
                "uri": tmp_path.as_uri(),
                "displayKind": "directory",
            }
        ]
    }
    content = prompt_content("look", raw, tmp_path)
    assert isinstance(content, list)
    assert content[1]["text"] == f"[Attached folder src: {tmp_path.resolve()}]"


def test_a_file_outside_the_root_is_named_but_not_offered(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    raw = {"attachments": [{"type": "resource", "label": "x", "uri": (tmp_path / "x").as_uri()}]}
    content = prompt_content("q", raw, root)
    assert isinstance(content, list)
    assert "outside this host's root; not read" in content[1]["text"]


def test_a_pasted_image_becomes_an_image_block(tmp_path: Path) -> None:
    data = _b64(b"\x89PNG fake")
    raw = {
        "attachments": [
            {
                "type": "embeddedResource",
                "label": "shot.png",
                "contentType": "image/png",
                "data": data,
            }
        ]
    }
    content = prompt_content("what is this", raw, tmp_path)
    assert isinstance(content, list)
    assert content[2] == {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": data},
    }


def test_a_pdf_becomes_a_document_block(tmp_path: Path) -> None:
    raw = {
        "attachments": [
            {
                "type": "embeddedResource",
                "label": "spec.pdf",
                "contentType": "application/pdf",
                "data": _b64(b"%PDF-1.7"),
            }
        ]
    }
    content = prompt_content("summarise", raw, tmp_path)
    assert isinstance(content, list)
    assert content[2]["type"] == "document"


def test_embedded_text_is_inlined_and_capped(tmp_path: Path) -> None:
    long = "x" * (MAX_INLINE_TEXT + 10)
    raw = {
        "attachments": [
            {
                "type": "embeddedResource",
                "label": "log.txt",
                "contentType": "text/plain; charset=utf-8",
                "data": _b64(long.encode()),
            }
        ]
    }
    content = prompt_content("why", raw, tmp_path)
    assert isinstance(content, list)
    text = content[1]["text"]
    assert text.startswith(f"[Attached log.txt (first {MAX_INLINE_TEXT} characters)]\n")
    assert len(text.split("\n", 1)[1]) == MAX_INLINE_TEXT


def test_undecodable_binary_is_not_inlined(tmp_path: Path) -> None:
    raw = {
        "attachments": [
            {
                "type": "embeddedResource",
                "label": "a.bin",
                "contentType": "application/octet-stream",
                "data": _b64(b"\xff\xfe\x00"),
            }
        ]
    }
    content = prompt_content("?", raw, tmp_path)
    assert isinstance(content, list)
    assert content[1]["text"] == "[Attached a.bin (application/octet-stream): binary, not shown]"


def test_simple_attachments_use_their_model_representation(tmp_path: Path) -> None:
    raw = {
        "attachments": [
            {"type": "simple", "label": "sym", "modelRepresentation": "def f(): ..."},
            {"type": "chat", "label": "Side chat", "resource": "ahp-chat:/side"},
            "garbage",
        ]
    }
    content = prompt_content("", raw, tmp_path)
    assert content == [
        {"type": "text", "text": "def f(): ..."},
        {"type": "text", "text": "[Attached Side chat (chat): not supported by this host]"},
    ]


async def test_attachments_are_sent_as_one_streamed_user_message(tmp_path: Path) -> None:
    clients: list[FakeClient] = []

    def factory(options: ClaudeAgentOptions) -> FakeClient:
        clients.append(FakeClient(options, [[_result()]]))
        return clients[-1]

    provider = ClaudeProvider(tmp_path, client_factory=factory)
    session = await provider.create_session(
        AgentSessionContext(session_uri="s", chat_uri="c", provider_id="claude")
    )
    raw: dict[str, Any] = {
        "text": "read this",
        "attachments": [{"type": "resource", "label": "a", "uri": (tmp_path / "a").as_uri()}],
    }
    await session.send_user_message(UserMessage(text="read this", raw=raw), RecordingSink())
    [sent] = clients[0].prompts
    assert sent == [
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {"type": "text", "text": "read this"},
                    {"type": "text", "text": f"[Attached file a: {(tmp_path / 'a').resolve()}]"},
                ],
            },
            "parent_tool_use_id": None,
        }
    ]


SERVER_INFO = {
    "models": [
        {
            "value": "default",
            "resolvedModel": "claude-opus-5-5[1m]",
            "displayName": "Default (recommended)",
            "description": "Opus 5.5 with 1M context",
            "supportedEffortLevels": ["low", "high"],
        },
        {"value": "claude-fable-5-1[1m]", "displayName": "Fable"},
        {"displayName": "no value: skipped"},
    ]
}


def test_models_come_from_claude_codes_own_list() -> None:
    models = models_from_server_info(SERVER_INFO)
    assert [(m.id, m.name) for m in models] == [
        ("default", "Default (recommended)"),
        ("claude-fable-5-1[1m]", "Fable"),
    ]
    assert models[0].meta == {
        "description": "Opus 5.5 with 1M context",
        "resolvedModel": "claude-opus-5-5[1m]",
        "supportedEffortLevels": ["low", "high"],
    }
    assert models_from_server_info(None) == ()


async def test_discovery_probes_claude_code_and_disconnects(tmp_path: Path) -> None:
    probes: list[FakeClient] = []

    def factory(options: ClaudeAgentOptions) -> FakeClient:
        client = FakeClient(options, [])
        client.server_info = SERVER_INFO
        probes.append(client)
        return client

    models = await discover_models(tmp_path, factory)
    assert [m.id for m in models] == ["default", "claude-fable-5-1[1m]"]
    assert probes[0].disconnected


async def test_a_failed_probe_offers_no_picker(tmp_path: Path) -> None:
    class Broken(FakeClient):
        async def connect(self) -> None:
            raise RuntimeError("no CLI")

    assert await discover_models(tmp_path, lambda options: Broken(options, [])) == ()


async def test_picking_default_means_the_account_default(tmp_path: Path) -> None:
    clients: list[FakeClient] = []

    def factory(options: ClaudeAgentOptions) -> FakeClient:
        clients.append(FakeClient(options, [[_result()], [_result()], [_result()]]))
        return clients[-1]

    provider = ClaudeProvider(
        tmp_path, client_factory=factory, models=(ModelInfo(id="default", name="Default"),)
    )
    session = await provider.create_session(
        AgentSessionContext(session_uri="s", chat_uri="c", provider_id="claude", model="default")
    )
    await session.send_user_message(UserMessage(text="x"), RecordingSink())
    assert clients[0].options.model is None  # not the literal string "default"
    await session.send_user_message(
        UserMessage(text="y", model=ModelSelection(id="claude-fable-5-1[1m]")), RecordingSink()
    )
    await session.send_user_message(
        UserMessage(text="z", model=ModelSelection(id="default")), RecordingSink()
    )
    assert clients[0].models == ["claude-fable-5-1[1m]", None]
