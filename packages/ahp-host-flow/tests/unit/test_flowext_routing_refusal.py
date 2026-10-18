"""`opts.x.chat` and `opts.x.attach` are refused for fleet and remote providers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ahp_host_flow.config import FlowConfig, ProviderConfig, Routing
from ahp_host_flow.dispatch import FlowAgentError, RoutingDispatcher
from ahp_host_flow.engine import AgentCall

from ..fakes import FakeDispatcher  # noqa: TID252

PROVIDERS = {
    "local": ProviderConfig("local", load="tests.flow_fakes:EchoProvider"),
    "far": ProviderConfig("far", load=None, via="fleet", node="node-1", agent="claude"),
    "dial": ProviderConfig("dial", load=None, url="ws://example.com/ahp", agent="claude"),
}


def _routing(tmp_path: Path) -> tuple[RoutingDispatcher, dict[str, FakeDispatcher]]:
    fakes = {"inprocess": FakeDispatcher(), "fleet": FakeDispatcher(), "remote": FakeDispatcher()}
    config = FlowConfig(providers=PROVIDERS, routing=Routing("local"), state_dir=tmp_path)
    dispatcher = RoutingDispatcher(
        config,
        inprocess=fakes["inprocess"],
        fleet=fakes["fleet"],
        remote=lambda _name, _spec: fakes["remote"],
    )
    return dispatcher, fakes


def _call(x: dict[str, Any]) -> AgentCall:
    return AgentCall(ordinal=0, prompt="p", opts={"x": x})


@pytest.mark.parametrize(
    ("provider", "kind"),
    [("far", "fleet"), ("dial", "remote")],
)
@pytest.mark.parametrize(
    ("x", "option"),
    [
        ({"chat": "planner"}, "chat"),
        ({"attach": [{"kind": "result", "value": 1}]}, "attach"),
        ({"chat": "planner", "attach": []}, "chat"),
    ],
)
async def test_flowext_routing_refuses_chat_and_attach(
    tmp_path: Path, provider: str, kind: str, x: dict[str, Any], option: str
) -> None:
    dispatcher, fakes = _routing(tmp_path)
    with pytest.raises(FlowAgentError) as info:
        await dispatcher.run_agent(_call({**x, "provider": provider}))
    assert str(info.value) == (
        f"opts.x.{option} needs an in-process provider; {provider!r} is a {kind} provider"
    )
    assert all(not fake.calls for fake in fakes.values())


async def test_flowext_routing_inprocess_takes_chat_and_attach(tmp_path: Path) -> None:
    dispatcher, fakes = _routing(tmp_path)
    await dispatcher.run_agent(_call({"chat": "planner", "attach": []}))
    assert len(fakes["inprocess"].calls) == 1
