"""A failed call walks the fallback chain; keys and the journal follow (plan §8 M16)."""

from __future__ import annotations

from pathlib import Path

import pytest
from ahp_protocol.ext import flow

from ahp_host_flow.engine import AgentCall, call_key, load_journal
from tests.flow_hosting import boot_flow, final_json
from tests.hosting import run_turn, state
from tests.limits_fakes import ScriptedSession

TOML = (
    '[providers.primary]\nload = "tests.limits_fakes:ScriptedProvider"\n'
    'fallback = ["backup"]\n'
    '[providers.primary.config]\nprovider_id = "primary"\n'
    '[providers.backup]\nload = "tests.limits_fakes:ScriptedProvider"\n'
    '[providers.backup.config]\nprovider_id = "backup"\n'
    '[routing]\ndefault = "primary"\n'
)
REMOTE_TOML = (
    '[providers.primary]\nload = "tests.limits_fakes:ScriptedProvider"\n'
    'fallback = ["far"]\n'
    '[providers.primary.config]\nprovider_id = "primary"\n'
    '[providers.far]\nurl = "wss://far.example.com"\nagent = "scripted"\n'
    '[routing]\ndefault = "primary"\n'
)
# The key `test_keyhook_engine_pinned_keys` pins for AgentCall(0, "x").
KEY_X = "036eee93bfecbf115a5be81ab172412161c96cd301cad2b7ad733e04c2b01208"
FAIL = "fail-on:primary:acp.start"


def workflow(*calls: str) -> str:
    body = "".join(f"    out.append(await agent({c}))\n" for c in calls)
    return (
        "meta = {'name': 'fall', 'description': 'd'}\n"
        "\n"
        "async def main(args):\n"
        "    out = []\n"
        f"{body}"
        "    return out\n"
    )


async def _run(tmp_path: Path, toml: str, *calls: str) -> tuple[object, flow.RunView, Path]:
    ScriptedSession.reset()
    user = tmp_path / "user"
    user.mkdir()
    (user / "fall.py").write_text(workflow(*calls), encoding="utf-8")
    fx = await boot_flow(tmp_path, str(tmp_path / "no-node"), toml)
    try:
        turn = await run_turn(fx.wire, fx.chat, "t1", "/fall")
        view = flow.run_view(state(fx.host, fx.chat), turn_id="t1")
    finally:
        await fx.shut()
    (run,) = (tmp_path / "state" / "runs").iterdir()
    try:
        value = final_json(turn)
    except ValueError:  # an uncaught agent() error ends the turn with no result
        value = None
    return value, view, run


async def test_fallchain_unavailable_falls_back(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level("INFO", logger="ahp_host_flow.dispatch"):
        value, view, run = await _run(tmp_path, TOML, repr(FAIL))
    assert value == ["ok"]
    messages = [r.getMessage() for r in caplog.records]
    assert "agent() #0: fell back from primary to backup (unavailable)" in messages
    journal = load_journal(tmp_path / "state", run.name)
    assert [(s.ordinal, s.provider) for s in journal.started] == [(0, "backup")]
    assert [(a.ordinal, a.status, a.value) for a in journal.agents] == [(0, "ok", "ok")]
    assert [(s.ordinal, s.provider) for s in view.steps] == [(0, "backup")]


async def test_fallchain_failed_primary_usage_is_counted(tmp_path: Path) -> None:
    value, _, run = await _run(tmp_path, TOML, repr("spend-fail-on:primary:acp.start:40"))
    assert value == ["ok"]
    (record,) = load_journal(tmp_path / "state", run.name).agents
    # The primary's 40 output tokens before it failed, and the backup's 1.
    assert (record.status, record.output_tokens) == ("ok", 41)


async def test_fallchain_refusal_does_not_fall_back(tmp_path: Path) -> None:
    value, _, run = await _run(tmp_path, TOML, repr("fail-on:primary:acp.refusal"))
    assert value == [None]
    journal = load_journal(tmp_path / "state", run.name)
    assert [(a.status, a.kind) for a in journal.agents] == [("error", "refused")]
    assert [s.provider for s in journal.started] == ["primary"]


async def test_fallchain_pinned_keys_unchanged(tmp_path: Path) -> None:
    _, _, run = await _run(tmp_path, TOML, "'x'", repr(FAIL))
    journal = load_journal(tmp_path / "state", run.name)
    keys = [a.key for a in journal.agents]
    assert keys == [KEY_X, call_key(AgentCall(1, FAIL))]
    assert call_key(AgentCall(0, "x")) == KEY_X
    # The fallen-back call keeps its primary-route key; only its start record names the backup.
    assert [(s.ordinal, s.key, s.provider) for s in journal.started] == [
        (0, KEY_X, "primary"),
        (1, keys[1], "backup"),
    ]


async def test_fallchain_named_chat_skips_remote(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level("INFO", logger="ahp_host_flow.dispatch"):
        _, _, run = await _run(tmp_path, REMOTE_TOML, f"{FAIL!r}, x={{'chat': 'c'}}")
    messages = [r.getMessage() for r in caplog.records]
    assert (
        "agent() #0: fallback to far skipped (far is a remote provider; "
        "opts.x.chat/attach need an in-process one)"
    ) in messages
    assert not any("fell back" in m for m in messages)
    journal = load_journal(tmp_path / "state", run.name)
    assert [(a.status, a.kind) for a in journal.agents] == [("error", "unavailable")]
