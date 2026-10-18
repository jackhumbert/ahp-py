"""VerbRunner: names, ordinals, call ids, errors, masking, abort and replay (plan M12)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from ahp_host_flow.engine import (
    RESERVED_VERBS,
    AbortSignal,
    JournalWriter,
    Redactor,
    RunHeader,
    RunOptions,
    VerbCall,
    VerbFailedError,
    VerbRecord,
    VerbReplay,
    VerbRunner,
    load_journal,
    verb_call_id,
    verb_key,
    verb_name_error,
)
from tests.hook_fakes import ECHO_CALLS, FakeVerbHost, FakeVerbs, echo_verb, reset_hooks


@pytest.fixture(autouse=True)
def _reset() -> None:
    reset_hooks()


def _writer(tmp_path: Path) -> JournalWriter:
    return JournalWriter.create(
        tmp_path, RunHeader("r1", "w.js", "return 1", False, None, 10, None)
    )


async def _call(runner: VerbRunner, name: str, *args: Any) -> Any:
    call, cached = runner.issue(name, list(args))
    return await runner.run(call, cached)


def test_customverb_runner_names() -> None:
    assert verb_name_error("tool") is None
    for name in ("ask", "approve", "canvas"):
        assert verb_name_error(name) is None
    for name in RESERVED_VERBS:
        message = verb_name_error(name)
        assert message is not None
        assert "reserved" in message
        assert all(r in message for r in RESERVED_VERBS)
    for name in ("_x", "1a", "a-b", "class", "None", "await"):
        assert verb_name_error(name) is not None
    assert verb_name_error(3) is not None
    with pytest.raises(ValueError, match="reserved"):
        RunOptions(verbs={"agent": echo_verb})
    with pytest.raises(ValueError, match=r"verbs\.tool must be callable"):
        RunOptions(verbs={"tool": 3})  # type: ignore[dict-item]
    assert RunOptions(verbs={"tool": echo_verb}).verbs == {"tool": echo_verb}


async def test_customverb_runner_ordinals_and_call_ids() -> None:
    fakes = FakeVerbs({"tool": "echo", "render": "echo"})
    runner = VerbRunner(fakes.handlers)
    assert runner.names == ("render", "tool")
    await _call(runner, "tool", 1)
    await _call(runner, "render", 2)
    await _call(runner, "tool", 1)
    assert [c.ordinal for c in fakes.calls] == [0, 1, 2]
    first, _, third = fakes.calls
    key = verb_key("tool", [1])
    assert first.call_id == verb_call_id(key, 0)
    assert third.call_id == verb_call_id(key, 1)
    assert first.call_id.endswith("-0")
    assert third.call_id.endswith("-1")
    assert first.call_id[:-2] == third.call_id[:-2]


def test_customverb_runner_undeclared_refused() -> None:
    runner = VerbRunner(FakeVerbs({"tool": "echo"}).handlers)
    with pytest.raises(ValueError, match="unknown verb 'nope': this run declares tool"):
        runner.issue("nope", [])
    with pytest.raises(ValueError, match="verb args must be an array"):
        runner.issue("tool", {})
    with pytest.raises(ValueError, match="verb args must be JSON"):
        runner.issue("tool", [object()])
    call, _ = runner.issue("tool", [])
    assert call.ordinal == 0
    assert call.call_id.endswith("-0")


async def test_customverb_runner_error_and_non_json(tmp_path: Path) -> None:
    fakes = FakeVerbs({"tool": "fail"})

    async def bad(call: VerbCall) -> Any:
        return {1, 2}

    async def silent(call: VerbCall) -> Any:
        raise ValueError

    handlers = {**fakes.handlers, "bad": bad, "silent": silent}
    writer = _writer(tmp_path)
    runner = VerbRunner(handlers, journal=writer)
    with pytest.raises(VerbFailedError) as err:
        await _call(runner, "tool", 1, "a")
    assert str(err.value) == 'tool failed: 1, "a"'
    with pytest.raises(VerbFailedError) as err:
        await _call(runner, "bad")
    assert str(err.value).startswith("bad() returned a value that is not JSON: ")
    with pytest.raises(VerbFailedError) as err:
        await _call(runner, "silent")
    assert str(err.value) == "ValueError"
    writer.close()
    verbs = load_journal(tmp_path, "r1").verbs
    assert [(v.name, v.status) for v in verbs] == [
        ("tool", "error"),
        ("bad", "error"),
        ("silent", "error"),
    ]
    assert verbs[0].error == 'tool failed: 1, "a"'
    assert verbs[2].error == "ValueError"


async def test_customverb_runner_masking(tmp_path: Path) -> None:
    secret = "token=abcdefgh12345678"
    seen: list[Any] = []

    async def tool(call: VerbCall) -> Any:
        seen.append(call.args[0])
        return {"echo": call.args[0]}

    host = FakeVerbHost()
    writer = _writer(tmp_path)
    runner = VerbRunner({"tool": tool}, host=host, mask=Redactor().mask_value, journal=writer)
    value = await _call(runner, "tool", secret)
    writer.close()
    assert seen == [secret]
    assert secret not in str(value)
    (call, args) = host.started_calls[0]
    assert call.name == "tool"
    assert secret not in str(args)
    assert host.finished_calls[0][1:] == (True, value, None)
    assert load_journal(tmp_path, "r1").verbs[0].value == value


async def test_customverb_runner_abort(tmp_path: Path) -> None:
    fakes = FakeVerbs({"tool": "hang"})
    abort = AbortSignal()
    logs: list[str] = []
    writer = _writer(tmp_path)
    runner = VerbRunner(fakes.handlers, abort=abort, on_log=logs.append, journal=writer)
    task = asyncio.ensure_future(_call(runner, "tool"))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    abort.abort()
    with pytest.raises(VerbFailedError, match=r"^tool\(\) aborted$"):
        await task
    assert "tool() #0 aborted" in logs
    with pytest.raises(VerbFailedError, match=r"^tool\(\) aborted$"):
        await _call(runner, "tool")
    runner.close()
    writer.close()
    verbs = load_journal(tmp_path, "r1").verbs
    assert [v.status for v in verbs] == ["open"]


async def test_customverb_runner_replay() -> None:
    fakes = FakeVerbs({"tool": "echo", "render": "echo", "other": "echo"})
    k_done, k_err, k_open = (verb_key(n, [1]) for n in ("tool", "render", "other"))
    ids = [verb_call_id(k, 0) for k in (k_done, k_err, k_open)]
    replay = VerbReplay(
        [
            VerbRecord(0, k_done, "tool", ids[0], "done", {"cached": True}),
            VerbRecord(1, k_err, "render", ids[1], "error", None, "stored failure"),
            VerbRecord(2, k_open, "other", ids[2], "open"),
        ]
    )
    logs: list[str] = []
    runner = VerbRunner(fakes.handlers, replay=replay, on_log=logs.append)
    assert await _call(runner, "tool", 1) == {"cached": True}
    with pytest.raises(VerbFailedError, match=r"^stored failure$"):
        await _call(runner, "render", 1)
    assert fakes.calls == []
    assert await _call(runner, "other", 1) == {"name": "other", "args": [1]}
    assert [c.call_id for c in fakes.calls] == [ids[2]]
    assert [ids[2]] == ECHO_CALLS
    assert logs == [
        "resume: other() #2 was cut off before it finished; "
        f"calling its handler again with call id {ids[2]}"
    ]


async def test_customverb_runner_default_context() -> None:
    async def tool(call: VerbCall) -> Any:
        return await call.context.ask("name?")

    runner = VerbRunner({"tool": tool})
    with pytest.raises(VerbFailedError) as err:
        await _call(runner, "tool")
    assert str(err.value).startswith("ask() has no answer path")


async def test_customverb_runner_sync_handler() -> None:
    def tool(call: VerbCall) -> Any:
        return {"n": len(call.args)}

    runner = VerbRunner({"tool": tool})
    assert await _call(runner, "tool", 1, 2) == {"n": 2}
