"""Notes from the harness in the transcript (`TurnSink.system_notification`)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ahp_host.provider.base import AgentSessionContext, UserMessage
from claude_agent_sdk import AssistantMessage, ResultMessage, ToolUseBlock
from claude_agent_sdk import UserMessage as SdkUserMessage
from claude_agent_sdk._internal.message_parser import parse_message
from claude_agent_sdk.types import ToolResultBlock

from ahp_host_claude.provider import ClaudeProvider
from tests.fakes import FakeClient, FakePublisher, RecordingSink


def _system(subtype: str, **fields: Any) -> Any:
    return parse_message(
        {"type": "system", "subtype": subtype, "uuid": "u", "session_id": "s1", **fields}
    )


def _result() -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="s1",
    )


async def _run(tmp_path: Path, steps: list[Any]) -> RecordingSink:
    provider = ClaudeProvider(tmp_path, client_factory=lambda o: FakeClient(o, [steps]))
    session = await provider.create_session(
        AgentSessionContext(
            session_uri="s", chat_uri="c", provider_id="claude", publisher=FakePublisher()
        )
    )
    sink = RecordingSink()
    await session.send_user_message(UserMessage(text="go"), sink)
    return sink


async def test_a_compaction_is_noted(tmp_path: Path) -> None:
    sink = await _run(
        tmp_path,
        [
            _system("compact_boundary", compact_metadata={"trigger": "auto", "pre_tokens": 180000}),
            _result(),
        ],
    )
    assert sink.notifications == ["Conversation compacted automatically from 180,000 tokens"]
    assert sink.notification_meta[0] == {
        "claudeCode": {"kind": "compaction", "trigger": "auto", "pre_tokens": 180000}
    }


async def test_background_work_ending_mid_turn_is_noted(tmp_path: Path) -> None:
    bash = {"command": "npm run dev", "run_in_background": True, "description": "Dev server"}
    sink = await _run(
        tmp_path,
        [
            AssistantMessage([ToolUseBlock("t1", "Bash", bash)], model="m"),
            _system(
                "task_started",
                task_id="b1",
                task_type="local_bash",
                tool_use_id="t1",
                description="Dev server",
            ),
            SdkUserMessage(content=[ToolResultBlock("t1", "Running in background", False)]),
            _system(
                "task_notification",
                task_id="b1",
                status="failed",
                output_file="/tmp/o",
                summary="exited with code 1",
            ),
            _result(),
        ],
    )
    assert sink.notifications == ["Dev server failed: exited with code 1"]


async def test_a_task_report_injected_mid_turn_is_not_called_a_message_from_a_device(
    tmp_path: Path,
) -> None:
    sink = await _run(
        tmp_path,
        [
            SdkUserMessage(
                content="<task-notification>done</task-notification>",
                uuid="n1",
                origin={"kind": "task-notification"},
            ),
            _result(),
        ],
    )
    assert sink.notifications == [
        "Background task update: <task-notification>done</task-notification>"
    ]
