"""`Session.workflows()` and `Session.start_workflow()` against a real flow host (plan §8 M13)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import ahp_client
from ahp_client import TurnCompleted
from ahp_protocol.transport import memory_pair

from tests.flow_hosting import SESSION, boot_flow

TOML = '[providers.rec]\nload = "tests.flow_fakes:RecordingProvider"\n'
ARGS: dict[str, Any] = {
    "type": "object",
    "properties": {"n": {"type": "integer"}},
    "required": ["n"],
}
ARGS_PY = (
    f"meta = {{'name': 'argsflow', 'description': 'd', 'x': {{'args': {ARGS!r}}}}}\n"
    "\n"
    "async def main(args):\n"
    "    return {'n': args['n'], 'r': await agent('hi', label='a')}\n"
)


async def test_startworkflow_against_flow_host(tmp_path: Path) -> None:
    user = tmp_path / "user"
    user.mkdir(parents=True, exist_ok=True)
    (user / "argsflow.py").write_text(ARGS_PY, encoding="utf-8")
    fx = await boot_flow(tmp_path, str(tmp_path / "no-node"), TOML)
    client_end, server_end = memory_pair()
    serving = asyncio.create_task(fx.host.serve(server_end))
    try:
        async with ahp_client.connect(transport=client_end, reconnect=False) as client:
            session = await client.open_session(SESSION)
            (info,) = [w for w in session.workflows() if w.qualified_name == "user:argsflow"]
            assert info.args_schema == ARGS
            stream = session.start_workflow("user:argsflow", {"n": 2})
            rid = stream.run_id
            assert rid is not None
            end = await asyncio.wait_for(stream, 20)
            assert isinstance(end, TurnCompleted)
            assert session.flow_view().run_id == rid
    finally:
        serving.cancel()
        await asyncio.gather(serving, return_exceptions=True)
        await fx.shut()
