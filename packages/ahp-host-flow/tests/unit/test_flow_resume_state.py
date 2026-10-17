"""The flow provider's resume state: what a restarted host hands back (plan §4.8)."""

from pathlib import Path
from typing import Any

import pytest
from ahp_host.provider.base import (
    AgentSessionContext,
    ResumableAgentProvider,
    ResumesTurns,
    UserMessage,
)

from ahp_host_flow.config import load_config
from ahp_host_flow.engine import JournalWriter, RunHeader
from ahp_host_flow.provider import NOTHING_TO_RESUME, FlowProvider, FlowSession


def _provider(tmp_path: Path) -> FlowProvider:
    toml = tmp_path / "flow.toml"
    state, node = (tmp_path / "state").as_posix(), (tmp_path / "no-node").as_posix()
    # load_config insists on one provider entry; these tests never dispatch to it.
    toml.write_text(
        f'state_dir = "{state}"\nnode = "{node}"\n\n'
        '[providers.echo]\nload = "ahp_host.provider.echo:EchoProvider"\n',
        encoding="utf-8",
    )
    return FlowProvider(load_config(toml), providers={}, user_workflows_dir=tmp_path / "user")


def _context(state: dict[str, Any] | None) -> AgentSessionContext:
    return AgentSessionContext(
        session_uri="flow:/s",
        chat_uri="ahp-chat:/main",
        provider_id="flow",
        resume_state=state,
    )


def _run(run_id: str = "abc", chat: str = "ahp-chat:/main", turn_id: str = "t1") -> dict[str, Any]:
    return {
        "chat": chat,
        "turnId": turn_id,
        "runId": run_id,
        "workflow": "demo",
        "model": None,
        "resumes": 0,
        "phasesStarted": 0,
        "phasesFinished": 0,
    }


async def _resumed(
    tmp_path: Path, state: dict[str, Any] | None
) -> tuple[FlowProvider, FlowSession]:
    provider = _provider(tmp_path)
    return provider, await provider.resume_session(_context(state))


def _journal(tmp_path: Path, filename: str, run_id: str = "abc") -> None:
    JournalWriter.create(
        tmp_path / "state",
        RunHeader(
            run_id=run_id,
            filename=filename,
            source="return 1",
            has_args=False,
            args=None,
            max_agents=1000,
            budget_total=None,
        ),
    ).close()


class _Sink:
    """A bare `TurnSink` stand-in: records how the resumed turn ends."""

    def __init__(self, turn_id: str) -> None:
        self.turn_id = turn_id
        self.failed: list[tuple[str, str, bool]] = []

    async def turn_failed(
        self, message: str, error_type: str = "agent.turn", *_: Any, resumable: bool = False
    ) -> None:
        self.failed.append((message, error_type, resumable))


async def test_flow_resume_state_provider_resumes_turns(tmp_path: Path) -> None:
    provider, session = await _resumed(tmp_path, None)
    assert isinstance(provider, ResumableAgentProvider)
    assert isinstance(session, ResumesTurns)


async def test_flow_resume_state_round_trips(tmp_path: Path) -> None:
    state = {"flow": 1, "runs": [_run()]}
    provider, session = await _resumed(tmp_path, state)
    assert await provider.resume_state_of(session) == state


async def test_flow_resume_state_reads_an_entry_without_the_counts(tmp_path: Path) -> None:
    """The counts are optional: an entry written without them means a turn never resumed."""
    short = {"chat": "ahp-chat:/main", "turnId": "t1", "runId": "abc", "workflow": "demo"}
    provider, session = await _resumed(tmp_path, {"flow": 1, "runs": [short]})
    assert await provider.resume_state_of(session) == {"flow": 1, "runs": [_run()]}


async def test_flow_resume_state_drops_malformed_entries(tmp_path: Path) -> None:
    bad_id = {"flow": 1, "runs": [_run("../x")]}
    no_turn = {"flow": 1, "runs": [{"chat": "ahp-chat:/main", "runId": "abc", "workflow": None}]}
    wrong_version = {"flow": 2, "runs": [_run()]}
    bad_count = {"flow": 1, "runs": [{**_run(), "resumes": -1}]}
    bool_count = {"flow": 1, "runs": [{**_run(), "phasesStarted": True}]}
    for state in (bad_id, no_turn, wrong_version, bad_count, bool_count):
        provider, session = await _resumed(tmp_path, state)
        assert await provider.resume_state_of(session) == {"flow": 1, "runs": []}


@pytest.mark.parametrize(
    ("chat", "turn_id"),
    [("ahp-chat:/worker", "t1"), ("ahp-chat:/main", "t2")],
    ids=["a worker chat", "another turn"],
)
async def test_flow_resume_state_declines_what_is_not_its_run(
    tmp_path: Path, chat: str, turn_id: str
) -> None:
    """A worker chat is never resumed (its run dispatches the call again), nor another turn."""
    _journal(tmp_path, "s.py")
    _, session = await _resumed(tmp_path, {"flow": 1, "runs": [_run()]})
    sink = _Sink(turn_id)
    await session.resume_turn(chat, turn_id, sink)  # type: ignore[arg-type]
    assert sink.failed == [(NOTHING_TO_RESUME, "flow.run", False)]


async def test_flow_resume_state_drops_a_run_whose_journal_is_gone(tmp_path: Path) -> None:
    provider, session = await _resumed(tmp_path, {"flow": 1, "runs": [_run("zzz")]})
    sink = _Sink("t1")
    await session.resume_turn("ahp-chat:/main", "t1", sink)  # type: ignore[arg-type]
    assert [(t, r) for _, t, r in sink.failed] == [("flow.run", False)]
    # Dropped from the state the host persists next, not carried there forever.
    assert await provider.resume_state_of(session) == {"flow": 1, "runs": []}


async def test_flow_resume_state_keeps_the_run_while_it_is_being_continued(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Between leaving `_restored` and `on_run_start` the run must still be saved."""
    _journal(tmp_path, "s.py")
    _, session = await _resumed(tmp_path, {"flow": 1, "runs": [_run()]})
    seen_in_run: list[list[dict[str, Any]]] = []
    handed: list[dict[str, Any]] = []

    async def fake_run_workflow(**kwargs: Any) -> None:
        seen_in_run.append(session.resume_state()["runs"])
        handed.append(kwargs)

    monkeypatch.setattr(session, "_run_workflow", fake_run_workflow)
    await session.resume_turn("ahp-chat:/main", "t1", _Sink("t1"))  # type: ignore[arg-type]
    # Counted as resumed once from the moment it is handed over.
    assert seen_in_run == [[{**_run(), "resumes": 1}]]
    assert handed[0]["resume_from"] == "abc"
    assert handed[0]["resumed"].resumes == 1
    assert session.resume_state()["runs"] == []  # gone once the turn is over


async def test_flow_resume_state_keeps_a_js_run_until_node_is_found(tmp_path: Path) -> None:
    _journal(tmp_path, "s.js")  # no Node on this config: the run cannot go on yet
    provider, session = await _resumed(tmp_path, {"flow": 1, "runs": [_run()]})
    sink = _Sink("t1")
    await session.resume_turn("ahp-chat:/main", "t1", sink)  # type: ignore[arg-type]
    assert [(t, r) for _, t, r in sink.failed] == [("flow.node", True)]
    assert await provider.resume_state_of(session) == {"flow": 1, "runs": [_run()]}


async def test_flow_resume_state_a_new_turn_forgets_the_restored_run(tmp_path: Path) -> None:
    """Once the chat moves on, `chat/turnResume` can never reach the old turn again."""
    provider, session = await _resumed(tmp_path, {"flow": 1, "runs": [_run()]})

    class _Quiet(_Sink):
        async def text_delta(self, text: str) -> None:
            pass

    await session.send_user_message(
        UserMessage(text="/help", chat_uri="ahp-chat:/main"),
        _Quiet("t2"),  # type: ignore[arg-type]
    )
    assert await provider.resume_state_of(session) == {"flow": 1, "runs": []}
