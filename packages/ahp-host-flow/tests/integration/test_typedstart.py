"""Typed workflow starts, args validation and client-chosen run ids (plan §8 M13)."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from ahp_protocol.ext import flow

from ahp_host_flow.engine import RUN_ID
from tests.flow_fakes import RecordingProvider
from tests.flow_hosting import FlowFixture, boot_flow, final_json
from tests.hosting import (
    all_channels,
    found,
    notifications,
    open_session,
    run_turn,
    start_turn,
    state,
    wait_turn,
)

TOML = '[providers.rec]\nload = "tests.flow_fakes:RecordingProvider"\n'
ARGS: dict[str, Any] = {
    "type": "object",
    "properties": {"n": {"type": "integer"}},
    "required": ["n"],
}
ARGS_PY = (
    f"meta = {{'name': 'argsflow', 'description': 'd', 'x': {{'args': {ARGS!r}}}}}\n"
    "\n"
    "async def main(args):\n"
    "    return {'n': args['n'], 'r': await agent('hi', label='a')}\n"
)
ARGS_JS = (
    "export const meta = {name: 'argsjs', description: 'd', x: {args: "
    '{type: "object", properties: {n: {type: "integer"}}, required: ["n"]}}}\n'
    "return {n: args.n}\n"
)
TEXT = '/user:argsflow {"n": 2}'


def write(base: Path, filename: str, source: str) -> None:
    user = base / "user"
    user.mkdir(parents=True, exist_ok=True)
    (user / filename).write_text(source, encoding="utf-8")


def error_of(turn: Mapping[str, Any]) -> Mapping[str, Any]:
    (part,) = found(turn, lambda m: m.get("kind") == "error")
    error = part["error"]
    assert isinstance(error, Mapping)
    return error


def typed(workflow: str, **kw: Any) -> dict[str, Any]:
    return flow.meta(flow.workflow_start(workflow, **kw))


def run_ids(fx: FlowFixture) -> list[str]:
    root = fx.base / "state" / "runs"
    return sorted(p.name for p in root.iterdir()) if root.is_dir() else []


async def typed_turn(
    fx: FlowFixture, turn_id: str, meta: Mapping[str, Any], text: str = TEXT
) -> dict[str, Any]:
    await start_turn(fx.wire, fx.chat, turn_id, text, meta=meta)
    return await wait_turn(fx.wire, fx.chat, turn_id)


def assert_nothing_dispatched(fx: FlowFixture, channels: set[str]) -> None:
    assert set(all_channels(fx.host)) == channels
    assert all(not p.sessions for p in RecordingProvider.instances)
    assert run_ids(fx) == []


async def test_typedstart_runs_with_the_given_run_id(tmp_path: Path) -> None:
    write(tmp_path, "argsflow.py", ARGS_PY)
    fx = await boot_flow(tmp_path, str(tmp_path / "no-node"), TOML)
    try:
        meta = typed("user:argsflow", args={"n": 2}, run_id="client-run-1")
        turn = await typed_turn(fx, "t1", meta)
        assert final_json(turn)["n"] == 2
        assert flow.run_view(state(fx.host, fx.chat)).run_id == "client-run-1"
        assert (tmp_path / "state" / "runs" / "client-run-1" / "journal.jsonl").exists()
    finally:
        await fx.shut()


async def test_typedstart_a_taken_run_id_gets_a_fresh_one(tmp_path: Path) -> None:
    write(tmp_path, "argsflow.py", ARGS_PY)
    fx = await boot_flow(tmp_path, str(tmp_path / "no-node"), TOML)
    try:
        meta = typed("user:argsflow", args={"n": 2}, run_id="client-run-1")
        await typed_turn(fx, "t1", meta)
        turn = await typed_turn(fx, "t2", meta)
        run_id = flow.run_view(state(fx.host, fx.chat)).run_id
        assert run_id != "client-run-1"
        assert run_id is not None
        assert RUN_ID.fullmatch(run_id)
        assert any("run id 'client-run-1' is not available" in n for n in notifications(turn))
    finally:
        await fx.shut()


async def test_typedstart_concurrent_starts_with_one_run_id_both_run(tmp_path: Path) -> None:
    write(tmp_path, "argsflow.py", ARGS_PY)
    fx = await boot_flow(tmp_path, str(tmp_path / "no-node"), TOML)
    try:
        other = await open_session(
            fx.wire, "flow:/s2", "flow", working_directories=[fx.workspace.as_uri()]
        )
        meta = typed("user:argsflow", args={"n": 2}, run_id="client-run-1")
        # Both starts are in flight before either run has created its journal.
        await start_turn(fx.wire, fx.chat, "t1", TEXT, meta=meta)
        await start_turn(fx.wire, other, "t2", TEXT, meta=meta)
        turns = [await wait_turn(fx.wire, c, t) for c, t in ((fx.chat, "t1"), (other, "t2"))]
        assert all(final_json(t)["n"] == 2 for t in turns)
        ids = [flow.run_view(state(fx.host, c)).run_id for c in (fx.chat, other)]
        assert None not in ids
        assert len(set(ids)) == 2
        assert "client-run-1" in ids
        assert sorted(run_ids(fx)) == sorted(str(i) for i in ids)
        taken = [n for t in turns for n in notifications(t) if "is not available" in n]
        assert len(taken) == 1, taken
    finally:
        await fx.shut()


async def test_typedstart_bad_args_fail_and_dispatch_nothing(tmp_path: Path) -> None:
    write(tmp_path, "argsflow.py", ARGS_PY)
    RecordingProvider.instances.clear()
    fx = await boot_flow(tmp_path, str(tmp_path / "no-node"), TOML)
    try:
        channels = set(all_channels(fx.host))
        turn = await typed_turn(fx, "t1", typed("user:argsflow", args={"n": "x"}))
        error = error_of(turn)
        assert error["errorType"] == "flow.badArgs"
        assert "n: 'x' is not of type 'integer'" in str(error["message"])
        assert_nothing_dispatched(fx, channels)
        turn = await run_turn(fx.wire, fx.chat, "t2", '/argsflow {"n": "x"}')
        error = error_of(turn)
        assert error["errorType"] == "flow.badArgs"
        assert "n: 'x' is not of type 'integer'" in str(error["message"])
        assert_nothing_dispatched(fx, channels)
    finally:
        await fx.shut()


async def test_typedstart_malformed_start_is_bad_args(tmp_path: Path) -> None:
    write(tmp_path, "argsflow.py", ARGS_PY)
    fx = await boot_flow(tmp_path, str(tmp_path / "no-node"), TOML)
    try:
        turn = await typed_turn(fx, "t1", {"ahp-py/flow": {"workflow": 5}})
        assert error_of(turn)["errorType"] == "flow.badArgs"
        turn = await typed_turn(fx, "t2", {"ahp-py/flow": {"workflow": "bogus:x"}})
        error = error_of(turn)
        assert error["errorType"] == "flow.badArgs"
        assert "Not a workflow name" in str(error["message"])
    finally:
        await fx.shut()


async def test_typedstart_stock_text_start_still_works(tmp_path: Path) -> None:
    write(tmp_path, "argsflow.py", ARGS_PY)
    fx = await boot_flow(tmp_path, str(tmp_path / "no-node"), TOML)
    try:
        turn = await run_turn(fx.wire, fx.chat, "t1", '/argsflow {"n": 3}')
        assert final_json(turn)["n"] == 3
    finally:
        await fx.shut()


@pytest.mark.node
async def test_typedstart_python_meta_x_args_js_twin(tmp_path: Path, node_path: str) -> None:
    write(tmp_path, "argsjs.js", ARGS_JS)
    fx = await boot_flow(tmp_path, node_path, TOML)
    try:
        turn = await typed_turn(fx, "t1", typed("user:argsjs", args={"n": "x"}))
        assert error_of(turn)["errorType"] == "flow.badArgs"
    finally:
        await fx.shut()


def test_typedstart_run_id_pattern_matches_the_engine() -> None:
    assert "^" + RUN_ID.pattern + "$" == flow.RUN_ID_PATTERN
