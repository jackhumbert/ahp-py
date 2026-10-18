"""The `[resume]` key hook on a hosted run: journal key mode and keys. No Node."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ahp_host_flow.engine import AgentCall, call_key, load_journal
from ahp_host_flow.engine.journal import LoadedJournal
from tests.flow_fakes import RestartHoldProvider, reset_fakes
from tests.flow_hosting import boot_flow, restart_flow, store_for
from tests.hosting import start_turn, state, wait_turn
from tests.integration.test_flow_durability import resume_by_hand, wait_resumed

BASE = '[providers.rec]\nload = "tests.flow_fakes:RecordingProvider"\n'
SCRIPT = (
    "meta = {'name': 'keyflow', 'description': 'd'}\n"
    "\n"
    "async def main(args):\n"
    "    return await agent('a')\n"
)


def _write(base: Path) -> str:
    user = base / "user"
    user.mkdir(parents=True, exist_ok=True)
    (user / "keyflow.py").write_text(SCRIPT, encoding="utf-8")
    return "/keyflow"


async def _run(tmp_path: Path, toml: str) -> tuple[dict[str, object], Path]:
    fx = await boot_flow(tmp_path, str(tmp_path / "no-node"), toml)
    try:
        await start_turn(fx.wire, fx.chat, "t1", _write(tmp_path))
        turn = await wait_turn(fx.wire, fx.chat, "t1")
    finally:
        await fx.shut()
    return dict(turn), tmp_path / "state"


def _journal(state: Path) -> LoadedJournal:
    (run,) = (state / "runs").iterdir()
    return load_journal(state, run.name)


async def test_keyhook_hosted_model_mode(tmp_path: Path) -> None:
    turn, state = await _run(tmp_path, BASE + '\n[resume]\nkey = "model"\n')
    assert turn["state"] == "complete"
    journal = _journal(state)
    assert journal.header.key_mode == "model"
    (record,) = journal.agents
    extra = {"provider": "rec", "model": None}
    assert record.key == call_key(AgentCall(0, "a"), extra=extra)


async def test_keyhook_hosted_default_mode(tmp_path: Path) -> None:
    turn, state = await _run(tmp_path, BASE)
    assert turn["state"] == "complete"
    journal = _journal(state)
    assert journal.header.key_mode == "default"
    (record,) = journal.agents
    assert record.key == call_key(AgentCall(0, "a"))


async def test_keyhook_hosted_path_mode(tmp_path: Path) -> None:
    path = "tests.hook_fakes:route_key"
    turn, state = await _run(tmp_path, BASE + f'\n[resume]\nkey = "{path}"\n')
    assert turn["state"] == "complete"
    journal = _journal(state)
    assert journal.header.key_mode == path
    (record,) = journal.agents
    assert record.key == call_key(AgentCall(0, "a"), extra={"p": "rec", "tag": "v1"})


async def test_keyhook_hosted_bad_path(tmp_path: Path) -> None:
    turn, _ = await _run(tmp_path, BASE + '\n[resume]\nkey = "tests.hook_fakes:nope"\n')
    assert turn["state"] == "error"
    assert "resume.key: cannot import" in json.dumps(turn)


HOLD_SCRIPT = (
    "meta = {'name': 'holdflow', 'description': 'd'}\n"
    "\n"
    "async def main(args):\n"
    "    a = await agent('one', model='fast')\n"
    "    b = await agent('two')\n"
    "    c = await agent('hold three')\n"
    "    return [a, b, c]\n"
)


def _hold_toml(alias_model: str) -> str:
    return (
        '[providers.hold]\nload = "tests.flow_fakes:RestartHoldProvider"\n'
        f'models = {{ fast = "{alias_model}" }}\n'
        '\n[routing]\ndefault = "hold"\n'
        '\n[resume]\nkey = "model"\n'
    )


@pytest.mark.parametrize(
    ("alias_after", "live"),
    [("m-2", ["one", "hold three"]), ("m-1", ["hold three"])],
)
async def test_keyhook_hosted_alias_change_resume(
    tmp_path: Path, alias_after: str, live: list[str]
) -> None:
    reset_fakes()
    user = tmp_path / "user"
    user.mkdir(parents=True, exist_ok=True)
    (user / "holdflow.py").write_text(HOLD_SCRIPT, encoding="utf-8")
    fx = await boot_flow(
        tmp_path, str(tmp_path / "no-node"), _hold_toml("m-1"), store=store_for(tmp_path)
    )
    await start_turn(fx.wire, fx.chat, "t1", "/holdflow")
    assert await fx.wire.until(lambda: "hold three" in RestartHoldProvider.prompts)
    await fx.shut()
    assert RestartHoldProvider.prompts == ["one", "two", "hold three"]
    RestartHoldProvider.holding = False
    RestartHoldProvider.prompts.clear()
    fx2 = await restart_flow(tmp_path, str(tmp_path / "no-node"), _hold_toml(alias_after))
    try:
        await resume_by_hand(fx2)
        await wait_resumed(fx2)
        # The changed alias made the earlier call live; the unchanged `two` came from the
        # journal, and the cut-off call runs again either way.
        assert RestartHoldProvider.prompts == live
        assert state(fx2.host, fx2.chat)["turns"][-1]["id"] == "t1"
    finally:
        await fx2.shut()
