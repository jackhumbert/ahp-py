"""Remote approvals and questions: relayed to the worker chat and answered there (plan §8 M15)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from ahp_client.client import elicitation
from ahp_host import Host, LoopbackSingleUserPolicy
from ahp_protocol.transport import Transport, memory_pair

from ahp_host_flow.config import ProviderConfig
from tests.flow_fakes import HangingProvider, ReasonEchoProvider, StrippedEchoProvider
from tests.flow_hosting import SESSION, FlowFixture, boot_flow, final_json
from tests.hosting import notifications, start_turn, state, wait_turn

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


@asynccontextmanager
async def booted(
    tmp_path: Path,
    far: Any,
    toml: str = TOML,
    *,
    workflow: str = WORKFLOW,
    hosts: list[Host] | None = None,
) -> AsyncIterator[FlowFixture]:
    """A flow host whose provider `far` is *far*, a second in-memory host (put in *hosts*)."""
    user = tmp_path / "user"
    user.mkdir()
    (user / "far.py").write_text(workflow, encoding="utf-8")
    remote = Host(far, LoopbackSingleUserPolicy())
    if hosts is not None:
        hosts.append(remote)
    serving: set[asyncio.Task[None]] = set()

    def dialer(spec: ProviderConfig) -> Callable[[], Awaitable[Transport]]:
        async def dial() -> Transport:
            client_end, server_end = memory_pair()
            task = asyncio.create_task(remote.serve(server_end))
            serving.add(task)
            task.add_done_callback(serving.discard)
            return client_end

        return dial

    fx = await boot_flow(tmp_path, "node", toml, provider_options={"remote_dialer": dialer})
    try:
        yield fx
    finally:
        await fx.shut()
        for task in list(serving):
            task.cancel()


async def worker_entry(fx: FlowFixture) -> tuple[str, Mapping[str, Any]]:
    """The worker chat and its entry in the session's `inputNeeded`, once one is shown."""

    def entries() -> list[Mapping[str, Any]]:
        needed = state(fx.host, SESSION).get("inputNeeded") or []
        return [e for e in needed if isinstance(e, Mapping) and e.get("chat") != fx.chat]

    assert await fx.wire.until(lambda: bool(entries())), state(fx.host, SESSION)
    entry = entries()[0]
    await fx.wire.request("subscribe", {"channel": str(entry["chat"])})
    return str(entry["chat"]), entry


async def test_remoteapprove_approval_finishes_the_remote_turn(tmp_path: Path) -> None:
    far = StrippedEchoProvider(provider_id="claude", confirm_tools=True)
    async with booted(tmp_path, far) as fx:
        await start_turn(fx.wire, fx.chat, "t1", "/far")
        worker, entry = await worker_entry(fx)
        await fx.wire.dispatch(
            worker,
            {
                "type": "chat/toolCallConfirmed",
                "turnId": entry["turnId"],
                "toolCallId": entry["toolCall"]["toolCallId"],
                "approved": True,
                "confirmed": "user-action",
            },
        )
        turn = await wait_turn(fx.wire, fx.chat, "t1")
        assert final_json(turn) == "You said: hello"


async def test_remoteapprove_denial_reason_reaches_the_remote_agent(tmp_path: Path) -> None:
    far = ReasonEchoProvider()
    async with booted(tmp_path, far) as fx:
        await start_turn(fx.wire, fx.chat, "t1", "/far")
        worker, entry = await worker_entry(fx)
        await fx.wire.dispatch(
            worker,
            {
                "type": "chat/toolCallConfirmed",
                "turnId": entry["turnId"],
                "toolCallId": entry["toolCall"]["toolCallId"],
                "approved": False,
                "reason": "denied",
                "reasonMessage": "use the other file",
            },
        )
        turn = await wait_turn(fx.wire, fx.chat, "t1")
        assert final_json(turn) == "denied: use the other file"


async def test_remoteapprove_question_round_trips(tmp_path: Path) -> None:
    far = StrippedEchoProvider(provider_id="claude", elicit=True)
    async with booted(tmp_path, far) as fx:
        await start_turn(fx.wire, fx.chat, "t1", "/far")
        worker, entry = await worker_entry(fx)
        assert entry["kind"] == "chatInput"
        await fx.wire.dispatch(worker, elicitation.input_completed(str(entry["id"]), "decline", {}))
        turn = await wait_turn(fx.wire, fx.chat, "t1")
        assert final_json(turn) == "(cancelled)"


def _approve(entry: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "type": "chat/toolCallConfirmed",
        "turnId": entry["turnId"],
        "toolCallId": entry["toolCall"]["toolCallId"],
        "approved": True,
        "confirmed": "user-action",
    }


async def test_remoteapprove_open_request_pauses_idle_timeout(tmp_path: Path) -> None:
    far = StrippedEchoProvider(provider_id="claude", confirm_tools=True)
    toml = TOML + "idle_timeout = 0.3\n"
    async with booted(tmp_path, far, toml) as fx:
        await start_turn(fx.wire, fx.chat, "t1", "/far")
        worker, entry = await worker_entry(fx)
        # A person takes well over idle_timeout to decide; the remote turn waits for them.
        await asyncio.sleep(1.0)
        await fx.wire.dispatch(worker, _approve(entry))
        turn = await wait_turn(fx.wire, fx.chat, "t1")
        assert final_json(turn) == "You said: hello"


async def test_remoteapprove_approval_timeout_fails_an_unanswered_request(tmp_path: Path) -> None:
    far = StrippedEchoProvider(provider_id="claude", confirm_tools=True)
    remote_host: list[Host] = []
    toml = TOML + "approval_timeout = 0.3\n"
    async with booted(tmp_path, far, toml, hosts=remote_host) as fx:
        await start_turn(fx.wire, fx.chat, "t1", "/far")
        turn = await wait_turn(fx.wire, fx.chat, "t1")
        assert final_json(turn) is None
        assert any(
            "provider 'far'" in n and "approval_timeout (0.3s)" in n for n in notifications(turn)
        ), notifications(turn)
        # The remote chat was cancelled: nothing is left running or asking there.
        (remote,) = remote_host

        def settled() -> bool:
            chats = [c for uri in list(remote._sessions) for c in remote._sessions[uri].chat_uris]
            return bool(chats) and all(
                (remote.sequencer.state_of(c) or {}).get("activeTurn") is None for c in chats
            )

        assert await fx.wire.until(settled)


async def test_remoteapprove_approval_timeout_answered_in_time(tmp_path: Path) -> None:
    far = StrippedEchoProvider(provider_id="claude", confirm_tools=True)
    async with booted(tmp_path, far, TOML + "approval_timeout = 30\n") as fx:
        await start_turn(fx.wire, fx.chat, "t1", "/far")
        worker, entry = await worker_entry(fx)
        await fx.wire.dispatch(worker, _approve(entry))
        turn = await wait_turn(fx.wire, fx.chat, "t1")
        assert final_json(turn) == "You said: hello"


async def test_remoteapprove_silent_remote_turn_still_times_out(tmp_path: Path) -> None:
    far = HangingProvider(provider_id="claude")
    toml = TOML + "idle_timeout = 0.3\n"
    async with booted(tmp_path, far, toml) as fx:
        await start_turn(fx.wire, fx.chat, "t1", "/far")
        turn = await wait_turn(fx.wire, fx.chat, "t1")
        assert final_json(turn) is None
        assert any("waited 0.3s with nothing happening" in n for n in notifications(turn))


SECRET_WORKFLOW = (
    "meta = {'name': 'far', 'description': 'd'}\n"
    "\n"
    "async def main(args):\n"
    "    return await agent('use sekrit-4242 now', label='Far')\n"
)
REDACT = "\n[redact]\npatterns = ['sekrit-[0-9]+', 'back how']\n"


async def test_remoteapprove_relayed_approval_is_masked(tmp_path: Path) -> None:
    far = StrippedEchoProvider(provider_id="claude", confirm_tools=True)
    async with booted(tmp_path, far, TOML + REDACT, workflow=SECRET_WORKFLOW) as fx:
        await start_turn(fx.wire, fx.chat, "t1", "/far")
        worker, entry = await worker_entry(fx)
        # The relayed copies: the session's entry and the call on the worker chat. (The
        # turn's message is the script's own prompt, published as written.)
        parts = state(fx.host, worker)["activeTurn"]["responseParts"]
        shown = str(entry) + str(parts)
        assert "sekrit-4242" not in shown
        assert "[redacted]" in str(entry["toolCall"])
        assert "[redacted]" in str(parts)
        await fx.wire.dispatch(worker, _approve(entry))
        turn = await wait_turn(fx.wire, fx.chat, "t1")
        assert turn is not None
        done = state(fx.host, worker)["turns"][-1]["responseParts"]
        assert "sekrit-4242" not in str(done)


async def test_remoteapprove_relayed_question_is_masked(tmp_path: Path) -> None:
    far = StrippedEchoProvider(provider_id="claude", elicit=True)
    async with booted(tmp_path, far, TOML + REDACT, workflow=SECRET_WORKFLOW) as fx:
        await start_turn(fx.wire, fx.chat, "t1", "/far")
        worker, entry = await worker_entry(fx)
        assert "back how" not in str(entry) + str(state(fx.host, worker).get("activeTurn"))
        assert "[redacted]" in str(entry)
        await fx.wire.dispatch(worker, elicitation.input_completed(str(entry["id"]), "decline", {}))
        turn = await wait_turn(fx.wire, fx.chat, "t1")
        assert final_json(turn) == "(cancelled)"
