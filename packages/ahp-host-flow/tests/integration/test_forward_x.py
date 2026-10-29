"""`[workflows] forward_x`: chosen `opts.x` keys reach the worker message `_meta`. No Node."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from ahp_host_flow.dispatch import FORWARDED_X_META_KEY, ProviderDispatcher
from ahp_host_flow.engine import AgentCall, call_key, load_journal
from tests.flow_fakes import RecordingProvider, reset_fakes
from tests.flow_hosting import boot_flow
from tests.hosting import start_turn, wait_turn

BASE = '[providers.rec]\nload = "tests.flow_fakes:RecordingProvider"\n'


def script(x: str) -> str:
    return (
        "meta = {'name': 'fx', 'description': 'd'}\n"
        "\n"
        "async def main(args):\n"
        f"    return await agent('a', x={x})\n"
    )


async def run(tmp_path: Path, toml: str, x: str) -> tuple[list[dict[str, object]], Path]:
    """The `_meta` of every message the worker received, and the state dir."""
    reset_fakes()
    (tmp_path / "user").mkdir(parents=True, exist_ok=True)
    (tmp_path / "user" / "fx.py").write_text(script(x), encoding="utf-8")
    fx = await boot_flow(tmp_path, str(tmp_path / "no-node"), toml)
    try:
        await start_turn(fx.wire, fx.chat, "t1", "/fx")
        turn = await wait_turn(fx.wire, fx.chat, "t1")
        assert turn["state"] == "complete", turn
        metas = [
            dict(m.raw.get("_meta") or {})
            for s in RecordingProvider.instances[0].sessions
            for m in s.received
        ]
    finally:
        await fx.shut()
    return metas, tmp_path / "state"


FORWARD = BASE + '\n[workflows]\nforward_x = ["tier", "gone"]\n'


async def test_forward_x_listed_key_reaches_the_worker_meta(tmp_path: Path) -> None:
    metas, _ = await run(tmp_path, FORWARD, "{'tier': {'n': 2}, 'other': 1}")
    assert metas == [{FORWARDED_X_META_KEY: {"tier": {"n": 2}}}]


async def test_forward_x_default_forwards_nothing(tmp_path: Path) -> None:
    metas, _ = await run(tmp_path, BASE, "{'tier': 1}")
    assert metas == [{}]


async def test_forward_x_unset_key_adds_no_meta(tmp_path: Path) -> None:
    metas, _ = await run(tmp_path, FORWARD, "{'other': 1}")
    assert metas == [{}]


async def test_forward_x_script_nan_arrives_as_null(tmp_path: Path) -> None:
    # The sandbox's JSON bridge turns NaN into null before the host sees it.
    metas, _ = await run(tmp_path, FORWARD, "{'tier': float('nan')}")
    assert metas == [{FORWARDED_X_META_KEY: {"tier": None}}]


def test_forward_x_non_json_value_is_skipped_and_logged() -> None:
    """A value that is not JSON (a library caller's) is skipped with a log line, not forwarded."""
    notes: list[str] = []
    stub = SimpleNamespace(
        config=SimpleNamespace(workflows=SimpleNamespace(forward_x=("a", "b"))),
        _note=notes.append,
    )
    call = AgentCall(3, "p", opts={"x": {"a": {1, 2}, "b": float("inf")}})
    assert ProviderDispatcher._forwarded_x(stub, call) == {}  # type: ignore[arg-type]
    assert len(notes) == 2
    assert "agent() #3: opts.x.a is not JSON" in notes[0]


async def test_forward_x_value_is_part_of_the_replay_key(tmp_path: Path) -> None:
    _, state = await run(tmp_path, FORWARD, "{'tier': 1}")
    (run_id,) = [p.name for p in (state / "runs").iterdir()]
    (record,) = load_journal(state, run_id).agents
    same = call_key(AgentCall(0, "a", opts={"x": {"tier": 1}}))
    other = call_key(AgentCall(0, "a", opts={"x": {"tier": 2}}))
    assert record.key == same != other
    assert json.dumps(record.key)
