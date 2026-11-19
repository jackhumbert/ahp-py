"""`show_image`: Claude puts a picture in the chat as a `contentRef` part."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ahp_host.provider.base import AgentSessionContext, UserMessage
from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ToolUseBlock
from claude_agent_sdk import UserMessage as SdkUserMessage
from claude_agent_sdk.types import ToolResultBlock

from ahp_host_claude import show_image
from ahp_host_claude.permissions import describe, past_tense
from ahp_host_claude.provider import ClaudeProvider, LocalClaudeSession
from tests.fakes import FakeClient, FakePublisher, RecordingSink, Step
from tests.test_file_edits import _result

PNG = b"\x89PNG\r\n\x1a\n" + bytes(32)


def test_only_images_are_shown() -> None:
    assert show_image.content_type(PNG) == "image/png"
    assert show_image.content_type(b"\xff\xd8\xff\xe0rest") == "image/jpeg"
    assert show_image.content_type(b"GIF89a...") == "image/gif"
    assert show_image.content_type(b"RIFF\0\0\0\0WEBPVP8 ") == "image/webp"
    assert show_image.content_type(b"<svg/>") is None
    assert show_image.content_type(b"") is None


def test_the_call_reads_as_showing_a_file() -> None:
    tool_input = {"file_path": "/w/chart.png"}
    assert describe(show_image.NAME, tool_input) == ("Show image", "chart.png")
    assert past_tense(show_image.NAME, tool_input, failed=False) == "Showed chart.png"


class Harness:
    def __init__(self, root: Path, *turns: list[Step]) -> None:
        self.work = root / "work"
        self.work.mkdir(exist_ok=True)
        self.turns = list(turns)
        self.options: ClaudeAgentOptions | None = None
        self.session: LocalClaudeSession | None = None

        def client(options: ClaudeAgentOptions) -> FakeClient:
            self.options = options
            return FakeClient(options, self.turns)

        self.provider = ClaudeProvider(root, client_factory=client)

    async def run(self, folder: bool = True) -> RecordingSink:
        session = await self.provider.create_session(
            AgentSessionContext(
                session_uri="s",
                chat_uri="c",
                provider_id="claude",
                working_directories=[self.work.as_uri()] if folder else [],
                publisher=FakePublisher(),
            )
        )
        assert isinstance(session, LocalClaudeSession)
        self.session = session
        sink = RecordingSink()
        await session.send_user_message(UserMessage(text="show me"), sink)
        return sink

    async def show(self, file_path: str) -> dict[str, Any]:
        assert self.session is not None
        return await self.session._show_image({"file_path": file_path})

    async def hook(self, file_path: str) -> Any:
        assert self.options is not None
        assert self.options.hooks is not None
        hook = self.options.hooks["PreToolUse"][0].hooks[0]
        hook_input: Any = {"tool_name": show_image.NAME, "tool_input": {"file_path": file_path}}
        return await hook(hook_input, "call-1", {"signal": None})


async def test_a_picture_in_the_folder_is_shown_below_its_call(tmp_path: Path) -> None:
    results: dict[str, Any] = {}
    harness: Harness

    async def call(options: ClaudeAgentOptions) -> None:
        assert show_image.SERVER in dict(options.mcp_servers or {})  # type: ignore[arg-type]
        results["allowed"] = await harness.hook("chart.png")
        results["outside"] = await harness.hook(str(tmp_path / "elsewhere.png"))
        results["shown"] = await harness.show("chart.png")
        results["text"] = await harness.show("notes.txt")
        results["missing"] = await harness.show("gone.png")

    harness = Harness(
        tmp_path,
        [
            AssistantMessage(
                [ToolUseBlock("call-1", show_image.NAME, {"file_path": "chart.png"})], model="m"
            ),
            call,
            SdkUserMessage(content=[ToolResultBlock("call-1", "Showed chart.png", False)]),
            _result(),
        ],
    )
    (harness.work / "chart.png").write_bytes(PNG)
    (harness.work / "notes.txt").write_bytes(b"hello")
    sink = await harness.run()

    # Inside the folders it runs without asking; outside, the gate decides.
    assert results["allowed"]["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert results["outside"]["hookSpecificOutput"]["permissionDecision"] == "ask"
    assert results["shown"] == {
        "content": [{"type": "text", "text": "Showed chart.png to the user."}]
    }
    assert sink.content_parts == [(PNG, "image/png")]
    assert results["text"]["is_error"] is True
    assert results["missing"]["is_error"] is True
    # The call's row comes first, then the picture.
    kinds = [event[0] for event in sink.events]
    assert kinds.index("contentRef") > kinds.index("started")


async def test_no_folder_no_tool(tmp_path: Path) -> None:
    harness = Harness(tmp_path, [_result()])
    await harness.run(folder=False)
    assert harness.options is not None
    assert show_image.SERVER not in dict(harness.options.mcp_servers or {})  # type: ignore[arg-type]
