"""The `session/update`s beyond a turn's text and tools, against `fake_agent.py`.

Config options and modes (both ways), titles, slash commands, plans, usage,
file edits, and the MCP servers a session is given.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from ahp_host.provider.base import (
    CompletionRequest,
    ConfigRequest,
    ModelInfo,
    ReconfiguresSessions,
    UserMessage,
)

from ahp_host_acp.mcp import McpServer
from ahp_host_acp.provider import AcpProvider, AcpSession, AgentSpec

from .fakes import FAKE_AGENT, RecordingPublisher, RecordingSink, context


def _provider(
    root: Path,
    log: Path,
    *flags: str,
    catalogue: Path | None = None,
    models: tuple[ModelInfo, ...] = (),
    config_options: Mapping[str, str | bool] | None = None,
    mcp_servers: tuple[McpServer, ...] = (),
) -> AcpProvider:
    env = {"FAKE_ACP_LOG": str(log), **{f"FAKE_ACP_{flag}": "1" for flag in flags}}
    spec = AgentSpec(
        FAKE_AGENT, env=env, config_options=config_options or {}, mcp_servers=mcp_servers
    )
    return AcpProvider(root, spec, models=models, catalogue_file=catalogue)


def _logged(log: Path, method: str) -> list[Any]:
    lines = log.read_text().splitlines() if log.exists() else []
    return [entry["params"] for entry in map(json.loads, lines) if entry["method"] == method]


async def _say(session: AcpSession, text: str, sink: RecordingSink | None = None) -> RecordingSink:
    sink = sink or RecordingSink()
    await session.send_user_message(UserMessage(text=text), sink)
    return sink


async def _whoami(session: AcpSession) -> dict[str, Any]:
    sink = await _say(session, "whoami")
    result: dict[str, Any] = json.loads("".join(e[1] for e in sink.events if e[0] == "text"))
    return result


@pytest.fixture
def log(tmp_path: Path) -> Path:
    return tmp_path / "agent.log"


@pytest.fixture
def catalogue(tmp_path: Path) -> Path:
    return tmp_path / "state" / "agent.json"


@pytest.fixture
async def warmed(tmp_path: Path, log: Path, catalogue: Path) -> Path:
    """A catalogue file that has seen the agent's options once."""
    provider = _provider(tmp_path, log, "CONFIG_OPTIONS", "COMMANDS", catalogue=catalogue)
    session = await provider.create_session(context(tmp_path))
    try:
        await _whoami(session)
    finally:
        await session.aclose()
    log.unlink()
    return catalogue


# -- config options -------------------------------------------------------------


async def test_options_reach_the_schema_once_the_agent_has_reported_them(
    tmp_path: Path, log: Path, catalogue: Path
) -> None:
    provider = _provider(tmp_path, log, "CONFIG_OPTIONS", catalogue=catalogue)
    notified: list[bool] = []

    async def changed() -> bool:
        notified.append(True)
        return True

    await provider.attach_agent_updates(changed)
    assert (await provider.resolve_config(ConfigRequest())).properties == {}
    assert provider.agent.to_wire()["models"] == []
    session = await provider.create_session(context(tmp_path))
    try:
        await _whoami(session)
    finally:
        await session.aclose()
    # The agent's models reach the picker without a restart...
    assert notified
    assert [m["id"] for m in provider.agent.to_wire()["models"]] == ["a", "b"]
    assert provider.default_model is None  # the agent keeps the model it starts with
    # ...and with a picker, the model option is the picker's, not the config's.
    resolved = await provider.resolve_config(ConfigRequest(values={"mode": "plan", "x": 1}))
    assert list(resolved.properties) == ["mode", "yolo"]
    assert resolved.values == {"mode": "plan", "yolo": False}
    # Kept across a restart.
    again = _provider(tmp_path, log, "CONFIG_OPTIONS", catalogue=catalogue)
    assert again.agent.to_wire() == provider.agent.to_wire()
    assert list((await again.resolve_config(ConfigRequest())).properties) == ["mode", "yolo"]


async def test_without_a_picker_the_model_option_is_session_config(
    tmp_path: Path, log: Path
) -> None:
    provider = _provider(tmp_path, log, "CONFIG_OPTIONS")
    provider.catalogue.remember_new_session(
        {
            "sessionId": "s",
            "configOptions": [
                {
                    "id": "model",
                    "name": "Model",
                    "category": "model",
                    "type": "select",
                    "currentValue": "a",
                    "options": [{"value": "a", "name": "A"}],
                }
            ],
        }
    )
    assert "model" in (await provider.resolve_config(ConfigRequest())).properties  # no picker yet


async def test_configured_models_win_over_the_agents(
    tmp_path: Path, log: Path, warmed: Path
) -> None:
    provider = _provider(
        tmp_path, log, "CONFIG_OPTIONS", catalogue=warmed, models=(ModelInfo(id="a", name="A!"),)
    )
    assert [m["name"] for m in provider.agent.to_wire()["models"]] == ["A!"]
    assert provider.default_model == "a"


async def test_a_session_starts_with_the_config_it_was_created_with(
    tmp_path: Path, log: Path, warmed: Path
) -> None:
    provider = _provider(tmp_path, log, "CONFIG_OPTIONS", catalogue=warmed)
    publisher = RecordingPublisher()
    created = context(tmp_path, publisher, config={"mode": "code", "yolo": True})
    session = await provider.create_session(created)
    try:
        who = await _whoami(session)
    finally:
        await session.aclose()
    assert (who["mode"], who["yolo"]) == ("code", True)
    sent = _logged(log, "session/set_config_option")
    assert {
        "sessionId": who["session"],
        "configId": "yolo",
        "type": "boolean",
        "value": True,
    } in sent
    assert publisher.of("config") == []  # the agent agreed: nothing to correct


async def test_client_changes_reach_the_agent_and_refusals_are_undone(
    tmp_path: Path, log: Path, warmed: Path
) -> None:
    provider = _provider(tmp_path, log, "CONFIG_OPTIONS", catalogue=warmed)
    publisher = RecordingPublisher()
    session = await provider.create_session(context(tmp_path, publisher))
    assert isinstance(session, ReconfiguresSessions)
    try:
        await _whoami(session)
        await session.config_changed({"mode": "plan"})
        assert (await _whoami(session))["mode"] == "plan"
        assert publisher.of("config") == []
        # A value the agent refuses: the session's state goes back to the truth.
        await session.config_changed({"mode": "forbidden"})
        assert publisher.of("config") == [{"mode": "plan"}]
        assert (await _whoami(session))["mode"] == "plan"
    finally:
        await session.aclose()


async def test_a_change_before_the_agent_runs_is_applied_when_it_starts(
    tmp_path: Path, log: Path, warmed: Path
) -> None:
    provider = _provider(tmp_path, log, "CONFIG_OPTIONS", catalogue=warmed)
    session = await provider.create_session(context(tmp_path))
    try:
        await session.config_changed({"mode": "code"})
        assert (await _whoami(session))["mode"] == "code"
    finally:
        await session.aclose()


async def test_the_agents_own_change_is_published(tmp_path: Path, log: Path, warmed: Path) -> None:
    provider = _provider(tmp_path, log, "CONFIG_OPTIONS", catalogue=warmed)
    publisher = RecordingPublisher()
    session = await provider.create_session(context(tmp_path, publisher))
    try:
        await _say(session, "selfmode")
        assert publisher.of("config") == [{"mode": "code"}]
        # ...and it sticks: the next turn does not switch it back.
        assert (await _whoami(session))["mode"] == "code"
    finally:
        await session.aclose()


async def test_a_resumed_session_reapplies_its_config(
    tmp_path: Path, log: Path, warmed: Path
) -> None:
    provider = _provider(tmp_path, log, "CONFIG_OPTIONS", catalogue=warmed)
    first = await provider.create_session(context(tmp_path, config={"mode": "plan"}))
    await _whoami(first)
    state = await provider.resume_state_of(first)
    await first.aclose()
    # The host passes the session's config values back from its state.
    again = await provider.resume_session(
        context(tmp_path, resume_state=state, config={"mode": "plan", "yolo": False})
    )
    try:
        who = await _whoami(again)
    finally:
        await again.aclose()
    assert (who["opened"], who["mode"]) == ("session/resume", "plan")


async def test_config_file_options_are_pinned(tmp_path: Path, log: Path, warmed: Path) -> None:
    provider = _provider(
        tmp_path, log, "CONFIG_OPTIONS", catalogue=warmed, config_options={"yolo": "true"}
    )
    yolo = (await provider.resolve_config(ConfigRequest())).properties["yolo"]
    assert (yolo["default"], yolo["readOnly"]) == (True, True)
    # A client cannot unpin it, at creation or after; the state is corrected.
    publisher = RecordingPublisher()
    session = await provider.create_session(context(tmp_path, publisher, config={"yolo": False}))
    try:
        await session.config_changed({"yolo": False})
        assert (await _whoami(session))["yolo"] is True
    finally:
        await session.aclose()
    assert publisher.of("config") == [{"yolo": True}]


async def test_legacy_modes_are_switched_with_set_mode(tmp_path: Path, log: Path) -> None:
    catalogue = tmp_path / "agent.json"
    provider = _provider(tmp_path, log, "MODES", catalogue=catalogue)
    first = await provider.create_session(context(tmp_path))
    await _whoami(first)
    await first.aclose()
    mode = (await provider.resolve_config(ConfigRequest())).properties["mode"]
    assert mode["enum"] == ["ask", "code"]
    publisher = RecordingPublisher()
    session = await provider.create_session(context(tmp_path, publisher, config={"mode": "code"}))
    try:
        assert (await _whoami(session))["mode"] == "code"
        assert [p["modeId"] for p in _logged(log, "session/set_mode")] == ["code"]
        await session.config_changed({"mode": "ask"})
        await _say(session, "selfmode")  # current_mode_update
        assert publisher.of("config") == [{"mode": "code"}]
    finally:
        await session.aclose()


# -- title, commands ---------------------------------------------------------------


async def test_the_agents_title_renames_the_session(tmp_path: Path, log: Path) -> None:
    publisher = RecordingPublisher()
    session = await _provider(tmp_path, log).create_session(context(tmp_path, publisher))
    try:
        await _say(session, "title")
    finally:
        await session.aclose()
    assert publisher.of("title") == ["Fake title"]  # a null title is not a rename


async def test_slash_commands_complete(tmp_path: Path, log: Path, catalogue: Path) -> None:
    provider = _provider(tmp_path, log, "COMMANDS", catalogue=catalogue)
    session = await provider.create_session(context(tmp_path))

    async def complete(chat: str = "ahp-chat:/1") -> list[str]:
        request = CompletionRequest(kind="userMessage", chat=chat, text="/", offset=1)
        return [item.insert_text for item in await provider.complete(request)]

    try:
        assert await complete() == []  # the agent has not said yet
        # Sent in the same write as session/new's answer: still caught.
        await _whoami(session)
        assert session.commands is not None
        assert await complete() == ["/web ", "/help "]
    finally:
        await session.aclose()
    # A session whose agent has not started yet offers what it offered last.
    assert await complete("ahp-chat:/another") == ["/web ", "/help "]
    assert await _provider(tmp_path, log, catalogue=catalogue).complete(
        CompletionRequest(kind="userMessage", chat="x", text="/h", offset=2)
    )


# -- plan, usage ----------------------------------------------------------------------


async def test_each_new_plan_is_one_finished_row(tmp_path: Path, log: Path) -> None:
    publisher = RecordingPublisher()
    session = await _provider(tmp_path, log).create_session(context(tmp_path, publisher))
    try:
        sink = await _say(session, "plan")
    finally:
        await session.aclose()
    started = [e for e in sink.events if e[0] == "started"]
    assert [(e[2], e[3]) for e in started] == [("plan", "Update plan")] * 2  # one resent unchanged
    first, later = (sink.past_tense[e[1]] for e in started)
    assert (first, later) == ("Updated the plan: 0 of 2 done", "Updated the plan: 1 of 2 done")
    completed = [e for e in sink.events if e[0] == "completed"]
    assert completed[-1][3] == {
        "content": [
            {
                "type": "text",
                "text": "- [x] Read the code (high priority)\n- [ ] **Fix the bug** (in progress)",
            }
        ]
    }
    assert publisher.of("activity") == ["Fix the bug"]


async def test_usage_carries_the_context_window_and_cost(
    tmp_path: Path, log: Path, catalogue: Path
) -> None:
    provider = _provider(tmp_path, log, "CONFIG_OPTIONS", catalogue=catalogue)
    session = await provider.create_session(context(tmp_path))
    try:
        sink = await _say(session, "usage")
    finally:
        await session.aclose()
    assert [e for e in sink.events if e[0] == "usage"] == [("usage", 10, None, None, None)]
    assert sink.usage_meta == [
        {"acpUsage": {"used": 10, "size": 200000, "cost": {"amount": 0.25, "currency": "USD"}}}
    ]
    # Remembered as the window of the model the agent was on.
    assert provider.catalogue.context_windows == {"a": 200000}
    (model, *_) = _provider(tmp_path, log, catalogue=catalogue).agent.to_wire()["models"]
    assert (model["id"], model["maxPromptTokens"]) == ("a", 200000)


# -- edits -> changeset -------------------------------------------------------------------


async def test_edits_become_the_sessions_changeset(tmp_path: Path, log: Path) -> None:
    (tmp_path / "notes.txt").write_text("one\n")
    (tmp_path / "code.py").write_text("a = 1\nb = 2\n")
    publisher = RecordingPublisher()
    session = await _provider(tmp_path, log).create_session(context(tmp_path, publisher))
    try:
        for word in ("edit", "fragment", "create", "badedit"):
            sink = await _say(session, word)
    finally:
        await session.aclose()
    # Still summarised in the tool's own result, too.
    assert sink.past_tense["edit_4"] == "Failed: notes.txt"
    assert len(publisher.changesets) == 3  # the failed edit published nothing
    uris = {changeset.uri for changeset, _ in publisher.changesets}
    assert len(uris) == 1  # one changeset, refreshed
    changeset, files = publisher.changesets[-1]
    assert (changeset.change_kind, changeset.reviewable) == ("session", True)
    by_name = {Path(f.uri).name: (f.before, f.after) for f in files}
    assert by_name == {
        "notes.txt": (b"one\n", b"one\nmore\n"),
        "code.py": (b"a = 1\nb = 2\n", b"a = 1\nb = 3\n"),
        "new.txt": (None, b"hello\n"),
    }


# -- MCP servers ----------------------------------------------------------------------------


@pytest.mark.parametrize("http", [False, True])
async def test_configured_mcp_servers_are_given_to_the_agent(
    tmp_path: Path, log: Path, http: bool
) -> None:
    servers = (
        McpServer("local", command=(Path(sys.executable).name, "-m", "server"), env={"K": "v"}),
        McpServer("remote", transport="http", url="https://example.com/mcp", headers={"A": "b"}),
    )
    flags = ("MCP_HTTP",) if http else ()
    session = await _provider(tmp_path, log, *flags, mcp_servers=servers).create_session(
        context(tmp_path)
    )
    try:
        await _whoami(session)
    finally:
        await session.aclose()
    (opened,) = _logged(log, "session/new")
    local = opened["mcpServers"][0]
    assert local["name"] == "local"
    assert Path(local["command"]).is_absolute()  # found on PATH: ACP wants a path
    assert (local["args"], local["env"]) == (["-m", "server"], [{"name": "K", "value": "v"}])
    remote = [s for s in opened["mcpServers"] if s["name"] == "remote"]
    if http:
        assert remote == [
            {
                "type": "http",
                "name": "remote",
                "url": "https://example.com/mcp",
                "headers": [{"name": "A", "value": "b"}],
            }
        ]
    else:
        assert remote == []  # the agent did not say it takes http servers
