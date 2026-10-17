"""agent() re-validates a result against opts.schema, on a fake dispatcher."""

from __future__ import annotations

from typing import Any

from ahp_host_flow.engine import AgentCall, RunOptions, Scheduler, schema_error

from ..fakes import FakeDispatcher  # noqa: TID252


async def _run(prompt: str, opts: dict[str, Any]) -> tuple[Any, list[str]]:
    logs: list[str] = []
    scheduler = Scheduler(FakeDispatcher(), RunOptions(), on_log=logs.append)
    value = await scheduler.run(AgentCall(ordinal=0, prompt=prompt, opts=opts))
    return value, logs


async def test_agent_schema_valid() -> None:
    assert await _run("x", {"schema": {"type": "string"}}) == ("x", [])


async def test_agent_schema_mismatch_is_null() -> None:
    value, logs = await _run("x", {"schema": {"type": "object"}})
    assert value is None
    assert logs == [
        "agent() #0 failed: result does not match opts.schema: (root): 'x' is not of type 'object'"
    ]


async def test_agent_schema_invalid_schema() -> None:
    value, logs = await _run("x", {"schema": {"type": 5}})
    assert value is None
    assert logs[0].startswith("agent() #0 failed: opts.schema is not a valid JSON Schema: ")


async def test_agent_schema_remote_ref_refused() -> None:
    value, logs = await _run("x", {"schema": {"$ref": "https://example.com/s.json"}})
    assert value is None
    assert logs == [
        "agent() #0 failed: opts.schema may only reference itself ('#...'), "
        "not 'https://example.com/s.json'"
    ]


async def test_agent_schema_not_a_mapping_ignored() -> None:
    assert await _run("x", {"schema": "nope"}) == ("x", [])


def test_agent_schema_error_messages() -> None:
    assert schema_error({"type": "integer"}, 3) is None
    assert (
        schema_error({"type": "integer"}, "3")
        == "result does not match opts.schema: (root): '3' is not of type 'integer'"
    )
