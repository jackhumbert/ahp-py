"""Inner providers loaded through their `from_config` factories, end to end (no Node, no model)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from ahp_host.node import Roots
from ahp_host.provider.base import ModelInfo
from ahp_host_acp import AcpProvider
from ahp_host_claude import ClaudeProvider

from ahp_host_flow import FlowProvider, load_config
from tests.flow_hosting import boot_flow, final_json
from tests.hosting import run_turn

FAKE_AGENT = Path(__file__).resolve().parent.parent / "acp_fake_agent.py"
ONE_PY = (
    "from ahp_host_flow.verbs import agent\n"
    "\n"
    "meta = {'name': 'one', 'description': 'd'}\n"
    "\n"
    "async def main(args):\n"
    "    return await agent('hi')\n"
)


async def test_flow_from_config_acp_drives_a_call(tmp_path: Path) -> None:
    q = json.dumps
    acp = tmp_path / "acp.toml"
    acp.write_text(
        'agent_name = "Fake ACP"\n'
        "port = 4322\n"
        f"command = [{q(sys.executable)}, {q(str(FAKE_AGENT))}]\n",
        encoding="utf-8",
    )
    toml = f'[providers.glm]\nload = "ahp_host_acp:from_config"\nconfig = {q(str(acp))}\n'
    (tmp_path / "ws").mkdir()
    (tmp_path / "user").mkdir()
    (tmp_path / "user" / "one.py").write_text(ONE_PY, encoding="utf-8")
    fx = await boot_flow(
        tmp_path, "no-node", toml, provider_options={"roots": Roots.single(tmp_path / "ws")}
    )
    try:
        turn = await run_turn(fx.wire, fx.chat, "t1", "/one")
        assert final_json(turn) == "acp: hi"
        loaded = fx.host.providers["flow"].providers["glm"]  # type: ignore[attr-defined]
        assert isinstance(loaded, AcpProvider)
        assert loaded.roots.paths == (
            (tmp_path / "ws").resolve(),
            (tmp_path / "state" / "runs").resolve(),
        )
        assert loaded.agent.display_name == "Fake ACP"
        assert (tmp_path / "state" / "providers" / "glm").is_dir()
    finally:
        await fx.shut()


def test_flow_from_config_claude_loads_and_lists_models(tmp_path: Path) -> None:
    toml = tmp_path / "flow.toml"
    toml.write_text(
        f"state_dir = {json.dumps(str(tmp_path / 'state'))}\n"
        "[providers.claude]\n"
        'load = "ahp_host_claude:from_config"\n'
        'models = { sonnet = "claude-sonnet-5-5" }\n'
        "[providers.claude.config]\n"
        'agent_name = "Claude"\n',
        encoding="utf-8",
    )
    flow = FlowProvider(load_config(toml), roots=Roots.single(tmp_path))
    assert "claude-sonnet-5-5" in [
        m.id if isinstance(m, ModelInfo) else m["id"] for m in flow.agent.models
    ]
    claude = flow.providers["claude"]
    assert isinstance(claude, ClaudeProvider)
    assert (tmp_path / "state" / "runs").resolve() in claude.roots.paths
