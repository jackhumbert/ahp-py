"""The resume-key hook in the engine: `RunOptions.key_extra`, key modes and the library."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import Any

import pytest

from ahp_host_flow import library
from ahp_host_flow.engine import AgentCall, RunOptions, call_key, canonical_json, load_journal
from ahp_host_flow.sandbox import run_script

from ..fakes import FakeDispatcher  # noqa: TID252

KEY_X = "036eee93bfecbf115a5be81ab172412161c96cd301cad2b7ad733e04c2b01208"
KEY_REVIEW = "207c4890f127edc51ec49ad0762c0a320c9260df79dbcd584f8abcbfca5ad046"

SCRIPT = (
    "async def main(args):\n"
    "    await agent('x')\n"
    "    phase('Review')\n"
    "    await agent('review', model='fast', label='r')\n"
    "    return 1\n"
)

RESUME_LINE = (
    "resume: run 'r1' keyed agent() calls by 'default' and this run keys them by 'model'; "
    "no cached call can match, so every agent() call runs live"
)


def _options(tmp_path: Path, **kwargs: Any) -> RunOptions:
    return RunOptions(timeout=60, concurrency=4, state_dir=tmp_path / "state", **kwargs)


async def _run(
    source: str | None, dispatcher: FakeDispatcher, options: RunOptions, **kwargs: Any
) -> Any:
    kwargs.setdefault("filename", "w.py")
    return await asyncio.wait_for(
        run_script(source, node=None, dispatcher=dispatcher, options=options, **kwargs), 30
    )


def _keys(tmp_path: Path, run_id: str) -> list[str]:
    return [r.key for r in load_journal(tmp_path / "state", run_id).agents]


def _model_of(call: AgentCall) -> dict[str, Any]:
    return {"model": call.opts.get("model")}


def _version(call: AgentCall) -> dict[str, int]:
    return {"v": 1}


def test_keyhook_engine_pinned_keys() -> None:
    assert (
        call_key(AgentCall(0, "p", {"a": 1, "b": 2}, "ph", "lb"))
        == "78b92547b902887d2c1b40f46a0d6dca204d49cded66b218109014a5e2e1aab3"
    )
    review = AgentCall(3, "review", {"model": "fast", "label": "r"}, "Review", "r")
    assert call_key(review) == KEY_REVIEW
    assert call_key(AgentCall(0, "x")) == KEY_X
    call = AgentCall(0, "p", {"a": 1}, "ph", "lb")
    for extra in (None, {"provider": "p", "model": None}):
        payload = {"extra": extra, "label": "lb", "opts": {"a": 1}, "phase": "ph", "prompt": "p"}
        expected = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
        assert call_key(call, extra=extra) == expected


async def test_keyhook_engine_default_run_keys(tmp_path: Path) -> None:
    await _run(SCRIPT, FakeDispatcher(), _options(tmp_path, run_id="r1"))
    assert _keys(tmp_path, "r1") == [KEY_X, KEY_REVIEW]
    assert load_journal(tmp_path / "state", "r1").header.key_mode == "default"


async def test_keyhook_engine_hook_changes_keys(tmp_path: Path) -> None:
    dispatcher = FakeDispatcher()
    options = _options(tmp_path, run_id="r1", key_extra=_version, key_mode="custom")
    await _run(SCRIPT, dispatcher, options)
    keys = _keys(tmp_path, "r1")
    assert keys == [call_key(c, extra={"v": 1}) for c in dispatcher.calls]
    assert keys[0] != KEY_X
    assert keys[1] != KEY_REVIEW
    assert load_journal(tmp_path / "state", "r1").header.key_mode == "custom"


async def test_keyhook_engine_same_mode_resumes_cached(tmp_path: Path) -> None:
    options = _options(tmp_path, run_id="r1", key_extra=_version, key_mode="custom")
    await _run(SCRIPT, FakeDispatcher(), options)
    dispatcher = FakeDispatcher()
    resumed = _options(
        tmp_path, run_id="r2", resume_from="r1", key_extra=_version, key_mode="custom"
    )
    assert await _run(None, dispatcher, resumed) == 1
    assert dispatcher.calls == []
    assert _keys(tmp_path, "r2") == _keys(tmp_path, "r1")


async def test_keyhook_engine_mode_change_logs(tmp_path: Path) -> None:
    await _run(SCRIPT, FakeDispatcher(), _options(tmp_path, run_id="r1"))
    dispatcher = FakeDispatcher()
    lines: list[str] = []
    resumed = _options(
        tmp_path, run_id="r2", resume_from="r1", key_extra=_model_of, key_mode="model"
    )
    assert await _run(None, dispatcher, resumed, on_log=lines.append) == 1
    assert lines.count(RESUME_LINE) == 1
    assert not [line for line in lines if "cache ended" in line]
    assert [c.prompt for c in dispatcher.calls] == ["x", "review"]
    assert load_journal(tmp_path / "state", "r2").header.key_mode == "model"


async def test_keyhook_engine_hook_failure(tmp_path: Path) -> None:
    def hook(call: AgentCall) -> Any:
        if call.prompt == "x":
            raise RuntimeError("boom")
        return {"v": 1}

    source = (
        "async def main(args):\n"
        "    try:\n"
        "        await agent('x')\n"
        "    except Exception as exc:\n"
        "        message = str(exc)\n"
        "    await agent('y')\n"
        "    return message\n"
    )
    dispatcher = FakeDispatcher()
    options = _options(tmp_path, run_id="r1", key_extra=hook, key_mode="custom")
    message = await _run(source, dispatcher, options)
    assert "resume key hook failed for agent() #0" in message
    assert "boom" in message
    assert [(c.ordinal, c.prompt) for c in dispatcher.calls] == [(0, "y")]


def test_keyhook_engine_options_validation() -> None:
    with pytest.raises(ValueError, match="key_extra must be callable"):
        RunOptions(key_extra="nope")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="key_mode must be a non-empty string"):
        RunOptions(key_extra=_version, key_mode="")
    with pytest.raises(ValueError, match="key_mode must be a non-empty string"):
        RunOptions(key_extra=_version, key_mode=3)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="key_mode needs key_extra"):
        RunOptions(key_mode="model")
    with pytest.raises(ValueError, match="key_mode 'default' means no key_extra"):
        RunOptions(key_extra=_version, key_mode="default")
    assert RunOptions().key_mode_name() == "default"
    assert RunOptions(key_mode="default").key_mode_name() == "default"
    assert RunOptions(key_extra=_version, key_mode="m").key_mode_name() == "m"
    assert RunOptions(key_extra=_version).key_mode_name() == f"{__name__}:_version"


async def test_keyhook_engine_library(tmp_path: Path) -> None:
    dispatcher = FakeDispatcher()
    options = RunOptions(state_dir=tmp_path / "state", run_id="L1")
    async with library.run(dispatcher, options, key_extra=_model_of) as r:
        await r.agent("a", model="fast")
        await r.agent("b")
    keys = _keys(tmp_path, "L1")
    assert keys == [call_key(c, extra=_model_of(c)) for c in dispatcher.calls]
    header = load_journal(tmp_path / "state", "L1").header
    assert header.key_mode == f"{_model_of.__module__}:{_model_of.__qualname__}"


async def test_keyhook_engine_library_keeps_options_hook(tmp_path: Path) -> None:
    dispatcher = FakeDispatcher()
    options = _options(tmp_path, run_id="L2", key_extra=_model_of, key_mode="custom")
    async with library.run(dispatcher, options) as r:
        await r.agent("a", model="fast")
    keys = _keys(tmp_path, "L2")
    assert keys == [call_key(c, extra=_model_of(c)) for c in dispatcher.calls]
    assert load_journal(tmp_path / "state", "L2").header.key_mode == "custom"
    # A key_extra keyword still wins over the options' hook.
    other = _options(tmp_path, run_id="L3", key_extra=_model_of, key_mode="custom")
    async with library.run(FakeDispatcher(), other, key_extra=_version) as r:
        await r.agent("a")
    header = load_journal(tmp_path / "state", "L3").header
    assert header.key_mode == f"{_version.__module__}:{_version.__qualname__}"
