"""The fan-out corpus script: on fakes, and (opt-in) through the configured providers.

`AHP_FLOW_LIVE_CONFIG` names a flow.toml; the live run makes real model calls.
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
from pathlib import Path
from typing import Any

import ahp_client
import pytest
from ahp_client import TurnCancelled, TurnCompleted, TurnFailed
from ahp_host import Host, LoopbackSingleUserPolicy
from ahp_protocol.ext.flow import RunView
from ahp_protocol.transport import memory_pair

from ahp_host_flow import FlowProvider, load_config
from ahp_host_flow.config import FlowConfig, parse_config
from ahp_host_flow.inprocess import FlowRunFailed, _value

pytestmark = pytest.mark.node

SCRIPT = Path(__file__).resolve().parents[2] / "conformance" / "corpus" / "fanout.js"
TOPICS = ["alpha", "beta", "gamma"]


async def run_corpus(config: FlowConfig, root: Path) -> tuple[Any, RunView]:
    """Run the fan-out script in a flow session rooted at *root*: its value and run view."""
    host = Host(FlowProvider(config, user_workflows_dir=root / "user"), LoopbackSingleUserPolicy())
    client_t, server_t = memory_pair()
    serving = asyncio.create_task(host.serve(server_t))
    try:
        async with (
            ahp_client.connect(transport=client_t) as client,
            await client.create_session(
                provider="flow", working_directories=[root.as_uri()]
            ) as session,
        ):
            text = ""
            async for event in session.prompt("```js\n" + SCRIPT.read_text("utf-8") + "\n```"):
                if isinstance(event, TurnCompleted):
                    text = event.text
                elif isinstance(event, TurnFailed):
                    raise FlowRunFailed(event.reason)
                elif isinstance(event, TurnCancelled):
                    raise FlowRunFailed("run cancelled")
            value = _value(text)
            await session.chat()
            return value, session.flow_view()
    finally:
        serving.cancel()
        await host.aclose()


async def test_fanout_corpus_on_fakes(tmp_path: Path, node_path: str) -> None:
    config = parse_config(
        {
            "state_dir": str(tmp_path / "state"),
            "node": node_path,
            "providers": {"echo": {"load": "ahp_host.provider.echo:EchoProvider"}},
        },
        base=tmp_path,
    )
    value, view = await run_corpus(config, tmp_path)
    assert value["answers"] == [f"You said: Reply with exactly one word: {t}" for t in TOPICS]
    assert value["summary"] == "You said: Reply with exactly one word: done (3 answers)"
    assert view.status == "ok"
    assert [p.title for p in view.phases] == ["Fan out", "Reduce"]
    assert [s.label for s in view.steps] == ["fan-alpha", "fan-beta", "fan-gamma", "reduce"]
    assert [s.status for s in view.steps] == ["ok"] * 4


@pytest.mark.skipif(
    not os.environ.get("AHP_FLOW_LIVE_CONFIG"),
    reason="set AHP_FLOW_LIVE_CONFIG to a flow.toml to make real model calls",
)
async def test_fanout_corpus_live(tmp_path: Path, node_path: str) -> None:
    config = dataclasses.replace(
        load_config(Path(os.environ["AHP_FLOW_LIVE_CONFIG"])),
        node=node_path,
        state_dir=tmp_path / "state",
    )
    value, view = await asyncio.wait_for(run_corpus(config, tmp_path), 900)
    # Wording varies by model: only the requested word is checked.
    for topic, answer in zip(TOPICS, value["answers"], strict=True):
        assert isinstance(answer, str)
        assert topic in answer.lower()
    assert "done" in str(value["summary"]).lower()
    assert view.status == "ok"
    assert [s.status for s in view.steps] == ["ok"] * 4
