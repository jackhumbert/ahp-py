"""`opts.x.provider` pins one `agent()` call to a configured provider, in JS and Python."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from tests import flow_fakes
from tests.flow_fakes import RecordingProvider
from tests.flow_hosting import boot_flow, final_json
from tests.hosting import notifications, run_turn

TOML = (
    '[providers.a]\nload = "tests.flow_fakes:RecordingProvider"\n'
    'config = { provider_id = "a" }\n\n'
    '[providers.b]\nload = "tests.flow_fakes:RecordingProvider"\n'
    'config = { provider_id = "b" }\n\n'
    '[routing]\ndefault = "a"\nrules = [ { model = "glm", provider = "b" } ]\n'
)
BODY = (
    "const r1 = await agent('p1'); "
    "const r2 = await agent('p2', {x: {provider: 'b'}}); "
    "const r3 = await agent('p3', {model: 'glm', x: {provider: 'a'}}); "
    "const r4 = await agent('p4', {x: {provider: 'nope'}}); "
    "return [r1, r2, r3, r4]"
)
PY = (
    "meta = {'name': 'pin', 'description': 'd'}\n"
    "\n"
    "async def main(args):\n"
    "    r1 = await agent('p1')\n"
    "    r2 = await agent('p2', x={'provider': 'b'})\n"
    "    r3 = await agent('p3', model='glm', x={'provider': 'a'})\n"
    "    r4 = await agent('p4', x={'provider': 'nope'})\n"
    "    return [r1, r2, r3, r4]\n"
)


@pytest.fixture(autouse=True)
def _fakes() -> Iterator[None]:
    flow_fakes.reset_fakes()
    yield
    flow_fakes.reset_fakes()


async def _pin(tmp_path: Path, node: str, text: str) -> None:
    fx = await boot_flow(tmp_path, node, TOML)
    try:
        turn = await run_turn(fx.wire, fx.chat, "t1", text)
        assert final_json(turn) == ["rec: p1", "rec: p2", "rec: p3", None]
        by_id = {p.provider_id: p for p in RecordingProvider.instances}
        a = [m.text for s in by_id["a"].sessions for m in s.messages]
        b = [m.text for s in by_id["b"].sessions for m in s.messages]
        assert a == ["p1", "p3"]
        assert b == ["p2"]
        assert any(
            "provider 'nope' is not configured (opts.x.provider)" in n for n in notifications(turn)
        )
    finally:
        await fx.shut()


@pytest.mark.node
async def test_flowext_provider_pin_js(tmp_path: Path, node_path: str) -> None:
    await _pin(tmp_path, node_path, "```js\n" + BODY + "\n```")


async def test_flowext_provider_pin_py(tmp_path: Path) -> None:
    user = tmp_path / "user"
    user.mkdir(parents=True, exist_ok=True)
    (user / "pin.py").write_text(PY, encoding="utf-8")
    await _pin(tmp_path, str(tmp_path / "no-node"), "/pin")
