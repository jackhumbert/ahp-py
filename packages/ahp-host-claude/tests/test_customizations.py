"""Skills, agents, plugins and MCP servers, as the session's customization tree."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ahp_host.provider.base import (
    AgentSessionContext,
    DescribesSession,
    HandlesCustomizations,
    ManagesMcpServers,
    UserMessage,
)
from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, SystemMessage

from ahp_host_claude import customizations
from ahp_host_claude.customizations import Sources, build
from ahp_host_claude.provider import ClaudeProvider, ClaudeSession
from tests.fakes import FakeClient, FakePublisher, RecordingSink, eventually


def _sources(plugin_path: Path) -> Sources:
    return Sources(
        init={
            "tools": ["Read", "Skill", "Agent"],
            "plugins": [{"name": "tools", "path": str(plugin_path), "version": "1.2.0"}],
        },
        commands=[
            {"name": "deploy", "description": "Ship it"},
            {"name": "tools:lint", "description": "Lint the code"},
            {"name": "review", "description": "Built in", "builtin": True},
        ],
        agents=[{"name": "planner", "description": "Plans things", "model": "opus"}],
        usage={
            "skills": {
                "skillFrontmatter": [
                    {"name": "deploy", "source": "userSettings"},
                    {"name": "house-style", "source": "projectSettings"},
                    {"name": "tools:lint", "source": "plugin", "pluginName": "tools"},
                    {"name": "bundled", "source": "bundled"},
                ]
            },
            "agents": [
                {"agentType": "planner", "source": "userSettings"},
                {"agentType": "Explore", "source": "built-in"},
            ],
        },
        mcp=[
            {"name": "github", "status": "connected", "config": {"url": "https://example.com/mcp"}},
            {"name": "notes", "status": "failed", "error": "spawn ENOENT"},
            {"name": "plugin:tools:search", "status": "pending"},
            {"name": "client", "status": "connected", "source": "sdk"},
        ],
    )


def _by_id(tree: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for entry in tree:
        found[entry["id"]] = entry
        for child in entry.get("children", []):
            found[child["id"]] = child
    return found


class TestTheTree:
    def test_it_has_the_protocols_two_levels(self, tmp_path: Path) -> None:
        config, project = tmp_path / "config", tmp_path / "project"
        (project / ".claude" / "skills" / "house-style").mkdir(parents=True)
        (project / ".claude" / "skills" / "house-style" / "SKILL.md").write_text("x")
        tree = build(
            _sources(tmp_path / "plugin"),
            cwd=project,
            config=config,
            hidden_servers=("client",),
        ).customizations
        assert {entry["type"] for entry in tree} <= {"plugin", "directory", "mcpServer"}
        assert [entry["id"] for entry in tree] == [
            "plugin:tools",
            "dir:user:skill",
            "dir:project:skill",
            "dir:user:agent",
            "mcp:github",
            "mcp:notes",
        ]
        entries = _by_id(tree)
        plugin = entries["plugin:tools"]
        assert plugin["uri"] == (tmp_path / "plugin").as_uri()
        assert plugin["version"] == "1.2.0"
        assert [c["id"] for c in plugin["children"]] == [
            "skill:tools:lint",
            "mcp:plugin:tools:search",
        ]
        assert entries["skill:tools:lint"]["name"] == "lint"
        assert entries["skill:tools:lint"]["description"] == "Lint the code"
        assert entries["mcp:plugin:tools:search"]["state"] == {"kind": "starting"}
        assert entries["dir:user:skill"]["uri"] == (config / "skills").as_uri()
        assert entries["dir:user:skill"]["contents"] == "skill"
        # The skill's own file, where Claude Code's layout puts it.
        assert (
            entries["skill:house-style"]["uri"]
            == (project / ".claude" / "skills" / "house-style" / "SKILL.md").as_uri()
        )
        # Not user-invocable: Claude Code lists no command for it.
        assert entries["skill:house-style"]["disableUserInvocation"] is True
        agent = entries["agent:planner"]
        assert agent["uri"] == (config / "agents" / "planner.md").as_uri()
        assert (agent["description"], agent["model"]) == ("Plans things", "opus")
        assert entries["mcp:github"]["uri"] == "https://example.com/mcp"
        assert entries["mcp:github"]["state"] == {"kind": "ready"}
        assert entries["mcp:notes"]["state"]["error"]["message"] == "spawn ENOENT"
        # Claude Code's own skills and agents are nobody's customization, and
        # the SDK's in-process servers are this host's client tools.
        assert "skill:bundled" not in entries
        assert "agent:Explore" not in entries
        assert "mcp:client" not in entries

    def test_switched_off_entries_stay_off(self, tmp_path: Path) -> None:
        sources = _sources(tmp_path)
        assert sources.mcp is not None
        sources.mcp = [{**sources.mcp[0], "status": "disabled"}, *sources.mcp[1:]]
        entries = _by_id(
            build(
                sources,
                cwd=tmp_path,
                config=tmp_path,
                disabled={"skill:deploy", "plugin:tools"},
                workspace="file:///work",
            ).customizations
        )
        assert entries["skill:deploy"]["enabled"] is False
        assert entries["plugin:tools"]["enablement"] == [{"kind": "session", "enabled": False}]
        assert entries["mcp:github"]["state"] == {"kind": "stopped"}
        assert entries["mcp:github"]["enablement"] == [
            {"kind": "workspace", "uri": "file:///work", "enabled": False}
        ]

    def test_a_session_without_the_tools_lists_no_skills_or_agents(self, tmp_path: Path) -> None:
        sources = _sources(tmp_path)
        sources.init = {"tools": ["WebSearch", "WebFetch"]}
        tree = build(sources, cwd=tmp_path, config=tmp_path).customizations
        assert [entry["type"] for entry in tree] == ["plugin", "mcpServer", "mcpServer"]
        assert [c["type"] for c in tree[0]["children"]] == ["mcpServer"]

    def test_a_plugin_not_yet_seen_in_init_gets_a_urn(self, tmp_path: Path) -> None:
        sources = _sources(tmp_path)
        sources.init = None
        entries = _by_id(build(sources, cwd=tmp_path, config=tmp_path).customizations)
        assert entries["plugin:tools"]["uri"] == "urn:claude-code:plugin:tools"

    def test_needing_a_sign_in_is_an_error_state_not_auth_required(self) -> None:
        state = customizations.mcp_state("needs-auth")
        assert state["kind"] == "error"
        assert state["error"]["errorType"] == "mcp.needsAuth"


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
    def __init__(self, root: Path, *turns: list[Any]) -> None:
        self.root = root
        self.turns = list(turns)
        self.clients: list[FakeClient] = []
        self.publisher = FakePublisher()
        self.provider = ClaudeProvider(root, client_factory=self._factory)
        self.mcp = [
            {"name": "github", "status": "connected"},
            {"name": "notes", "status": "disabled"},
        ]

    def _factory(self, options: ClaudeAgentOptions) -> FakeClient:
        client = FakeClient(options, self.turns)
        client.server_info = {
            "commands": [{"name": "deploy", "description": "Ship it"}],
            "agents": [{"name": "planner", "description": "Plans"}],
        }
        client.context_limits = {
            None: {
                "skills": {"skillFrontmatter": [{"name": "deploy", "source": "userSettings"}]},
                "agents": [{"agentType": "planner", "source": "projectSettings"}],
            }
        }
        client.mcp_servers = self.mcp
        self.clients.append(client)
        return client

    async def session(self) -> ClaudeSession:
        folder = self.root / "work"
        folder.mkdir(exist_ok=True)
        return await self.provider.create_session(
            AgentSessionContext(
                session_uri="s",
                chat_uri="c",
                provider_id="claude",
                working_directories=[folder.as_uri()],
                publisher=self.publisher,
            )
        )


async def _started(harness: Harness) -> ClaudeSession:
    session = await harness.session()
    await session.send_user_message(UserMessage(text="hi"), RecordingSink())
    await eventually(lambda: bool(harness.publisher.trees))
    return session


async def test_the_tree_is_published_once_the_client_has_said(tmp_path: Path) -> None:
    harness = Harness(tmp_path, [_result()])
    session = await harness.session()
    assert isinstance(session, DescribesSession)
    assert (await session.describe()).customizations == []  # nothing known yet
    await session.send_user_message(UserMessage(text="hi"), RecordingSink())
    await eventually(lambda: bool(harness.publisher.trees))
    entries = _by_id(harness.publisher.trees[-1])
    assert {"skill:deploy", "agent:planner", "mcp:github", "mcp:notes"} <= set(entries)
    assert (await session.describe()).customizations == harness.publisher.trees[-1]


async def test_a_servers_state_alone_goes_out_as_its_lifecycle(tmp_path: Path) -> None:
    harness = Harness(tmp_path, [_result()], [_result()])
    session = await _started(harness)
    published = len(harness.publisher.trees)
    harness.mcp[0]["status"] = "failed"
    await session.send_user_message(UserMessage(text="again"), RecordingSink())
    await eventually(lambda: bool(harness.publisher.mcp_states))
    assert harness.publisher.mcp_states[-1][0] == "mcp:github"
    assert harness.publisher.mcp_states[-1][1]["kind"] == "error"
    assert len(harness.publisher.trees) == published


async def test_mcp_servers_start_and_stop_in_claude_code(tmp_path: Path) -> None:
    harness = Harness(tmp_path, [_result()])
    session = await _started(harness)
    assert isinstance(session, ManagesMcpServers)
    await session.start_mcp_server("mcp:notes")  # switched off: switched back on
    await session.start_mcp_server("mcp:github")  # running: reconnected
    await session.stop_mcp_server("mcp:github")
    assert harness.clients[0].mcp_calls == [
        ("toggle", "notes", True),
        ("reconnect", "github", None),
        ("toggle", "github", False),
    ]
    # What is really the case is published over the reducer's guess.
    entries = _by_id(harness.publisher.trees[-1])
    assert entries["mcp:github"]["state"] == {"kind": "stopped"}
    assert entries["mcp:notes"]["state"] == {"kind": "ready"}


async def test_a_skill_switched_off_is_refused_by_the_skill_tool(tmp_path: Path) -> None:
    harness = Harness(tmp_path, [_result()], [_result()])
    session = await _started(harness)
    assert isinstance(session, HandlesCustomizations)
    await session.customization_toggled("skill:deploy", False)
    assert harness.clients[0].disconnected  # restarted on the same conversation
    await session.send_user_message(UserMessage(text="again"), RecordingSink())
    assert "Skill(deploy)" in harness.clients[-1].options.disallowed_tools
    state = await harness.provider.resume_state_of(session)
    assert state is not None
    assert state["disabled"] == ["skill:deploy"]
    # And it stays off in the tree published from then on.
    await eventually(
        lambda: _by_id(harness.publisher.trees[-1])["skill:deploy"].get("enabled") is False
    )


async def test_what_claude_code_cannot_switch_off_is_put_back(tmp_path: Path) -> None:
    harness = Harness(tmp_path, [_result()])
    session = await _started(harness)
    assert isinstance(session, HandlesCustomizations)
    published = len(harness.publisher.trees)
    await session.customization_toggled("agent:planner", False)
    assert len(harness.publisher.trees) == published + 1
    assert "enabled" not in _by_id(harness.publisher.trees[-1])["agent:planner"]
    assert not harness.clients[0].disconnected


async def test_picking_a_custom_agent_starts_claude_code_as_it(tmp_path: Path) -> None:
    harness = Harness(tmp_path, [_result()], [_result()], [_result()])
    session = await _started(harness)
    agent = _by_id(harness.publisher.trees[-1])["agent:planner"]
    await session.send_user_message(
        UserMessage(text="plan it", agent_uri=agent["uri"]), RecordingSink()
    )
    assert harness.clients[-1].options.extra_args["agent"] == "planner"
    assert harness.clients[0].disconnected
    await session.send_user_message(UserMessage(text="no agent"), RecordingSink())
    assert "agent" not in harness.clients[-1].options.extra_args


async def test_a_mode_an_agent_file_set_is_put_back(tmp_path: Path) -> None:
    """Security-relevant: a session never runs looser than its own setting."""
    harness = Harness(
        tmp_path,
        [
            SystemMessage(
                subtype="init", data={"session_id": "abc", "permissionMode": "bypassPermissions"}
            ),
            _result(),
        ],
    )
    await _started(harness)
    assert harness.clients[0].permission_modes == ["default"]


async def test_a_claude_ai_mirror_cannot_be_rewound_or_reconfigured(tmp_path: Path) -> None:
    from ahp_host.provider.base import TruncatesHistory

    from ahp_host_claude.provider import LocalClaudeSession

    mirror = ClaudeSession(
        AgentSessionContext(session_uri="s", chat_uri="c", provider_id="claude"),
        root=tmp_path,
        client_factory=lambda o: FakeClient(o, []),
        mirror_of="cse_1",
    )
    for protocol in (TruncatesHistory, ManagesMcpServers, HandlesCustomizations):
        assert not isinstance(mirror, protocol)
    harness = Harness(tmp_path)
    local = await harness.session()
    assert isinstance(local, LocalClaudeSession)
    for protocol in (TruncatesHistory, ManagesMcpServers, HandlesCustomizations):
        assert isinstance(local, protocol)
