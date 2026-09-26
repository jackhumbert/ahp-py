"""Remote Control: sessions also on claude.ai, driven and approved from there."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from ahp_host.provider.base import AgentSessionContext, ConfigRequest, UserMessage
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolPermissionContext,
    ToolResultBlock,
    ToolUseBlock,
)
from claude_agent_sdk import UserMessage as SdkUserMessage

from ahp_host_claude.provider import ClaudeProvider, ClaudeSession, _Turn, discover
from ahp_host_claude.remote_control import auto_enable, bridge_of
from tests.fakes import FakeClient, FakePublisher, RecordingSink, Step, eventually


def _result(**overrides: Any) -> ResultMessage:
    fields: dict[str, Any] = {
        "subtype": "success",
        "duration_ms": 1,
        "duration_api_ms": 1,
        "is_error": False,
        "num_turns": 1,
        "session_id": "claude-1",
    }
    fields.update(overrides)
    return ResultMessage(**fields)


def _from_phone(text: str, uuid: str = "phone-1") -> SdkUserMessage:
    """A message typed on claude.ai, as the CLI replays it."""
    return SdkUserMessage(content=text, uuid=uuid, origin={"kind": "human"})


def _texts(sink: RecordingSink) -> str:
    return "".join(e[1] for e in sink.events if e[0] == "text")


class Harness:
    def __init__(self, root: Path, *turns: list[Step], remote_control: bool = True) -> None:
        self.turns = list(turns)
        self.clients: list[FakeClient] = []
        self.publisher = FakePublisher()
        self.bridge_reply: Any = None
        #: What claude.ai says about a session's status, by id; "active" if unset.
        self.statuses: dict[str, str] = {}
        self.provider = ClaudeProvider(
            root,
            client_factory=self._factory,
            remote_control=remote_control,
            status_on_claude_ai=self._status,
        )

    async def _status(self, session_id: str) -> str | None:
        return self.statuses.get(session_id, "active")

    def _factory(self, options: ClaudeAgentOptions) -> FakeClient:
        client = FakeClient(options, self.turns)
        if self.bridge_reply is not None:
            client.bridge_reply = self.bridge_reply
        self.clients.append(client)
        return client

    def context(self, **config: Any) -> AgentSessionContext:
        return AgentSessionContext(
            session_uri="s",
            chat_uri="c",
            provider_id="claude",
            config=config,
            publisher=self.publisher,
        )

    async def session(self, **config: Any) -> ClaudeSession:
        session = await self.provider.create_session(self.context(**config))
        if session.remote_control:
            await eventually(lambda: session.session_url is not None or bool(self.clients))
            await eventually(lambda: bool(self.clients[-1].remote_controls))
        return session


# -- the default -------------------------------------------------------------


def test_the_default_is_claude_codes_own_verdict() -> None:
    assert auto_enable({"remote_control_auto_enable": True}) is True
    assert auto_enable({"remote_control_auto_enable": False}) is False
    assert auto_enable({}) is False, "an older CLI says nothing: off"
    assert auto_enable(None) is False


async def test_discovery_reads_it_from_start_up(tmp_path: Path) -> None:
    def factory(options: ClaudeAgentOptions) -> FakeClient:
        client = FakeClient(options, [])
        client.server_info = {"remote_control_auto_enable": True, "models": []}
        return client

    assert (await discover(tmp_path, factory)).remote_control is True


def test_a_reply_without_a_session_is_not_a_bridge() -> None:
    assert bridge_of({}) is None
    assert bridge_of({"session_url": "u"}) is None
    bridge = bridge_of({"session_url": "u", "bridge_session_id": "cse_1"})
    assert bridge is not None
    assert bridge.bridge_session_id == "cse_1"


async def test_the_setting_follows_the_default_and_can_be_turned_off(tmp_path: Path) -> None:
    on = ClaudeProvider(tmp_path, remote_control=True)
    resolved = await on.resolve_config(ConfigRequest(provider="claude"))
    assert resolved.properties["remoteControl"]["type"] == "boolean"
    assert resolved.properties["remoteControl"]["default"] is True
    assert resolved.values["remoteControl"] is True
    off = await on.resolve_config(ConfigRequest(provider="claude", values={"remoteControl": False}))
    assert off.values["remoteControl"] is False


# -- turning it on -------------------------------------------------------------


async def test_a_new_session_goes_on_claude_ai_before_its_first_message(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    session = await harness.session()

    await eventually(lambda: session.session_url is not None)
    assert harness.clients[0].remote_controls == [(True, None)]
    assert session.session_url == "https://claude.ai/code/session_1"
    state = await harness.provider.resume_state_of(session)
    assert state is not None
    assert state["bridgeSessionId"] == "cse_1"
    assert state["remoteControl"] is True


async def test_a_new_claude_ai_session_is_saved_straight_away(tmp_path: Path) -> None:
    """The host saves a session when something happens in it; an idle one
    would lose its bridge id at the next restart, and get a new one each time."""
    harness = Harness(tmp_path)
    session = await harness.session()
    await eventually(lambda: session.session_url is not None)
    assert harness.publisher.config_changes == [{"remoteControl": True}]

    resumed = Harness(tmp_path)
    again = await resumed.provider.resume_session(
        AgentSessionContext(
            session_uri="s",
            chat_uri="c",
            provider_id="claude",
            resume_state={"claudeSessionId": "claude-1", "bridgeSessionId": "cse_1"},
            publisher=resumed.publisher,
        )
    )
    await eventually(lambda: again.session_url is not None)
    assert resumed.publisher.config_changes == [], "saved a reattach that changed nothing"


async def test_a_session_without_it_starts_on_its_first_message(tmp_path: Path) -> None:
    harness = Harness(tmp_path, [_result()], remote_control=False)
    session = await harness.session()
    await asyncio.sleep(0.01)
    assert harness.clients == [], "started a Claude client nobody can reach"
    await session.send_user_message(UserMessage(text="hi"), RecordingSink())
    assert harness.clients[0].remote_controls == []


async def test_a_resumed_session_reattaches_to_the_same_claude_ai_session(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    session = await harness.provider.resume_session(
        AgentSessionContext(
            session_uri="s",
            chat_uri="c",
            provider_id="claude",
            resume_state={"claudeSessionId": "claude-1", "bridgeSessionId": "cse_old"},
        )
    )
    await eventually(lambda: session.session_url is not None)
    assert harness.clients[0].remote_controls == [(True, "cse_old")]
    assert harness.clients[0].options.resume == "claude-1"


async def test_a_refused_reattach_starts_a_fresh_one(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.bridge_reply = RuntimeError("gone")
    session = await harness.provider.resume_session(
        AgentSessionContext(
            session_uri="s",
            chat_uri="c",
            provider_id="claude",
            resume_state={"claudeSessionId": "claude-1", "bridgeSessionId": "cse_old"},
        )
    )
    await eventually(lambda: bool(harness.clients) and len(harness.clients[0].remote_controls) == 2)
    assert harness.clients[0].remote_controls == [(True, "cse_old"), (True, None)]
    assert session.session_url is None


async def test_a_refusal_leaves_a_working_local_session(tmp_path: Path) -> None:
    harness = Harness(tmp_path, [AssistantMessage([TextBlock("hello")], model="m"), _result()])
    harness.bridge_reply = RuntimeError("Remote Control is disabled by policy")
    session = await harness.session()
    sink = RecordingSink()
    await session.send_user_message(UserMessage(text="hi"), sink)
    assert _texts(sink) == "hello"
    assert session.session_url is None


async def test_it_can_be_switched_during_a_session(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    session = await harness.session()
    await eventually(lambda: session.session_url is not None)

    await session.config_changed({"remoteControl": False})
    assert harness.clients[0].remote_controls[-1] == (False, None)
    assert session.session_url is None
    await session.config_changed({"remoteControl": True})
    assert harness.clients[0].remote_controls[-1:] == [(True, "cse_1")]
    assert session.session_url is not None


# -- deleting a session ---------------------------------------------------------


async def test_deleting_a_session_archives_it_on_claude_ai(tmp_path: Path) -> None:
    """Kept is fixed while it is on, and a kept session is never archived:
    off, back on unkept (the same session), off."""
    harness = Harness(tmp_path)
    session = await harness.session()
    await eventually(lambda: session.session_url is not None)

    await session.disposed()
    client = harness.clients[0]
    assert client.remote_controls[1:] == [(False, None), (True, "cse_1"), (False, None)]
    assert client.keeps[1:] == [True, False, True]
    assert session.bridge_session_id is None
    state = await harness.provider.resume_state_of(session)
    assert state is None or "bridgeSessionId" not in state


async def test_shutting_down_leaves_it_on_claude_ai(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    session = await harness.session()
    await eventually(lambda: session.session_url is not None)
    await session.aclose()
    assert harness.clients[0].remote_controls == [(True, None)]
    assert harness.clients[0].keeps == [True]


async def test_a_session_turned_off_earlier_is_still_archived_when_deleted(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    session = await harness.session()
    await eventually(lambda: session.session_url is not None)
    await session.config_changed({"remoteControl": False})

    await session.disposed()
    assert harness.clients[0].remote_controls[-2:] == [(True, "cse_1"), (False, None)]
    assert harness.clients[0].keeps[-2] is False


async def test_deleting_a_session_that_never_had_it_starts_nothing(tmp_path: Path) -> None:
    harness = Harness(tmp_path, remote_control=False)
    session = await harness.session()
    await session.disposed()
    assert harness.clients == []


# -- archived on claude.ai ---------------------------------------------------------


async def _resumed(harness: Harness, status: str) -> ClaudeSession:
    harness.statuses["cse_1"] = status
    session = await harness.provider.resume_session(
        AgentSessionContext(
            session_uri="s",
            chat_uri="c",
            provider_id="claude",
            resume_state={"claudeSessionId": "claude-1", "bridgeSessionId": "cse_1"},
            publisher=harness.publisher,
        )
    )
    await asyncio.sleep(0.02)
    return session


async def test_a_restart_leaves_a_session_archived_on_claude_ai_alone(tmp_path: Path) -> None:
    """Reattaching un-archives: every restart used to bring those back."""
    harness = Harness(tmp_path)
    await _resumed(harness, "archived")
    assert harness.clients == [], "started, and so un-archived, a session archived there"


async def test_a_restart_reattaches_one_still_active_there(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    session = await _resumed(harness, "active")
    await eventually(lambda: session.session_url is not None)
    assert harness.clients[0].remote_controls == [(True, "cse_1")]


async def test_a_message_here_brings_one_archived_there_back(tmp_path: Path) -> None:
    harness = Harness(tmp_path, [_result()])
    session = await _resumed(harness, "archived")
    await session.send_user_message(UserMessage(text="hi"), RecordingSink())
    assert harness.clients[0].remote_controls == [(True, "cse_1")]


async def test_unarchiving_here_reattaches_even_though_claude_ai_says_archived(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    session = await harness.session()
    await eventually(lambda: session.session_url is not None)

    async def archived(session_id: str) -> str | None:
        return "archived"

    session._status_on_claude_ai = archived
    await session.archived_changed(True)
    await session.archived_changed(False)
    await eventually(lambda: len(harness.clients) == 2 and session.session_url is not None)


# -- archiving --------------------------------------------------------------


async def test_archiving_files_it_away_on_claude_ai_and_stops_claude(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    session = await harness.session()
    await eventually(lambda: session.session_url is not None)

    await session.archived_changed(True)
    client = harness.clients[0]
    assert client.remote_controls[1:] == [(False, None), (True, "cse_1"), (False, None)]
    assert client.keeps[2] is False
    assert client.disconnected, "an archived session kept its Claude process"
    state = await harness.provider.resume_state_of(session)
    assert state is not None
    assert state["archived"] is True
    assert state["bridgeSessionId"] == "cse_1", "unarchiving needs the same session"


async def test_unarchiving_brings_it_back_on_claude_ai(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    session = await harness.session()
    await eventually(lambda: session.session_url is not None)
    await session.archived_changed(True)

    await session.archived_changed(False)
    await eventually(lambda: len(harness.clients) == 2 and session.session_url is not None)
    assert harness.clients[1].remote_controls == [(True, "cse_1")]
    assert harness.clients[1].keeps == [True]


async def test_an_archived_session_is_not_started_on_restore(tmp_path: Path) -> None:
    """Starting it would reattach, which unarchives it on claude.ai."""
    harness = Harness(tmp_path)
    session = await harness.provider.resume_session(
        AgentSessionContext(
            session_uri="s",
            chat_uri="c",
            provider_id="claude",
            resume_state={"bridgeSessionId": "cse_1", "remoteControl": True, "archived": True},
        )
    )
    await asyncio.sleep(0.02)
    assert harness.clients == []
    await session.config_changed({"remoteControl": True})
    await session.disposed()
    assert harness.clients == [], "touched a session already filed away"


async def test_a_turn_in_an_archived_session_stays_off_claude_ai(tmp_path: Path) -> None:
    harness = Harness(tmp_path, [_result()])
    session = await harness.provider.resume_session(
        AgentSessionContext(
            session_uri="s",
            chat_uri="c",
            provider_id="claude",
            resume_state={
                "claudeSessionId": "claude-1",
                "bridgeSessionId": "cse_1",
                "remoteControl": True,
                "archived": True,
            },
        )
    )
    await session.send_user_message(UserMessage(text="hi"), RecordingSink())
    assert harness.clients[0].remote_controls == []


# -- turns from elsewhere ------------------------------------------------------


async def test_a_message_from_the_phone_opens_a_turn_here(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    await harness.session()
    harness.clients[0].push(
        _from_phone("say hi"),
        AssistantMessage([TextBlock("Hi!")], model="m"),
        _result(),
    )
    await eventually(lambda: bool(harness.publisher.tasks))
    await asyncio.wait_for(harness.publisher.tasks[0], 2)

    [(text, sink)] = harness.publisher.turns
    assert text == "say hi"
    assert _texts(sink) == "Hi!"


async def test_a_phone_turn_after_a_turn_here_waits_for_the_host(tmp_path: Path) -> None:
    """The phone's message lands just as a turn here ends: the host still has
    that turn open for a moment, and refuses a second one until it closes."""
    harness = Harness(tmp_path)
    await harness.session()
    busy = asyncio.create_task(asyncio.sleep(0.1))
    harness.publisher.tasks.append(busy)
    harness.clients[0].push(
        _from_phone("again"), AssistantMessage([TextBlock("ok")], model="m"), _result()
    )
    await eventually(lambda: len(harness.publisher.turns) == 1)
    assert harness.publisher.refusals > 0
    await asyncio.wait_for(harness.publisher.tasks[-1], 2)
    assert _texts(harness.publisher.turns[0][1]) == "ok"


async def test_our_own_messages_are_not_mistaken_for_the_phones(tmp_path: Path) -> None:
    harness = Harness(
        tmp_path,
        [
            SdkUserMessage(content="hi"),
            AssistantMessage([TextBlock("hello")], model="m"),
            _result(),
        ],
    )
    session = await harness.session()
    await session.send_user_message(UserMessage(text="hi"), RecordingSink())
    assert harness.publisher.turns == []


async def test_a_phone_message_during_a_turn_here_is_shown_in_it(tmp_path: Path) -> None:
    harness = Harness(
        tmp_path,
        [
            AssistantMessage([TextBlock("working")], model="m"),
            _from_phone("also do y"),
            AssistantMessage([TextBlock(" and y")], model="m"),
            _result(),
        ],
    )
    session = await harness.session()
    sink = RecordingSink()
    await session.send_user_message(UserMessage(text="do x"), sink)
    assert "also do y" in _texts(sink)
    assert harness.publisher.turns == []


async def test_a_stopped_turn_does_not_end_the_next_one(tmp_path: Path) -> None:
    """Stopping a turn here makes Claude Code answer with an aborted result,
    later. It must not be read as the end of whatever turn comes next."""
    harness = Harness(
        tmp_path,
        [],  # the first turn never finishes on its own
        [
            _result(terminal_reason="aborted_streaming"),
            AssistantMessage([TextBlock("second")], model="m"),
            _result(),
        ],
    )
    session = await harness.session()
    first = asyncio.create_task(session.send_user_message(UserMessage(text="a"), RecordingSink()))
    await eventually(lambda: len(harness.clients[0].prompts) == 1)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    sink = RecordingSink()
    await asyncio.wait_for(session.send_user_message(UserMessage(text="b"), sink), 2)
    assert _texts(sink) == "second"


# -- approvals answered elsewhere --------------------------------------------


def _asked_then_answered_elsewhere(
    result: ToolResultBlock, *, denial_notice: bool = False
) -> list[Step]:
    """Claude Code asks both sides; the phone answers, so it withdraws ours."""
    asked: list[asyncio.Task[Any]] = []

    async def ask(options: ClaudeAgentOptions) -> None:
        can_use_tool = options.can_use_tool
        assert can_use_tool is not None

        async def asking() -> Any:
            return await can_use_tool(
                "Write", {"file_path": "a", "content": "x"}, ToolPermissionContext(tool_use_id="w1")
            )

        asked.append(asyncio.create_task(asking()))
        await asyncio.sleep(0.01)

    async def withdraw(options: ClaudeAgentOptions) -> None:
        asked[0].cancel()  # the SDK, on `control_cancel_request`
        await asyncio.sleep(0.01)

    return [
        AssistantMessage(
            [ToolUseBlock(id="w1", name="Write", input={"file_path": "a", "content": "x"})],
            model="m",
        ),
        ask,
        withdraw,
        *(
            [SystemMessage("permission_denied", {"tool_use_id": "w1", "tool_name": "Write"})]
            if denial_notice
            else []
        ),
        SdkUserMessage(content=[result]),
        _result(),
    ]


async def test_an_approval_on_the_phone_withdraws_the_prompt_here(tmp_path: Path) -> None:
    harness = Harness(
        tmp_path,
        _asked_then_answered_elsewhere(ToolResultBlock(tool_use_id="w1", content="written")),
    )
    session = await harness.session()
    sink = RecordingSink()
    sink.hold = asyncio.Event()
    await session.send_user_message(UserMessage(text="write a"), sink)

    assert ("confirm", "w1") in sink.events
    assert ("confirmed_elsewhere", "w1", True, None) in sink.events
    assert sink.events.index(("confirmed_elsewhere", "w1", True, None)) < next(
        i for i, e in enumerate(sink.events) if e[0] == "completed"
    )


async def test_a_denial_on_the_phone_is_reported_as_one(tmp_path: Path) -> None:
    denied = ToolResultBlock(
        tool_use_id="w1",
        content="The user doesn't want to proceed with this tool use. The tool use was rejected.",
        is_error=True,
    )
    harness = Harness(tmp_path, _asked_then_answered_elsewhere(denied))
    session = await harness.session()
    sink = RecordingSink()
    sink.hold = asyncio.Event()
    await session.send_user_message(UserMessage(text="write a"), sink)

    assert ("confirmed_elsewhere", "w1", False, "Declined on another device") in sink.events


async def test_a_denial_is_recognised_by_claude_codes_notice(tmp_path: Path) -> None:
    """The notice, not the wording: claude.ai's own denial says "Denied by
    user" today, and a denial with the user's feedback says something else."""
    denied = ToolResultBlock(tool_use_id="w1", content="Use b.txt instead", is_error=True)
    harness = Harness(tmp_path, _asked_then_answered_elsewhere(denied, denial_notice=True))
    session = await harness.session()
    sink = RecordingSink()
    sink.hold = asyncio.Event()
    await session.send_user_message(UserMessage(text="write a"), sink)

    reports = [e for e in sink.events if e[0] == "confirmed_elsewhere"]
    assert reports == [("confirmed_elsewhere", "w1", False, "Declined on another device")]


@pytest.mark.parametrize(
    "wording",
    ["The user doesn't want to proceed with this tool use. Rejected.", "Denied by user"],
)
async def test_a_denial_without_the_notice_is_recognised_by_its_wording(
    tmp_path: Path, wording: str
) -> None:
    denied = ToolResultBlock(tool_use_id="w1", content=wording, is_error=True)
    harness = Harness(tmp_path, _asked_then_answered_elsewhere(denied))
    session = await harness.session()
    sink = RecordingSink()
    sink.hold = asyncio.Event()
    await session.send_user_message(UserMessage(text="write a"), sink)
    assert ("confirmed_elsewhere", "w1", False, "Declined on another device") in sink.events


async def test_a_long_running_tool_approved_elsewhere_is_reported_before_it_ends(
    tmp_path: Path,
) -> None:
    """A result that takes a while means the tool is running, i.e. approved."""
    steps = _asked_then_answered_elsewhere(ToolResultBlock(tool_use_id="w1", content="done"))

    async def run_for_a_while(options: ClaudeAgentOptions) -> None:
        await asyncio.sleep(0.8)

    steps.insert(3, run_for_a_while)
    harness = Harness(tmp_path, steps)
    session = await harness.session()
    sink = RecordingSink()
    sink.hold = asyncio.Event()
    turn = asyncio.create_task(session.send_user_message(UserMessage(text="write a"), sink))
    await eventually(lambda: ("confirmed_elsewhere", "w1", True, None) in sink.events, 0.75)
    assert not any(e[0] == "completed" for e in sink.events)
    await asyncio.wait_for(turn, 2)


# -- the approval mode, switched on claude.ai ----------------------------------


def _modes(publisher: FakePublisher) -> list[dict[str, Any]]:
    """The approval-mode changes published, leaving out the saves of a bridge id."""
    return [change for change in publisher.config_changes if "permissionMode" in change]


def _status(mode: str) -> SystemMessage:
    return SystemMessage("status", {"status": None, "permissionMode": mode})


async def test_a_mode_switched_on_the_phone_is_followed_here(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    session = await harness.session()
    harness.clients[0].push(_status("acceptEdits"))
    await eventually(lambda: session.approvals == "acceptEdits")
    assert _modes(harness.publisher) == [{"permissionMode": "acceptEdits"}]


async def test_our_own_switch_echoed_back_is_not_published_again(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    session = await harness.session()
    await session.config_changed({"permissionMode": "auto"})
    harness.clients[0].push(_status("auto"))
    await asyncio.sleep(0.02)
    assert session.approvals == "auto"
    assert _modes(harness.publisher) == []


@pytest.mark.parametrize("mode", ["bypassPermissions", "dontAsk"])
async def test_a_mode_this_adapter_does_not_offer_is_put_back_to_ask(
    tmp_path: Path, mode: str
) -> None:
    harness = Harness(tmp_path)
    session = await harness.session(permissionMode="acceptEdits")
    harness.clients[0].push(_status(mode))
    await eventually(lambda: session.approvals == "default")
    assert harness.clients[0].permission_modes == ["default"]
    assert _modes(harness.publisher) == [{"permissionMode": "default"}]


async def test_a_plan_approved_on_the_phone_drops_to_ask(tmp_path: Path) -> None:
    """In plan mode the gate stays out of the way; Claude Code's default mode
    after the plan would then run whatever the user's allow rules allow."""
    steps = _asked_then_answered_elsewhere(ToolResultBlock(tool_use_id="w1", content="ok"))
    steps[0] = AssistantMessage(
        [ToolUseBlock(id="w1", name="ExitPlanMode", input={"plan": "p"})], model="m"
    )
    harness = Harness(tmp_path, steps)
    session = await harness.session(permissionMode="plan")
    sink = RecordingSink()
    sink.hold = asyncio.Event()
    await session.send_user_message(UserMessage(text="plan it"), sink)

    assert session.approvals == "default"
    assert _modes(harness.publisher) == [{"permissionMode": "default"}]


async def test_a_plan_approved_here_shows_the_mode_it_drops_to(tmp_path: Path) -> None:
    harness = Harness(tmp_path, remote_control=False)
    session = await harness.session(permissionMode="plan")
    session._begin(_Turn(sink=RecordingSink(approve=True)))
    await session._can_use_tool(
        "ExitPlanMode", {"plan": "p"}, ToolPermissionContext(tool_use_id="p1")
    )
    assert session.approvals == "default"
    assert _modes(harness.publisher) == [{"permissionMode": "default"}]


# -- configuration -------------------------------------------------------------


def test_the_config_file_and_flags_can_override_the_default(tmp_path: Path) -> None:
    from ahp_host_claude.__main__ import _parse_args
    from ahp_host_claude.config import ConfigError, load

    config = tmp_path / "node.toml"
    config.write_text(f"root = '{tmp_path}'\n")
    assert load(_parse_args(["--config", str(config)])).remote_control is None
    config.write_text(f"root = '{tmp_path}'\nremote_control = false\n")
    assert load(_parse_args(["--config", str(config)])).remote_control is False
    flagged = load(_parse_args(["--config", str(config), "--remote-control"]))
    assert flagged.remote_control is True
    config.write_text(f"root = '{tmp_path}'\nremote_control = 'on'\n")
    with pytest.raises(ConfigError, match="remote_control"):
        load(_parse_args(["--config", str(config)]))
