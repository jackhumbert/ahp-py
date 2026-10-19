"""``Session.workflows`` and ``Session.start_workflow``: typed workflow starts, client side."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any, cast

import pytest
from ahp_protocol.ext import flow
from ahp_protocol.ext.flow import WorkflowInfo

from ahp_client.api.client import Client, Session

from ._sibling import requires_sibling_host

SCHEMA: dict[str, Any] = {"type": "object", "properties": {"n": {"type": "integer"}}}


class _Mirror:
    def __init__(self, states: dict[str, Any]) -> None:
        self._states = states

    def state(self, uri: str) -> Any:
        return self._states.get(uri)


class _Stub:
    def __init__(self, states: dict[str, Any]) -> None:
        self.mirror = _Mirror(states)


def _prompt(name: str, source: str, language: str, **extra: Any) -> dict[str, Any]:
    # The shape ahp-host-flow lists: ``type`` discriminator, descriptor under FLOW_META_KEY.
    marker = {"kind": "workflow", "source": source, "language": language, **extra}
    return {"type": "prompt", "name": name, "_meta": flow.meta(marker)}


def _session(state: dict[str, Any] | None) -> Session:
    states = {} if state is None else {"s:/1": state}
    return Session(cast(Client, _Stub(states)), "s:/1", "flow", owned=False)


def test_startworkflow_workflows_lists_the_plugin_children() -> None:
    state = {
        "customizations": [
            {
                "type": "plugin",
                "name": "host",
                "children": [
                    _prompt("typed", "host", "js", description="d", argsSchema=SCHEMA),
                    _prompt("plain", "host", "python"),
                ],
            }
        ]
    }
    found = _session(state).workflows()
    assert [w.qualified_name for w in found] == ["host:typed", "host:plain"]
    assert all(isinstance(w, WorkflowInfo) for w in found)
    assert found[0].args_schema == SCHEMA
    assert found[1].args_schema is None
    assert found[1].language == "python"


def test_startworkflow_no_customizations_is_empty() -> None:
    assert _session({}).workflows() == ()
    assert _session(None).workflows() == ()


def test_startworkflow_run_id_is_known_before_any_await() -> None:
    session = _session({})
    assert session.start_workflow("host:demo", {"n": 1}, run_id="r1").run_id == "r1"
    made = session.start_workflow("demo").run_id
    assert made is not None
    assert len(made) == 32
    assert set(made) <= set("0123456789abcdef")


def test_startworkflow_rejects_bad_input_before_sending() -> None:
    session = _session({})
    with pytest.raises(ValueError, match="runId"):
        session.start_workflow("demo", run_id="../x")
    with pytest.raises(ValueError, match="workflow"):
        session.start_workflow("")
    with pytest.raises(TypeError, match="meta"):
        session.start_workflow("demo", meta={"k": 1})


async def _run(scenario: Any) -> None:
    from ahp_host.core import Host, LoopbackSingleUserPolicy
    from ahp_host.provider import EchoProvider
    from ahp_protocol.transport import memory_pair

    from ahp_client.api import connect

    host = Host(provider=EchoProvider(), policy=LoopbackSingleUserPolicy())
    client_side, host_side = memory_pair()
    served = asyncio.get_running_loop().create_task(host.serve(host_side))
    try:
        async with connect(transport=client_side) as client:
            provider = str(client.agents()[0]["provider"])
            session = await client.create_session(provider=provider, cwd=".")
            await scenario(session)
    finally:
        served.cancel()
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await served


def _last_message(session: Session, chat_uri: str) -> dict[str, Any]:
    state = session._client.mirror.state(chat_uri)
    assert isinstance(state, dict)
    message = state["turns"][-1]["message"]
    assert isinstance(message, dict)
    return message


@requires_sibling_host
async def test_startworkflow_sends_text_and_typed_meta() -> None:
    from ahp_client.api import TurnCompleted

    async def scenario(session: Session) -> None:
        stream = session.start_workflow("host:demo", {"n": 1}, run_id="r1")
        end = await stream
        assert isinstance(end, TurnCompleted)
        chat = await session.chat()
        message = _last_message(session, chat.uri)
        assert message["text"] == '/host:demo {"n": 1}'
        assert message["_meta"] == {
            "ahp-py/flow": {"workflow": "host:demo", "args": {"n": 1}, "runId": "r1"}
        }
        assert isinstance(await chat.prompt("plain", idle_timeout=10.0), TurnCompleted)
        assert "_meta" not in _last_message(session, chat.uri)

    await _run(scenario)
