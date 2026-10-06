"""`/` commands and `@` files as the user types (`completions`)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ahp_host.provider.base import AgentSessionContext, Completes, CompletionRequest, UserMessage
from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, SystemMessage

from ahp_host_claude.attachments import attachment_blocks
from ahp_host_claude.completions import TRIGGERS
from ahp_host_claude.provider import ClaudeProvider
from ahp_host_claude.roots import Roots
from tests.fakes import FakeClient, RecordingSink, eventually

COMMANDS = [
    {"name": "review", "description": "Review a change", "argumentHint": "<pr>"},
    {"name": "release-notes", "description": "Draft notes"},
    {"name": "tools:lint", "description": "Lint"},
    {"name": "exit", "description": "Quit", "builtin": True},
]


def _result() -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="abc",
    )


class Harness:
    def __init__(self, root: Path, *turns: list[Any], commands: Any = ()) -> None:
        self.root = root
        self.clients: list[FakeClient] = []
        self.turns = list(turns)
        self.provider = ClaudeProvider(root, client_factory=self._factory, commands=commands)

    def _factory(self, options: ClaudeAgentOptions) -> FakeClient:
        client = FakeClient(options, self.turns)
        client.server_info = {"commands": COMMANDS}
        client.suggestions = [{"path": "src/"}, {"path": "src/app.py"}, {"path": "../../outside"}]
        client.suggestion_cwd = str(self.root / "work")
        self.clients.append(client)
        return client

    async def session(self) -> Any:
        work = self.root / "work"
        work.mkdir(exist_ok=True)
        return await self.provider.create_session(
            AgentSessionContext(
                session_uri="s",
                chat_uri="chat",
                provider_id="claude",
                working_directories=[work.as_uri()],
            )
        )


def _request(text: str, offset: int | None = None) -> CompletionRequest:
    return CompletionRequest(
        kind="userMessage", chat="chat", text=text, offset=len(text) if offset is None else offset
    )


def test_the_provider_completes_and_names_its_triggers() -> None:
    assert isinstance(ClaudeProvider(Path("/tmp")), Completes)
    assert ClaudeProvider.completion_trigger_characters == TRIGGERS == ("/", "@")


async def test_slash_completes_claude_codes_commands(tmp_path: Path) -> None:
    harness = Harness(tmp_path, [_result()])
    session = await harness.session()
    await session.send_user_message(UserMessage(text="hi"), RecordingSink())
    await eventually(lambda: session._sources.commands == COMMANDS)

    items = await harness.provider.complete(_request("/re"))
    assert [item.insert_text for item in items] == ["/release-notes ", "/review "]
    review = items[1].to_wire()
    assert (review["rangeStart"], review["rangeEnd"]) == (0, 3)
    assert review["attachment"]["type"] == "simple"
    assert review["attachment"]["label"] == "/review"
    assert review["attachment"]["_meta"] == {
        "claudeCode": {
            "command": "review",
            "description": "Review a change",
            "argumentHint": "<pr>",
        }
    }
    # A plugin's command matches by its own name too.
    assert [i.insert_text for i in await harness.provider.complete(_request("/lin"))] == [
        "/tools:lint "
    ]


async def test_slash_only_at_the_start_and_never_a_terminal_command(tmp_path: Path) -> None:
    harness = Harness(
        tmp_path,
        [SystemMessage(subtype="init", data={"terminal_slash_commands": ["exit"]}), _result()],
    )
    session = await harness.session()
    await session.send_user_message(UserMessage(text="hi"), RecordingSink())
    assert await harness.provider.complete(_request("see /re")) == []
    assert await harness.provider.complete(_request("/ex")) == []
    # Whitespace before it is still the start of the message.
    assert len(await harness.provider.complete(_request("  /rev"))) == 1


async def test_before_the_client_starts_the_start_up_list_is_used(tmp_path: Path) -> None:
    harness = Harness(tmp_path, commands=[{"name": "review"}])
    session = await harness.session()  # held, as the host holds it
    items = await harness.provider.complete(_request("/r"))
    assert [item.insert_text for item in items] == ["/review "]
    # And the client is started, so the next keystroke has the session's own.
    await eventually(lambda: bool(harness.clients) and harness.clients[0].connected)
    await session.aclose()


async def test_at_completes_files_inside_the_served_folders(tmp_path: Path) -> None:
    harness = Harness(tmp_path, [_result()])
    session = await harness.session()
    await session.send_user_message(UserMessage(text="hi"), RecordingSink())

    items = await harness.provider.complete(_request("look at @sr"))
    assert harness.clients[0].suggestion_queries[-1] == "sr"
    wires = [item.to_wire() for item in items]
    assert [w["insertText"] for w in wires] == ["@src ", "@src/app.py "]
    assert wires[1]["attachment"] == {
        "type": "resource",
        "uri": (tmp_path / "work" / "src" / "app.py").resolve().as_uri(),
        "label": "app.py",
        "displayKind": "document",
    }
    assert wires[0]["attachment"]["displayKind"] == "directory"
    assert wires[0]["attachment"]["label"] == "src/"
    assert (wires[0]["rangeStart"], wires[0]["rangeEnd"]) == (8, 11)
    # `../../outside` is outside every served folder: not offered.


async def test_offsets_are_utf16(tmp_path: Path) -> None:
    harness = Harness(tmp_path, [_result()])
    session = await harness.session()
    await session.send_user_message(UserMessage(text="hi"), RecordingSink())
    text = "\N{GRINNING FACE} @src"
    items = await harness.provider.complete(_request(text, offset=7))  # the emoji is 2 units
    assert items
    assert items[0].range_start == 3
    assert items[0].range_end == 7


async def test_an_accepted_command_adds_nothing_to_the_prompt(tmp_path: Path) -> None:
    attachment = {
        "type": "simple",
        "label": "/review",
        "_meta": {"claudeCode": {"command": "review"}},
    }
    assert attachment_blocks([attachment], Roots.single(tmp_path)) == []


async def test_an_unknown_chat_gets_nothing(tmp_path: Path) -> None:
    provider = ClaudeProvider(tmp_path)
    request = CompletionRequest(kind="userMessage", chat="nobody", text="/x", offset=2)
    assert await provider.complete(request) == ()


async def test_the_unwrapped_control_requests_have_claude_codes_shape() -> None:
    """`file_suggestions` and `get_context_usage` (summary), which the SDK does not wrap."""
    from ahp_host_claude.remote_control import RemoteControlClient

    sent: list[dict[str, Any]] = []

    class Query:
        async def _send_control_request(self, request: dict[str, Any]) -> dict[str, Any]:
            sent.append(request)
            return {"suggestions": []}

    client = RemoteControlClient(options=ClaudeAgentOptions())
    client._query = Query()
    assert await client.file_suggestions("src/a") == {"suggestions": []}
    await client.context_usage()
    assert sent == [
        {"subtype": "file_suggestions", "query": "src/a"},
        {"subtype": "get_context_usage", "detail": "summary"},
    ]
