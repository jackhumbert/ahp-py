"""The subagent preamble reaches only what the inner agent is sent (plan §4.4)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from ahp_host import Host, LoopbackSingleUserPolicy
from ahp_host.provider.echo import EchoProvider
from ahp_protocol.transport import Transport, memory_pair

from ahp_host_flow.config import ProviderConfig
from ahp_host_flow.engine import DEFAULT_PREAMBLE, load_journal
from tests import flow_fakes
from tests.fleet_fakes import FakeFleet, join_fake_gateway
from tests.flow_fakes import RecordingProvider
from tests.flow_hosting import SESSION, FlowFixture, boot_flow
from tests.hosting import all_channels, found, notifications, run_turn, state, worker_chats

REC = '[providers.rec]\nload = "tests.flow_fakes:RecordingProvider"\n{extra}'
FLEET = '[providers.alpha]\nvia = "fleet"\nnode = "alpha"\nagent = "claude"\n{extra}'
REMOTE = '[providers.far]\nurl = "wss://my-mac-mini.example.com"\nagent = "claude"\n{extra}'
SCHEMA = "{'type': 'object', 'properties': {'n': {'type': 'integer'}}, 'required': ['n']}"


def flow(name: str, body: str) -> str:
    return f"meta = {{'name': {name!r}, 'description': 'd'}}\n\nasync def main(args):\n{body}"


def put(base: Path, name: str, body: str) -> None:
    user = base / "user"
    user.mkdir(parents=True, exist_ok=True)
    (user / f"{name}.py").write_text(flow(name, body), encoding="utf-8")


@pytest.fixture(autouse=True)
def _fakes() -> Iterator[None]:
    flow_fakes.reset_fakes()
    yield
    flow_fakes.reset_fakes()


async def worker_message(fx: FlowFixture) -> dict[str, Any]:
    """The first turn's message of the one worker chat."""
    (worker,) = worker_chats(fx.host, SESSION)
    await fx.wire.request("subscribe", {"channel": worker["resource"]})
    return dict(state(fx.host, str(worker["resource"]))["turns"][0]["message"])


async def test_preamble_inprocess_default_text(tmp_path: Path) -> None:
    put(tmp_path, "one", "    return await agent('hi')\n")
    fx = await boot_flow(tmp_path, "no-node", REC.format(extra="preamble = true\n"))
    try:
        await run_turn(fx.wire, fx.chat, "t1", "/one")
        (rec,) = RecordingProvider.instances
        (message,) = rec.sessions[0].received
        assert message.text == DEFAULT_PREAMBLE + "\n\nhi"
        assert message.raw["text"] == message.text
        assert (await worker_message(fx))["text"] == "hi"
        (call,) = found(state(fx.host, fx.chat), lambda m: m.get("toolName") == "agent")
        tool_input = call["toolInput"]
        if isinstance(tool_input, str):
            tool_input = json.loads(tool_input)
        assert tool_input["prompt"] == "hi"
    finally:
        await fx.shut()


@pytest.mark.parametrize(
    ("extra", "sent"),
    [("preamble = false\n", "hi"), ('preamble = "Be terse."\n', "Be terse.\n\nhi")],
)
async def test_preamble_inprocess_off_and_custom(tmp_path: Path, extra: str, sent: str) -> None:
    put(tmp_path, "one", "    return await agent('hi')\n")
    fx = await boot_flow(tmp_path, "no-node", REC.format(extra=extra))
    try:
        await run_turn(fx.wire, fx.chat, "t1", "/one")
        (rec,) = RecordingProvider.instances
        assert [m.text for m in rec.sessions[0].received] == [sent]
    finally:
        await fx.shut()


async def test_preamble_structured_fallback_after_prompt(tmp_path: Path) -> None:
    put(tmp_path, "one", f"    return await agent('hi', schema={SCHEMA})\n")
    fx = await boot_flow(tmp_path, "no-node", REC.format(extra="preamble = true\n"))
    try:
        await run_turn(fx.wire, fx.chat, "t1", "/one")
        (rec,) = RecordingProvider.instances
        text = rec.sessions[0].received[0].text
        assert text.startswith(DEFAULT_PREAMBLE + "\n\nhi")
        assert len(text) > len(DEFAULT_PREAMBLE + "\n\nhi")
    finally:
        await fx.shut()


async def test_preamble_fleet_start_session_prompt(tmp_path: Path) -> None:
    put(tmp_path, "one", "    return await agent('one')\n")
    fx = await boot_flow(tmp_path, "no-node", FLEET.format(extra="preamble = true\n"))
    fleet = FakeFleet()
    client = await join_fake_gateway(fx.host, SESSION, fleet)
    try:
        assert await fx.wire.until(lambda: bool(state(fx.host, SESSION).get("activeClients")))
        await asyncio.sleep(0.1)
        await run_turn(fx.wire, fx.chat, "t1", "/one")
        assert fleet.starts[0]["prompt"] == DEFAULT_PREAMBLE + "\n\none"
        assert (await worker_message(fx))["text"] == "one"
    finally:
        await client.aclose()
        await fx.shut()


async def test_preamble_remote_chat_prompt(tmp_path: Path) -> None:
    put(tmp_path, "far", "    return await agent('hello', label='Far')\n")
    far = Host(
        EchoProvider(provider_id="claude", structured=True, capabilities={"multipleChats": {}}),
        LoopbackSingleUserPolicy(),
    )
    serving: set[asyncio.Task[None]] = set()

    def dialer(spec: ProviderConfig) -> Callable[[], Awaitable[Transport]]:
        async def dial() -> Transport:
            client_end, server_end = memory_pair()
            task = asyncio.create_task(far.serve(server_end))
            serving.add(task)
            task.add_done_callback(serving.discard)
            return client_end

        return dial

    fx = await boot_flow(
        tmp_path,
        "no-node",
        REMOTE.format(extra="preamble = true\n"),
        provider_options={"remote_dialer": dialer},
    )
    try:
        await run_turn(fx.wire, fx.chat, "t1", "/far")
        (uri,) = [u for u in all_channels(far) if "defaultChat" in state(far, u)]
        (chat,) = [c for c in state(far, uri)["chats"] if c.get("title") == "Far"]
        turns = state(far, str(chat["resource"]))["turns"]
        assert turns[0]["message"]["text"] == DEFAULT_PREAMBLE + "\n\nhello"
        assert (await worker_message(fx))["text"] == "hello"
    finally:
        await fx.shut()
        for task in list(serving):
            task.cancel()


async def test_preamble_journal_keys_unchanged(tmp_path: Path) -> None:
    keys: list[list[tuple[int, str]]] = []
    for name, extra in (("off", "preamble = false\n"), ("on", "preamble = true\n")):
        base = tmp_path / name
        base.mkdir()
        put(base, "two", "    a = await agent('one')\n    return [a, await agent('two')]\n")
        fx = await boot_flow(base, "no-node", REC.format(extra=extra))
        try:
            turn = await run_turn(fx.wire, fx.chat, "t1", "/two")
            (line,) = [n for n in notifications(turn) if n.startswith("Run ")]
            journal = load_journal(base / "state", line.removeprefix("Run "))
            keys.append([(r.ordinal, r.key) for r in journal.agents])
        finally:
            await fx.shut()
    assert keys[0] == keys[1]
    assert len(keys[0]) == 2


async def test_preamble_only_on_the_first_message_of_a_named_chat(tmp_path: Path) -> None:
    put(
        tmp_path,
        "pair",
        "    a = await agent('one', x={'chat': 'pair'})\n"
        "    b = await agent('two', x={'chat': 'pair'})\n"
        "    return [a, b]\n",
    )
    fx = await boot_flow(tmp_path, "no-node", REC.format(extra="preamble = true\n"))
    try:
        await run_turn(fx.wire, fx.chat, "t1", "/pair")
        (rec,) = RecordingProvider.instances
        (session,) = rec.sessions
        first, follow_up = session.received
        assert first.text == DEFAULT_PREAMBLE + "\n\none"
        assert first.raw["text"] == first.text
        # The inner session already has the preamble: the follow-up is the bare prompt.
        assert follow_up.text == "two"
        assert follow_up.raw["text"] == "two"
    finally:
        await fx.shut()
