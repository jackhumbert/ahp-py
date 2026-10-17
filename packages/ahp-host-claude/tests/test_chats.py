"""A conversation per chat: new chats, forks, side chats, and what is the session's."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from ahp_host.core import AhpError
from ahp_host.provider.base import (
    AgentSessionContext,
    CancelsChats,
    ChatContext,
    ConfigRequest,
    FollowsChatWorkingDirectories,
    ForkedFrom,
    HostsChats,
    ReconfiguresChats,
    UserMessage,
)
from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, TextBlock
from claude_agent_sdk import UserMessage as SdkUserMessage

from ahp_host_claude.provider import ClaudeProvider, ClaudeSession, LocalClaudeSession
from tests.fakes import FakeClient, FakePublisher, RecordingSink, eventually, text_of

DEFAULT = "ahp-chat:/default"


def _result(session_id: str = "abc") -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id=session_id,
    )


def _answer(entry: str, session_id: str = "abc") -> list[Any]:
    return [AssistantMessage([TextBlock("ok")], model="m", uuid=entry), _result(session_id)]


class Harness:
    def __init__(self, root: Path, *turns: list[Any]) -> None:
        self.root = root
        self.turns = list(turns)
        self.clients: list[FakeClient] = []
        self.publisher = FakePublisher()
        self.provider = ClaudeProvider(root, client_factory=self._factory)
        self.work = root / "work"
        self.work.mkdir(exist_ok=True)

    def _factory(self, options: ClaudeAgentOptions) -> FakeClient:
        client = FakeClient(options, self.turns)
        self.clients.append(client)
        return client

    def context(self, **fields: Any) -> AgentSessionContext:
        base: dict[str, Any] = {
            "session_uri": "s",
            "chat_uri": DEFAULT,
            "provider_id": "claude",
            "working_directories": [self.work.as_uri()],
            "publisher": self.publisher,
        }
        return AgentSessionContext(**{**base, **fields})

    async def session(self, **fields: Any) -> LocalClaudeSession:
        session = await self.provider.create_session(self.context(**fields))
        assert isinstance(session, LocalClaudeSession)
        return session


async def _two_turns(harness: Harness) -> LocalClaudeSession:
    session = await harness.session()
    await session.send_user_message(
        UserMessage(text="one", chat_uri=DEFAULT), RecordingSink(turn_id="t1")
    )
    await session.send_user_message(
        UserMessage(text="two", chat_uri=DEFAULT), RecordingSink(turn_id="t2")
    )
    return session


def test_the_agent_advertises_chats_with_forks_and_side_chats(tmp_path: Path) -> None:
    capabilities = ClaudeProvider(tmp_path).agent.capabilities
    assert capabilities["multipleChats"] == {"fork": True, "sideChat": True}


async def test_a_local_session_hosts_chats(tmp_path: Path) -> None:
    session = await Harness(tmp_path).session()
    for protocol in (HostsChats, CancelsChats, FollowsChatWorkingDirectories):
        assert isinstance(session, protocol)


async def test_each_chat_is_its_own_conversation(tmp_path: Path) -> None:
    harness = Harness(tmp_path, _answer("a1"), _answer("b1", "other"))
    session = await harness.session()
    await session.send_user_message(UserMessage(text="hi", chat_uri=DEFAULT), RecordingSink())
    await session.chat_opened(ChatContext(session_uri="s", chat_uri="ahp-chat:/second"))
    await session.send_user_message(
        UserMessage(text="hello there", chat_uri="ahp-chat:/second"), RecordingSink()
    )
    default, second = harness.clients
    assert [text_of(p) for p in default.prompts] == ["hi"]
    assert [text_of(p) for p in second.prompts] == ["hello there"]
    assert second.options.resume is None  # a conversation of its own
    assert second.options.cwd == default.options.cwd  # in the session's folder
    assert session.claude_session_id == "abc"
    assert session.chats["ahp-chat:/second"].claude_session_id == "other"


async def test_a_fork_copies_the_source_conversation_at_its_turn(tmp_path: Path) -> None:
    harness = Harness(tmp_path, _answer("a1"), _answer("a2"), _answer("f1", "forked"))
    session = await _two_turns(harness)
    fork = ForkedFrom(session_uri="s", chat_uri=DEFAULT, turn_id="t1", turns=[{"id": "t1"}])
    await session.chat_opened(
        ChatContext(session_uri="s", chat_uri="ahp-chat:/fork", origin={"kind": "fork"}, fork=fork)
    )
    child = session.chats["ahp-chat:/fork"]
    # Its copied turn keeps the source's id, so it can be rewound in turn.
    assert [m.turn for m in child.marks] == ["t1"]
    await session.send_user_message(
        UserMessage(text="instead", chat_uri="ahp-chat:/fork"), RecordingSink(turn_id="t3")
    )
    options = harness.clients[-1].options
    assert options.resume == "abc"
    assert options.resume_session_at == "a1"
    assert options.fork_session is True  # the source stays as its chat has it
    assert child.claude_session_id == "forked"
    assert child.rewind is None


async def test_a_side_chat_has_the_turn_as_context_and_none_of_its_history(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path, _answer("a1"), _answer("a2"), _answer("s1", "side"))
    session = await _two_turns(harness)
    side = ForkedFrom(session_uri="s", chat_uri=DEFAULT, turn_id="t2")
    await session.chat_opened(
        ChatContext(session_uri="s", chat_uri="ahp-chat:/side", side_chat=side)
    )
    child = session.chats["ahp-chat:/side"]
    assert child.marks == []
    assert child.rewind is not None
    assert child.rewind.at == "a2"
    assert child.rewind.fork


async def test_a_source_it_cannot_cut_is_given_as_its_transcript(tmp_path: Path) -> None:
    harness = Harness(tmp_path, _answer("a1"), _answer("a2"), _answer("f1", "fresh"))
    session = await _two_turns(harness)
    turns = [{"id": "old", "message": {"text": "from before"}, "responseParts": []}]
    fork = ForkedFrom(session_uri="s", chat_uri=DEFAULT, turn_id="old", turns=turns)
    await session.chat_opened(ChatContext(session_uri="s", chat_uri="ahp-chat:/f", fork=fork))
    await session.send_user_message(
        UserMessage(text="go on", chat_uri="ahp-chat:/f"), RecordingSink()
    )
    options = harness.clients[-1].options
    assert options.resume is None
    prompt = text_of(harness.clients[-1].prompts[0])
    assert "User: from before" in prompt
    assert prompt.endswith("go on")


async def test_closing_a_chat_stops_its_claude(tmp_path: Path) -> None:
    harness = Harness(tmp_path, _answer("a1"), _answer("b1"))
    session = await harness.session()
    await session.chat_opened(ChatContext(session_uri="s", chat_uri="ahp-chat:/x"))
    await session.send_user_message(UserMessage(text="x", chat_uri="ahp-chat:/x"), RecordingSink())
    await session.chat_closed("ahp-chat:/x")
    assert harness.clients[0].disconnected
    assert "ahp-chat:/x" not in session.chats


async def test_stopping_one_chat_leaves_the_others_running(tmp_path: Path) -> None:
    harness = Harness(tmp_path, _answer("a1"), _answer("b1"))
    session = await harness.session()
    await session.send_user_message(UserMessage(text="a", chat_uri=DEFAULT), RecordingSink())
    await session.chat_opened(ChatContext(session_uri="s", chat_uri="ahp-chat:/x"))
    await session.send_user_message(UserMessage(text="b", chat_uri="ahp-chat:/x"), RecordingSink())
    await session.cancel_chat("ahp-chat:/x")
    default, other = harness.clients
    assert other.interrupted
    assert not default.interrupted


async def test_chats_survive_a_restart(tmp_path: Path) -> None:
    harness = Harness(tmp_path, _answer("b1", "other"), _answer("b2", "other"))
    session = await harness.session()
    await session.chat_opened(ChatContext(session_uri="s", chat_uri="ahp-chat:/x"))
    await session.send_user_message(
        UserMessage(text="b", chat_uri="ahp-chat:/x"), RecordingSink(turn_id="x1")
    )
    state = await harness.provider.resume_state_of(session)
    assert state is not None
    assert state["chats"]["ahp-chat:/x"]["claudeSessionId"] == "other"

    resumed = await harness.provider.resume_session(harness.context(resume_state=dict(state)))
    assert isinstance(resumed, LocalClaudeSession)
    await resumed.chat_opened(ChatContext(session_uri="s", chat_uri="ahp-chat:/x", restored=True))
    child = resumed.chats["ahp-chat:/x"]
    assert child.claude_session_id == "other"
    assert [m.turn for m in child.marks] == ["x1"]
    await resumed.send_user_message(UserMessage(text="c", chat_uri="ahp-chat:/x"), RecordingSink())
    assert harness.clients[-1].options.resume == "other"


async def test_the_approval_mode_is_the_sessions(tmp_path: Path) -> None:
    harness = Harness(tmp_path, _answer("a1"))
    session = await harness.session()
    await session.chat_opened(ChatContext(session_uri="s", chat_uri="ahp-chat:/x"))
    await session.config_changed({"permissionMode": "acceptEdits"})
    assert session.chats["ahp-chat:/x"].approvals == "acceptEdits"


# -- a chat's own approval mode and effort (AHP 1.1.0) ----------------------------


def _mode_changes(harness: Harness) -> list[tuple[Any, str | None]]:
    """Each approval mode the adapter published, and the chat it named."""
    publisher = harness.publisher
    return [
        (values["permissionMode"], chat)
        for values, chat in zip(publisher.config_changes, publisher.config_chats, strict=True)
        if "permissionMode" in values
    ]


async def test_both_settings_may_be_set_per_chat(tmp_path: Path) -> None:
    """`chatMutable`, and the hook that honours it: marking one without the
    other would have the state say one thing and Claude do another."""
    harness = Harness(tmp_path)
    resolved = await harness.provider.resolve_config(ConfigRequest(provider="claude", values={}))
    assert resolved.properties["permissionMode"]["chatMutable"] is True
    assert resolved.properties["effort"]["chatMutable"] is True
    assert "chatMutable" not in resolved.properties["remoteControl"]
    assert isinstance(await harness.session(), ReconfiguresChats)


async def test_a_chat_can_approve_more_without_loosening_the_others(tmp_path: Path) -> None:
    """Security-relevant: the reason per-chat config exists upstream."""
    harness = Harness(tmp_path, _answer("b1", "x"))
    session = await harness.session()
    await session.chat_opened(ChatContext(session_uri="s", chat_uri="ahp-chat:/x"))
    await session.chat_opened(ChatContext(session_uri="s", chat_uri="ahp-chat:/y"))
    await session.send_user_message(UserMessage(text="b", chat_uri="ahp-chat:/x"), RecordingSink())

    await session.chat_config_changed("ahp-chat:/x", {"permissionMode": "acceptEdits"})

    x, y = session.chats["ahp-chat:/x"], session.chats["ahp-chat:/y"]
    assert x.approvals == "acceptEdits"
    assert harness.clients[0].permission_modes == ["acceptEdits"]
    assert session.approvals == "default"
    assert y.approvals == "default"


async def test_the_sessions_mode_passes_over_a_chat_with_its_own(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    session = await harness.session()
    await session.chat_opened(ChatContext(session_uri="s", chat_uri="ahp-chat:/x"))
    await session.chat_opened(ChatContext(session_uri="s", chat_uri="ahp-chat:/y"))
    await session.chat_config_changed("ahp-chat:/x", {"permissionMode": "acceptEdits"})

    await session.config_changed({"permissionMode": "plan"})

    assert session.approvals == "plan"
    assert session.chats["ahp-chat:/y"].approvals == "plan"
    assert session.chats["ahp-chat:/x"].approvals == "acceptEdits"


async def test_an_overlay_without_the_key_puts_the_chat_back_on_the_session(
    tmp_path: Path,
) -> None:
    """ "To reset a property to the session's value, replace the overlay with
    one that omits the property" -- the host hands over the whole overlay."""
    harness = Harness(tmp_path)
    session = await harness.session(config={"permissionMode": "acceptEdits"})
    await session.chat_opened(ChatContext(session_uri="s", chat_uri="ahp-chat:/x"))
    await session.chat_config_changed("ahp-chat:/x", {"permissionMode": "plan"})
    assert session.chats["ahp-chat:/x"].approvals == "plan"

    await session.chat_config_changed("ahp-chat:/x", {})

    assert session.chats["ahp-chat:/x"].approvals == "acceptEdits"


async def test_the_default_chat_keeps_its_own_apart_from_the_sessions(tmp_path: Path) -> None:
    """The default chat's conversation is the session object itself, so the
    session's own value is held apart: a chat opened afterwards, and the
    saved state, get the session's -- not the default chat's."""
    harness = Harness(tmp_path, _answer("a1"))
    session = await harness.session()
    await session.send_user_message(UserMessage(text="a", chat_uri=DEFAULT), RecordingSink())

    await session.chat_config_changed(DEFAULT, {"permissionMode": "acceptEdits"})
    await session.chat_opened(ChatContext(session_uri="s", chat_uri="ahp-chat:/x"))

    assert session.approvals == "acceptEdits"
    assert session.session_approvals == "default"
    assert session.chats["ahp-chat:/x"].approvals == "default"
    state = await harness.provider.resume_state_of(session)
    assert state is not None
    assert state["permissionMode"] == "default"


async def test_a_resumed_session_gets_each_chats_own_back_from_the_host(tmp_path: Path) -> None:
    """Not saved by the adapter: the overlay is chat state the host keeps,
    and the host tells a resumed agent each one, as `chat_config_changed`."""
    harness = Harness(tmp_path, _answer("a1"))
    session = await harness.session()
    await session.send_user_message(UserMessage(text="a", chat_uri=DEFAULT), RecordingSink())
    await session.chat_config_changed(DEFAULT, {"permissionMode": "acceptEdits"})
    state = await harness.provider.resume_state_of(session)
    assert state is not None

    resumed = await harness.provider.resume_session(harness.context(resume_state=dict(state)))
    assert isinstance(resumed, LocalClaudeSession)
    assert resumed.approvals == "default"
    await resumed.chat_config_changed(DEFAULT, {"permissionMode": "acceptEdits"})
    assert resumed.approvals == "acceptEdits"
    assert resumed.session_approvals == "default"


async def test_a_chats_own_effort_restarts_only_that_chat(tmp_path: Path) -> None:
    harness = Harness(tmp_path, _answer("a1"), _answer("b1", "x"), _answer("b2", "x"))
    session = await harness.session()
    await session.send_user_message(UserMessage(text="a", chat_uri=DEFAULT), RecordingSink())
    await session.chat_opened(ChatContext(session_uri="s", chat_uri="ahp-chat:/x"))
    await session.send_user_message(UserMessage(text="b", chat_uri="ahp-chat:/x"), RecordingSink())
    default, other = harness.clients

    await session.chat_config_changed("ahp-chat:/x", {"effort": "high"})

    assert other.disconnected
    assert not default.disconnected
    await session.send_user_message(UserMessage(text="c", chat_uri="ahp-chat:/x"), RecordingSink())
    assert harness.clients[-1].options.effort == "high"
    assert session.effort == "default"


async def test_leaving_plan_mode_in_a_chat_with_its_own_mode_stays_in_that_chat(
    tmp_path: Path,
) -> None:
    """Security-relevant. Claude Code moves its own mode (a plan approved); the
    gate follows, and only where the mode was set: that chat's overlay, told
    to clients on that chat, never the session's or another chat's."""
    harness = Harness(tmp_path)
    session = await harness.session()
    await session.chat_opened(ChatContext(session_uri="s", chat_uri="ahp-chat:/x"))
    await session.chat_config_changed("ahp-chat:/x", {"permissionMode": "plan"})
    x = session.chats["ahp-chat:/x"]

    await x._left_plan_mode()

    assert x.approvals == "default"
    assert _mode_changes(harness) == [("default", "ahp-chat:/x")]
    assert session.session_approvals == "default"
    await session.config_changed({"permissionMode": "acceptEdits"})
    assert x.approvals == "default", "the chat's own mode stopped being its own"


async def test_a_mode_moved_in_a_chat_without_its_own_is_the_sessions(tmp_path: Path) -> None:
    """And every running Claude follows -- the default chat's included, whose
    gate used to move while its Claude Code stayed in plan mode."""
    harness = Harness(tmp_path, _answer("a1"))
    session = await harness.session(config={"permissionMode": "plan"})
    await session.send_user_message(UserMessage(text="a", chat_uri=DEFAULT), RecordingSink())
    await session.chat_opened(ChatContext(session_uri="s", chat_uri="ahp-chat:/x"))
    await session.chat_opened(ChatContext(session_uri="s", chat_uri="ahp-chat:/y"))
    await session.chat_config_changed("ahp-chat:/y", {"permissionMode": "plan"})

    await session.chats["ahp-chat:/x"]._left_plan_mode()

    assert _mode_changes(harness) == [("default", None)]
    assert session.session_approvals == "default"
    assert session.approvals == "default"
    assert harness.clients[0].permission_modes[-1:] == ["default"]
    assert session.chats["ahp-chat:/y"].approvals == "plan"


async def test_our_own_switch_echoed_in_a_narrowed_chat_is_not_a_change(tmp_path: Path) -> None:
    """A chat narrowed away from the session's folder runs Claude Code in Ask
    while its mode stays looser, and Claude Code reports that Ask back.
    Followed, it rewrote the mode clients see -- the session's, for every
    chat. Only our own switch is passed over: one made after it is followed."""
    other = tmp_path / "other"
    other.mkdir()
    harness = Harness(tmp_path, _answer("a1"), _answer("a2"))
    session = await harness.session(
        working_directories=[harness.work.as_uri(), other.as_uri()],
        config={"permissionMode": "acceptEdits"},
    )
    # The first turn fixes Claude Code's `cwd`, which narrowing then leaves
    # out; narrowing restarts Claude, on the next turn.
    await session.send_user_message(UserMessage(text="a", chat_uri=DEFAULT), RecordingSink())
    await session.chat_working_directories_changed(DEFAULT, [other.as_uri()])
    await session.send_user_message(UserMessage(text="b", chat_uri=DEFAULT), RecordingSink())
    await session.config_changed({"permissionMode": "auto"})
    assert harness.clients[-1].permission_modes[-1:] == ["default"]

    await session._on_mode_elsewhere("default")

    assert session.approvals == "auto"
    assert _mode_changes(harness) == []

    # Not ours this time: a switch made on claude.ai.
    await session._on_mode_elsewhere("default")
    assert session.approvals == "default"
    assert _mode_changes(harness) == [("default", None)]


async def test_a_chat_narrowed_away_from_the_sessions_folder_asks_for_everything(
    tmp_path: Path,
) -> None:
    """Security-relevant: Claude Code's looser modes act in its `cwd` unasked."""
    other = tmp_path / "other"
    other.mkdir()
    harness = Harness(tmp_path, _answer("a1"), _answer("a2"), _answer("a3"))
    work = harness.work.as_uri()
    session = await harness.session(
        working_directories=[work, other.as_uri()], config={"permissionMode": "acceptEdits"}
    )
    await session.send_user_message(UserMessage(text="a", chat_uri=DEFAULT), RecordingSink())
    assert harness.clients[0].options.permission_mode == "acceptEdits"
    await session.chat_working_directories_changed(DEFAULT, [other.as_uri()])
    await session.send_user_message(UserMessage(text="b", chat_uri=DEFAULT), RecordingSink())
    assert harness.clients[1].options.permission_mode == "default"
    # And one narrowed to nothing has no tools that touch the machine.
    await session.chat_working_directories_changed(DEFAULT, [])
    await session.send_user_message(UserMessage(text="c", chat_uri=DEFAULT), RecordingSink())
    assert harness.clients[2].options.tools == ["WebSearch", "WebFetch"]


async def test_truncating_a_chat_cuts_that_chats_conversation(tmp_path: Path) -> None:
    harness = Harness(tmp_path, _answer("b1", "x"), _answer("b2", "x"))
    session = await harness.session()
    await session.chat_opened(ChatContext(session_uri="s", chat_uri="ahp-chat:/x"))
    for turn in ("x1", "x2"):
        await session.send_user_message(
            UserMessage(text=turn, chat_uri="ahp-chat:/x"), RecordingSink(turn_id=turn)
        )
    await session.history_truncated("ahp-chat:/x", "x1")
    child = session.chats["ahp-chat:/x"]
    assert child.rewind is not None
    assert child.rewind.at == "b1"
    assert session.rewind is None


async def test_a_claude_ai_session_refuses_a_second_chat(tmp_path: Path) -> None:
    mirror = ClaudeSession(
        AgentSessionContext(session_uri="s", chat_uri=DEFAULT, provider_id="claude"),
        root=tmp_path,
        client_factory=lambda o: FakeClient(o, []),
        mirror_of="cse_1",
    )
    with pytest.raises(AhpError) as refused:
        await mirror.chat_opened(ChatContext(session_uri="s", chat_uri="ahp-chat:/x"))
    assert refused.value.code == -32009  # PermissionDenied


async def test_a_forked_session_starts_where_its_copied_turns_end(tmp_path: Path) -> None:
    harness = Harness(tmp_path, _answer("a1"), _answer("a2"), _answer("n1", "new"))
    await _two_turns(harness)
    fork = ForkedFrom(session_uri="s", chat_uri=DEFAULT, turn_id="t1")
    forked = await harness.provider.create_session(
        harness.context(session_uri="s2", chat_uri="ahp-chat:/s2", fork=fork)
    )
    assert isinstance(forked, LocalClaudeSession)
    assert [m.turn for m in forked.marks] == ["t1"]
    await forked.send_user_message(UserMessage(text="then"), RecordingSink())
    options = harness.clients[-1].options
    assert (options.resume, options.resume_session_at, options.fork_session) == ("abc", "a1", True)


async def test_a_forked_session_without_the_sources_folder_is_seeded_instead(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path, _answer("a1"), _answer("a2"), _answer("n1", "new"))
    await _two_turns(harness)
    turns = [{"id": "t1", "message": {"text": "one"}, "responseParts": []}]
    fork = ForkedFrom(session_uri="s", chat_uri=DEFAULT, turn_id="t1", turns=turns)
    forked = await harness.provider.create_session(
        harness.context(
            session_uri="s2", chat_uri="ahp-chat:/s2", fork=fork, working_directories=[]
        )
    )
    await forked.send_user_message(UserMessage(text="then"), RecordingSink())
    assert harness.clients[-1].options.resume is None
    assert "User: one" in text_of(harness.clients[-1].prompts[0])


async def test_completions_answer_from_the_chats_own_session(tmp_path: Path) -> None:
    from ahp_host.provider.base import CompletionRequest

    harness = Harness(tmp_path)
    session = await harness.session()
    await session.chat_opened(ChatContext(session_uri="s", chat_uri="ahp-chat:/x"))
    session.chats["ahp-chat:/x"]._sources.commands = [{"name": "review"}]
    items = await harness.provider.complete(
        CompletionRequest(kind="userMessage", chat="ahp-chat:/x", text="/re", offset=3)
    )
    assert [item.insert_text for item in items] == ["/review "]


async def test_a_message_injected_into_a_chat_opens_a_turn_there(tmp_path: Path) -> None:
    """A background task reporting back into the chat that started it.

    Claude Code injects the task's notification into that chat's own
    conversation once its turn has ended. It used to be dropped outside the
    default chat, because `external_turn` could only open a turn there.
    """
    harness = Harness(tmp_path, _answer("a1"))
    session = await harness.session()
    await session.chat_opened(ChatContext(session_uri="s", chat_uri="ahp-chat:/x"))
    await session.send_user_message(UserMessage(text="x", chat_uri="ahp-chat:/x"), RecordingSink())
    harness.clients[0].push(
        SdkUserMessage(content="task finished", uuid="n1", origin={"kind": "task-notification"}),
        *_answer("a2"),
    )
    await eventually(lambda: bool(harness.publisher.turns))
    assert harness.publisher.external_chats == ["ahp-chat:/x"]
    assert harness.publisher.turns[0][0] == "task finished"
