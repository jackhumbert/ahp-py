"""Named chats and in-flight worker turns in the flow resume state (M17.3a)."""

from pathlib import Path
from typing import Any

from ahp_host.provider.base import AgentSessionContext

from ahp_host_flow.config import load_config
from ahp_host_flow.provider import FlowProvider, FlowSession


def _provider(tmp_path: Path) -> FlowProvider:
    toml = tmp_path / "flow.toml"
    state, node = (tmp_path / "state").as_posix(), (tmp_path / "no-node").as_posix()
    toml.write_text(
        f'state_dir = "{state}"\nnode = "{node}"\n\n'
        '[providers.echo]\nload = "tests.flow_fakes:StrippedEchoProvider"\n',
        encoding="utf-8",
    )
    return FlowProvider(load_config(toml), providers={}, user_workflows_dir=tmp_path / "user")


def _run() -> dict[str, Any]:
    return {
        "chat": "ahp-chat:/main",
        "turnId": "t1",
        "runId": "abc",
        "workflow": "demo",
        "model": None,
        "resumes": 0,
        "phasesStarted": 0,
        "phasesFinished": 0,
    }


def _named() -> dict[str, Any]:
    return {
        "provider": "rh",
        "workerChat": "ahp-chat:/w1",
        "title": "p",
        "config": {"effort": "low"},
        "folder": None,
        "innerState": {"memory": ["one"]},
    }


def _inflight() -> dict[str, Any]:
    return {"provider": "rh", "turnId": "w-t1", "innerState": {"memory": ["one"]}}


async def _resumed(tmp_path: Path, state: dict[str, Any]) -> tuple[FlowProvider, FlowSession]:
    provider = _provider(tmp_path)
    context = AgentSessionContext(
        session_uri="flow:/s", chat_uri="ahp-chat:/main", provider_id="flow", resume_state=state
    )
    return provider, await provider.resume_session(context)


async def test_durnamed_state_round_trips(tmp_path: Path) -> None:
    run = {**_run(), "namedChats": {"p": _named()}, "inflight": {"ahp-chat:/w2": _inflight()}}
    state = {"flow": 1, "runs": [run]}
    provider, session = await _resumed(tmp_path, state)
    assert await provider.resume_state_of(session) == state


async def test_durnamed_state_drops_malformed(tmp_path: Path) -> None:
    run = {
        **_run(),
        "namedChats": {
            "good": _named(),
            "bad-title": {**_named(), "title": 5},
            "bad-config": {**_named(), "config": []},
            "bad-state": {**_named(), "innerState": "x"},
            "not-a-record": 7,
        },
        "inflight": {
            "ahp-chat:/ok": _inflight(),
            "ahp-chat:/bad": {**_inflight(), "turnId": 1},
            "ahp-chat:/none": {"provider": "rh", "turnId": "x", "innerState": None},
        },
    }
    provider, session = await _resumed(tmp_path, {"flow": 1, "runs": [run]})
    written = await provider.resume_state_of(session)
    assert written == {
        "flow": 1,
        "runs": [
            {**_run(), "namedChats": {"good": _named()}, "inflight": {"ahp-chat:/ok": _inflight()}}
        ],
    }


async def test_durnamed_state_absent_keys_unchanged(tmp_path: Path) -> None:
    state = {"flow": 1, "runs": [_run()]}
    provider, session = await _resumed(tmp_path, state)
    written = await provider.resume_state_of(session)
    assert written == state
    assert written is not None
    (entry,) = written["runs"]
    assert "namedChats" not in entry
    assert "inflight" not in entry
