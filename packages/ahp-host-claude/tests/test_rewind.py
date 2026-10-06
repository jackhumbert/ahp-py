"""Edit-and-resend: the agent forgets the turns the client stopped showing."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ahp_host.provider.base import AgentSessionContext, TruncatesHistory, UserMessage
from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, TextBlock
from claude_agent_sdk import UserMessage as SdkUserMessage

from ahp_host_claude.history import Rewind, TurnMark, plan
from ahp_host_claude.provider import REWOUND_NOTE, ClaudeProvider, LocalClaudeSession, _turn_id_of
from tests.fakes import FakeClient, RecordingSink, text_of


def _marks(*turns: str) -> list[TurnMark]:
    return [TurnMark(turn=t, prompt=f"p-{t}", last=f"l-{t}") for t in turns]


class TestThePlan:
    def test_keeping_a_turn_resumes_at_its_last_entry(self) -> None:
        cut = plan(_marks("a", "b", "c"), "a")
        assert [m.turn for m in cut.keep] == ["a"]
        # Two turns go, so none can be named as the only one dropped.
        assert cut.rewind == Rewind(at="l-a")

    def test_dropping_one_turn_names_it_for_claude_code_to_check(self) -> None:
        assert plan(_marks("a", "b"), "a").rewind == Rewind(at="l-a", drops="p-b")

    def test_a_cut_already_waiting_means_the_transcript_holds_more(self) -> None:
        assert plan(_marks("a", "b"), "a", pending=True).rewind == Rewind(at="l-a")

    def test_keeping_the_last_turn_changes_nothing(self) -> None:
        assert plan(_marks("a", "b"), "b").changes_nothing

    def test_no_turn_or_an_unknown_one_forgets_everything(self) -> None:
        assert plan(_marks("a"), None).forget_all
        # Safest: never keep what the user was shown being taken back.
        assert plan(_marks("a"), "zzz").forget_all

    def test_marks_round_trip(self) -> None:
        mark = TurnMark(turn="a", prompt="p", last="l")
        assert TurnMark.from_wire(mark.to_wire()) == mark
        assert Rewind.from_wire(Rewind(at="x", drops="y").to_wire()) == Rewind(at="x", drops="y")
        assert TurnMark.from_wire({"turn": "a"}) is None


def _result() -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="abc",
    )


def _answer(entry: str) -> list[Any]:
    return [AssistantMessage(content=[TextBlock("ok")], model="m", uuid=entry), _result()]


class Harness:
    def __init__(self, root: Path, *turns: list[Any]) -> None:
        self.turns = list(turns)
        self.clients: list[FakeClient] = []
        self.connect_errors: list[Exception] = []
        self.provider = ClaudeProvider(root, client_factory=self._factory)

    def _factory(self, options: ClaudeAgentOptions) -> FakeClient:
        client = FakeClient(options, self.turns)
        if self.connect_errors:
            client.connect_error = self.connect_errors.pop(0)
        self.clients.append(client)
        return client

    async def session(self, state: dict[str, Any] | None = None) -> LocalClaudeSession:
        context = AgentSessionContext(
            session_uri="s", chat_uri="chat", provider_id="claude", resume_state=state
        )
        if state is not None:
            session = await self.provider.resume_session(context)
        else:
            session = await self.provider.create_session(context)
        assert isinstance(session, LocalClaudeSession)
        return session


async def _two_turns(harness: Harness) -> tuple[LocalClaudeSession, RecordingSink, RecordingSink]:
    session = await harness.session()
    first, second = RecordingSink(turn_id="t1"), RecordingSink(turn_id="t2")
    await session.send_user_message(UserMessage(text="one"), first)
    await session.send_user_message(UserMessage(text="two"), second)
    return session, first, second


async def test_a_local_session_truncates(tmp_path: Path) -> None:
    session = await Harness(tmp_path).session()
    assert isinstance(session, TruncatesHistory)


async def test_the_agent_resumes_at_the_turn_kept(tmp_path: Path) -> None:
    harness = Harness(tmp_path, _answer("a1"), _answer("a2"), _answer("a3"))
    session, _, _ = await _two_turns(harness)
    second_prompt = harness.clients[0].prompts[1][0]["uuid"]

    await session.history_truncated("chat", "t1")
    assert harness.clients[0].disconnected
    # The cut survives a restart until it has been taken.
    state = await harness.provider.resume_state_of(session)
    assert state is not None
    assert state["rewind"] == {"at": "a1", "drops": second_prompt}
    assert [mark["turn"] for mark in state["turns"]] == ["t1"]

    await session.send_user_message(UserMessage(text="two, again"), RecordingSink(turn_id="t2b"))
    options = harness.clients[1].options
    assert options.resume == "abc"
    assert options.resume_session_at == "a1"
    assert options.resume_drops_turn == second_prompt
    assert not options.fork_session  # a branch of the same transcript, uuids and all
    # Claude is told, once, that files were not rewound.
    assert text_of(harness.clients[1].prompts[0]) == f"{REWOUND_NOTE}\n\ntwo, again"
    assert session.rewind is None, "taken: the new branch has its first message"
    assert [mark.turn for mark in session.marks] == ["t1", "t2b"]
    state = await harness.provider.resume_state_of(session)
    assert state is not None
    assert "rewind" not in state
    assert "rewound" not in state


async def test_truncating_everything_starts_a_new_conversation(tmp_path: Path) -> None:
    harness = Harness(tmp_path, _answer("a1"), _answer("a2"), _answer("a3"))
    session, _, _ = await _two_turns(harness)
    await session.history_truncated("chat", None)
    await session.send_user_message(UserMessage(text="fresh"), RecordingSink(turn_id="n1"))
    assert harness.clients[1].options.resume is None
    assert harness.clients[1].options.resume_session_at is None
    assert [mark.turn for mark in session.marks] == ["n1"]


async def test_a_turn_it_does_not_know_forgets_everything(tmp_path: Path) -> None:
    harness = Harness(tmp_path, _answer("a1"), _answer("a2"), _answer("a3"))
    session, _, _ = await _two_turns(harness)
    await session.history_truncated("chat", "from-before-this-existed")
    assert session.claude_session_id is None
    assert session.marks == []


async def test_keeping_the_last_turn_leaves_claude_running(tmp_path: Path) -> None:
    harness = Harness(tmp_path, _answer("a1"), _answer("a2"))
    session, _, _ = await _two_turns(harness)
    await session.history_truncated("chat", "t2")
    assert not harness.clients[0].disconnected


async def test_a_refused_cut_is_taken_without_the_check(tmp_path: Path) -> None:
    harness = Harness(tmp_path, _answer("a1"), _answer("a2"), _answer("a3"))
    session, _, _ = await _two_turns(harness)
    await session.history_truncated("chat", "t1")
    harness.connect_errors = [
        RuntimeError("Resume rejected by --resume-drops-turn: a queued message would go too")
    ]
    await session.send_user_message(UserMessage(text="again"), RecordingSink())
    refused, taken = harness.clients[1], harness.clients[2]
    assert refused.options.resume_drops_turn is not None
    assert taken.options.resume_session_at == "a1"
    assert taken.options.resume_drops_turn is None
    assert taken.connected


async def test_a_cut_at_a_message_claude_code_cannot_find_forgets_it_all(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path, _answer("a1"), _answer("a2"), _answer("a3"))
    session, _, _ = await _two_turns(harness)
    await session.history_truncated("chat", "t1")
    harness.connect_errors = [RuntimeError("No message found with message.uuid of: a1")]
    await session.send_user_message(UserMessage(text="again"), RecordingSink())
    assert harness.clients[2].options.resume is None


async def test_the_cut_and_the_marks_survive_a_host_restart(tmp_path: Path) -> None:
    harness = Harness(tmp_path, _answer("a1"), _answer("a2"), _answer("a3"))
    session, _, _ = await _two_turns(harness)
    await session.history_truncated("chat", "t1")
    state = await harness.provider.resume_state_of(session)
    assert state is not None

    resumed = await harness.session(dict(state))
    assert resumed.rewind == session.rewind
    assert [mark.turn for mark in resumed.marks] == ["t1"]
    await resumed.send_user_message(UserMessage(text="after restart"), RecordingSink())
    assert harness.clients[-1].options.resume_session_at == "a1"
    assert REWOUND_NOTE in str(text_of(harness.clients[-1].prompts[0]))


async def test_a_worker_chat_is_not_rewound(tmp_path: Path) -> None:
    harness = Harness(tmp_path, _answer("a1"), _answer("a2"))
    session, _, _ = await _two_turns(harness)
    await session.history_truncated("ahp-chat:/worker", "t1")
    assert session.rewind is None
    assert len(session.marks) == 2


async def test_the_note_waits_for_a_prompt_that_is_not_a_slash_command(tmp_path: Path) -> None:
    harness = Harness(tmp_path, _answer("a1"), _answer("a2"), _answer("a3"), _answer("a4"))
    session, _, _ = await _two_turns(harness)
    await session.history_truncated("chat", "t1")
    await session.send_user_message(UserMessage(text="/compact"), RecordingSink())
    await session.send_user_message(UserMessage(text="carry on"), RecordingSink())
    prompts = [text_of(p) for p in harness.clients[1].prompts]
    assert prompts == ["/compact", f"{REWOUND_NOTE}\n\ncarry on"]


async def test_a_turn_typed_elsewhere_is_marked_by_its_replayed_uuid(tmp_path: Path) -> None:
    from tests.fakes import FakePublisher, eventually

    publisher = FakePublisher()
    clients: list[FakeClient] = []

    def factory(options: ClaudeAgentOptions) -> FakeClient:
        clients.append(FakeClient(options, []))
        return clients[-1]

    provider = ClaudeProvider(tmp_path, client_factory=factory)
    session = await provider.create_session(
        AgentSessionContext(
            session_uri="s", chat_uri="chat", provider_id="claude", publisher=publisher
        )
    )
    assert isinstance(session, LocalClaudeSession)
    await session.start()
    clients[0].push(
        SdkUserMessage(content="from the phone", uuid="phone-1", origin={"kind": "human"}),
        *_answer("a9"),
    )
    await eventually(lambda: bool(session.marks) and session.marks[0].last == "a9")
    (_text, sink) = publisher.turns[0]
    assert session.marks[0] == TurnMark(turn=sink.turn_id, prompt="phone-1", last="a9")


def test_the_turn_id_is_read_from_the_hosts_own_sink() -> None:
    class HostSink:
        _turn_id = "turn-7"

    assert _turn_id_of(HostSink()) == "turn-7"  # type: ignore[arg-type]
    assert _turn_id_of(RecordingSink(turn_id="t")) == "t"
