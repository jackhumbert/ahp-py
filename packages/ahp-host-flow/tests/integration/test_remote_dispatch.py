"""Remote (direct) providers: `agent()` calls run on another host over ahp-client (plan §8 M7)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path

from ahp_host import Host, LoopbackSingleUserPolicy
from ahp_protocol.transport import Transport, memory_pair

from ahp_host_flow.config import ProviderConfig
from ahp_host_flow.engine import load_journal
from tests.flow_fakes import StrippedEchoProvider
from tests.flow_hosting import SESSION, boot_flow, final_json
from tests.hosting import all_channels, run_turn, state, text_of, worker_chats

# A placeholder: the in-memory dialer below never dials it.
TOML = """
[providers.far]
url = "wss://my-mac-mini.example.com"
agent = "claude"
"""
WORKFLOW = (
    "meta = {'name': 'far', 'description': 'd'}\n"
    "\n"
    "async def main(args):\n"
    "    return await agent('hello', label='Far')\n"
)


async def test_remote_dispatch_runs_a_call_on_another_host(tmp_path: Path) -> None:
    user = tmp_path / "user"
    user.mkdir()
    (user / "far.py").write_text(WORKFLOW, encoding="utf-8")
    far = Host(
        StrippedEchoProvider(
            provider_id="claude", structured=True, capabilities={"multipleChats": {}}
        ),
        LoopbackSingleUserPolicy(),
    )
    serving: set[asyncio.Task[None]] = set()

    def dialer(spec: ProviderConfig) -> Callable[[], Awaitable[Transport]]:
        assert spec.url == "wss://my-mac-mini.example.com"

        async def dial() -> Transport:
            client_end, server_end = memory_pair()
            task = asyncio.create_task(far.serve(server_end))
            serving.add(task)
            task.add_done_callback(serving.discard)
            return client_end

        return dial

    fx = await boot_flow(tmp_path, "node", TOML, provider_options={"remote_dialer": dialer})
    try:
        turn = await run_turn(fx.wire, fx.chat, "t1", "/far")
        assert final_json(turn) == "You said: hello"
        remote = [u for u in all_channels(far) if "defaultChat" in state(far, u)]
        assert len(remote) == 1
        chats = state(far, remote[0])["chats"]
        assert "Far" in [c.get("title") for c in chats]
        (worker,) = worker_chats(fx.host, SESSION)
        await fx.wire.request("subscribe", {"channel": worker["resource"]})
        shown = text_of(state(fx.host, str(worker["resource"])))
        assert "You said: hello" in shown
        assert "Remote session: " in shown
        (run,) = (tmp_path / "state" / "runs").iterdir()
        (started,) = load_journal(tmp_path / "state", run.name).started
        assert started.session == remote[0]
    finally:
        await fx.shut()
        for task in list(serving):
            task.cancel()
