"""The reference agent behind the flow provider: routing, rate limits, the README block (M18)."""

from __future__ import annotations

import json
import re
import sys
import tomllib
from pathlib import Path

from ahp_host.node import Roots
from ahp_host_acp.reference.stub import StubServer, refuse, say

from ahp_host_flow.config import parse_config
from ahp_host_flow.engine import load_journal
from tests.flow_hosting import FlowFixture, boot_flow, final_json
from tests.hosting import run_turn

ROOT = Path(__file__).resolve().parents[2]

REVIEW = (
    "meta = {'name': 'review', 'description': 'd'}\n"
    "\n"
    "async def main(args):\n"
    "    first = await agent('draft it', label='Draft')\n"
    "    phase('Review')\n"
    "    second = await agent('review it', label='Review')\n"
    "    return {'first': first, 'second': second}\n"
)
FAIL = (
    "meta = {'name': 'fail', 'description': 'd'}\n"
    "\n"
    "async def main(args):\n"
    "    return await agent('x', label='Fail')\n"
)


def _toml(stub: StubServer, default: str) -> str:
    q = json.dumps
    echo = '[providers.echo]\nload = "tests.flow_fakes:StrippedEchoProvider"\n\n'
    rule = '\n[[routing.rules]]\nprovider = "openai"\nphase = "Review"\n'
    return (
        (echo if default == "echo" else "")
        + '[providers.openai]\nload = "ahp_host_acp:from_config"\n\n'
        + f'[providers.openai.config]\ncommand = [{q(sys.executable)}, "-m", '
        + '"ahp_host_acp.reference"]\n\n'
        + "[providers.openai.config.env]\n"
        + f"AHP_OPENAI_BASE_URL = {q(stub.base_url)}\n"
        + 'AHP_OPENAI_MODEL = "stub-model"\n\n'
        + f"[routing]\ndefault = {q(default)}\n"
        + (rule if default == "echo" else "")
    )


async def _boot(tmp_path: Path, toml: str) -> FlowFixture:
    # The ACP provider serves only the folders it is given; without the workspace as a root it
    # refuses the session's cwd ("outside this host's folders") before the endpoint is reached.
    roots = Roots.single((tmp_path / "ws").resolve())
    return await boot_flow(
        tmp_path, str(tmp_path / "no-node"), toml, provider_options={"roots": roots}
    )


async def test_refflow_phase_routes_to_the_reference_agent(tmp_path: Path) -> None:
    user = tmp_path / "user"
    user.mkdir()
    (user / "review.py").write_text(REVIEW, encoding="utf-8")
    with StubServer([say("LGTM from the stub")]) as stub:
        fx = await _boot(tmp_path, _toml(stub, "echo"))
        try:
            turn = await run_turn(fx.wire, fx.chat, "t1", "/review")
        finally:
            await fx.shut()
        result = final_json(turn)
        assert result["second"] == "LGTM from the stub"
        assert result["first"] != "LGTM from the stub"
        assert len(stub.requests) == 1
        assert "review it" in stub.requests[0]["messages"][-1]["content"]


async def test_refflow_rate_limited(tmp_path: Path) -> None:
    user = tmp_path / "user"
    user.mkdir()
    (user / "fail.py").write_text(FAIL, encoding="utf-8")
    with StubServer([refuse()]) as stub:
        fx = await _boot(tmp_path, _toml(stub, "openai"))
        try:
            await run_turn(fx.wire, fx.chat, "t1", "/fail")
        finally:
            await fx.shut()
    (run,) = (tmp_path / "state" / "runs").iterdir()
    kinds = [a.kind for a in load_journal(tmp_path / "state", run.name).agents]
    assert kinds == ["rate_limited"]


def test_refflow_readme_example_parses(tmp_path: Path) -> None:
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    section = text[text.index("## An OpenAI-compatible endpoint") :]
    block = re.search(r"```toml\n(.*?)```", section, re.DOTALL)
    assert block is not None
    config = parse_config(tomllib.loads(block.group(1)), base=tmp_path)
    assert config.providers["local"].load == "ahp_host_acp:from_config"
    rule = config.routing.rules[0]
    assert (rule.provider, rule.phase) == ("local", "Review")
