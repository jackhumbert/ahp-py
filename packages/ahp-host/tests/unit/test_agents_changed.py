"""`root/agentsChanged`: an agent whose models arrive after the host started.

`RootState.agents` was computed once, when the root channel was built. An
agent that discovers its models at start-up -- and can fail to, and retry --
or that only learns them per session left the model picker empty for the life
of the host. `UpdatesAgentInfo` hands such a provider a notifier.
"""

from __future__ import annotations

from typing import Any

import pytest
from ahp_protocol.channels import ROOT_URI

from ahp_host.core import Host, LoopbackSingleUserPolicy
from ahp_host.provider import EchoProvider
from ahp_host.provider.base import (
    AgentInfo,
    AgentInfoChanged,
    ModelInfo,
    UpdatesAgentInfo,
)

from .hosting import assert_frames_valid, connect, shut, state

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class LateModels:
    """A provider that knows no models until it is told it found some."""

    def __init__(self) -> None:
        self.models: tuple[ModelInfo, ...] = ()
        self.provider_id = "late"
        self.changed: AgentInfoChanged | None = None

    @property
    def agent(self) -> AgentInfo:
        return AgentInfo(
            provider=self.provider_id,
            display_name="Late",
            description="Finds its models eventually.",
            models=self.models,
        )

    async def create_session(self, context: Any) -> Any:  # pragma: no cover - not used
        raise AssertionError("no sessions in these tests")

    async def attach_agent_updates(self, changed: AgentInfoChanged) -> None:
        self.changed = changed


def _agents(host: Host) -> list[dict[str, Any]]:
    agents = state(host, ROOT_URI).get("agents")
    return list(agents) if isinstance(agents, list) else []


def test_customizations_are_published_on_agent_info() -> None:
    """`AgentInfo.customizations` (1.0.0): what the agent itself brings."""
    plugin = {"kind": "plugin", "id": "p1", "uri": "file:///plugins/p1", "name": "P1"}
    wire = AgentInfo(
        provider="x", display_name="X", description="", customizations=(plugin,)
    ).to_wire()
    assert wire["customizations"] == [plugin]
    bare = AgentInfo(provider="x", display_name="X", description="")
    assert "customizations" not in bare.to_wire()


def test_the_protocol_is_feature_detected() -> None:
    assert isinstance(LateModels(), UpdatesAgentInfo)
    assert not isinstance(EchoProvider(), UpdatesAgentInfo)


async def test_late_models_reach_the_root_channel_once() -> None:
    provider = LateModels()
    host = Host(provider, LoopbackSingleUserPolicy())
    wire, serving = await connect(host)
    try:
        assert provider.changed is not None, "the notifier was never attached"
        assert _agents(host)[0]["models"] == []

        provider.models = (ModelInfo(id="m1", name="Model One"),)
        assert await provider.changed() is True
        await wire.until(lambda: bool(wire.actions(ROOT_URI, "root/agentsChanged")))
        assert [m["id"] for m in _agents(host)[0]["models"]] == ["m1"]

        # Unchanged: nothing published, so a provider may call it freely.
        assert await provider.changed() is False
        await wire.request("ping", {})
        assert len(wire.actions(ROOT_URI, "root/agentsChanged")) == 1
        assert_frames_valid(wire, ("root", ROOT_URI))
    finally:
        await shut(host, wire, serving)


async def test_a_changed_provider_id_publishes_nothing() -> None:
    """The id keys every session the agent serves."""
    provider = LateModels()
    host = Host(provider, LoopbackSingleUserPolicy())
    wire, serving = await connect(host)
    try:
        assert provider.changed is not None
        provider.provider_id = "renamed"
        assert await provider.changed() is False
        assert _agents(host)[0]["provider"] == "late"
    finally:
        await shut(host, wire, serving)


async def test_an_embedder_can_refresh_directly() -> None:
    provider = LateModels()
    host = Host(provider, LoopbackSingleUserPolicy())
    assert await host.refresh_agents() is False, "no root channel yet: nothing to change"
    wire, serving = await connect(host)
    try:
        provider.models = (ModelInfo(id="m2", name="Model Two"),)
        assert await host.refresh_agents() is True
        assert [m["id"] for m in _agents(host)[0]["models"]] == ["m2"]
    finally:
        await shut(host, wire, serving)
