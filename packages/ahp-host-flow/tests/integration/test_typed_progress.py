"""Typed progress (plan §4.7) over a real in-memory host: what a run publishes on its main chat.

Descriptors ride in `_meta["ahp-py/flow"]`; the text stays what a stock client renders.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import ahp_client
import pytest
from ahp_client import TurnCompleted
from ahp_protocol.ext import flow
from ahp_protocol.transport import memory_pair
from jsonschema import Draft202012Validator

from tests.flow_hosting import SESSION, FlowFixture, boot_flow, descriptors, final_json
from tests.hosting import found, run_turn, start_turn, state, text_of, wait_turn, worker_chats

pytestmark = pytest.mark.node

TOML = """
[providers.echo]
load = "ahp_host.provider.echo:EchoProvider"

[providers.echo_c]
load = "ahp_host.provider.echo:EchoProvider"
config = { confirm_tools = true }

[providers.echo_s]
load = "ahp_host.provider.echo:EchoProvider"
config = { structured = true }

[providers.hang]
load = "tests.flow_fakes:HangingProvider"

[routing]
default = "echo"
rules = [
  { provider = "echo_c", label = "confirm*" },
  { provider = "echo_s", label = "srt*" },
  { provider = "hang", label = "slow*" },
]
"""

TWO_PHASES = """```js
phase('A');
const xs = await parallel([() => agent('x', {label: 'x'}), () => agent('y', {label: 'y'})]);
phase('B');
const z = await agent('z', {label: 'z'});
return {xs, z}
```"""

SCHEMA = "{type: 'object', properties: {n: {type: 'integer'}}, required: ['n']}"


async def boot(tmp_path: Path, node_path: str) -> FlowFixture:
    return await boot_flow(tmp_path, node_path, TOML)


def notification_parts(turn: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [
        part
        for part in found(turn, lambda m: m.get("kind") == "systemNotification")
        if descriptors(part)
    ]


def notification_kinds(turn: Mapping[str, Any]) -> list[str]:
    return [next(iter(descriptors(part)[0])) for part in notification_parts(turn)]


def agent_calls(turn: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """The turn's `agent` tool calls, once each, by ordinal."""
    calls = {
        str(m["toolCallId"]): m
        for m in found(turn, lambda m: m.get("toolName") == "agent" and "toolCallId" in m)
    }
    return sorted(calls.values(), key=lambda c: descriptors(c)[0]["stepStarted"]["ordinal"])


def folded(fx: FlowFixture) -> flow.RunView:
    return flow.run_view(state(fx.host, fx.chat))


def summary(view: flow.RunView) -> tuple[Any, ...]:
    return (
        view.status,
        [p.title for p in view.phases],
        [(s.ordinal, s.status, s.result, s.chat) for s in view.steps],
    )


async def test_typed_progress_descriptors(tmp_path: Path, node_path: str) -> None:
    fx = await boot(tmp_path, node_path)
    try:
        turn = await run_turn(fx.wire, fx.chat, "t1", TWO_PHASES)
        assert turn["state"] == "complete"
        assert notification_kinds(turn) == [
            "runStarted",
            "phaseStarted",
            "phaseFinished",
            "phaseStarted",
            "phaseFinished",
            "runFinished",
        ]
        validator = Draft202012Validator(flow.DESCRIPTOR_SCHEMA)
        for descriptor in descriptors(turn):
            validator.validate(descriptor)
        (run_dir,) = (tmp_path / "state" / "runs").iterdir()
        started = descriptors(notification_parts(turn)[0])[0]["runStarted"]
        assert started["runId"] == run_dir.name
        workers = {str(c["resource"]) for c in worker_chats(fx.host, SESSION)}
        calls = agent_calls(turn)
        assert len(calls) == 3
        for call, phase, prompt in zip(calls, "AAB", "xyz", strict=True):
            descriptor = descriptors(call)[0]
            begun = descriptor["stepStarted"]
            assert begun["chat"] in workers
            assert begun["provider"] == "echo"
            assert begun["phase"] == phase
            done = descriptor["stepFinished"]
            assert done["status"] == "ok"
            assert done["result"] == f"You said: {prompt}"
            assert isinstance(done["usage"]["inputTokens"], int)
            assert isinstance(done["usage"]["outputTokens"], int)
    finally:
        await fx.shut()


async def test_typed_progress_run_view_from_host_state(tmp_path: Path, node_path: str) -> None:
    fx = await boot(tmp_path, node_path)
    try:
        await run_turn(fx.wire, fx.chat, "t1", TWO_PHASES)
        view = folded(fx)
        assert view.status == "ok"
        assert [p.title for p in view.phases] == ["A", "B"]
        assert all(p.status == "finished" for p in view.phases)
        assert [(s.ordinal, s.status, s.result) for s in view.steps] == [
            (0, "ok", "You said: x"),
            (1, "ok", "You said: y"),
            (2, "ok", "You said: z"),
        ]
        assert {s.chat for s in view.steps} == {
            str(c["resource"]) for c in worker_chats(fx.host, SESSION)
        }
    finally:
        await fx.shut()


async def test_typed_progress_flow_view_over_client(tmp_path: Path, node_path: str) -> None:
    fx = await boot(tmp_path, node_path)
    try:
        client_t, server_t = memory_pair()
        serving = asyncio.create_task(fx.host.serve(server_t))
        try:
            async with (
                ahp_client.connect(transport=client_t) as client,
                await client.create_session(
                    provider="flow", working_directories=[fx.workspace.as_uri()]
                ) as session,
            ):
                async for event in session.prompt(TWO_PHASES):
                    if isinstance(event, TurnCompleted):
                        break
                await session.chat()
                client_view = session.flow_view()
                host_chat = str(state(fx.host, session.uri)["defaultChat"])
                host_view = flow.run_view(state(fx.host, host_chat))
        finally:
            serving.cancel()
        assert client_view.status == "ok"
        assert len(client_view.steps) == 3
        assert summary(client_view) == summary(host_view)
    finally:
        await fx.shut()


async def test_typed_progress_stock_text(tmp_path: Path, node_path: str) -> None:
    fx = await boot(tmp_path, node_path)
    try:
        turn = await run_turn(fx.wire, fx.chat, "t1", TWO_PHASES)
        texts = [part.get("content") for part in notification_parts(turn)]
        assert all(isinstance(t, str) and t for t in texts)
        assert str(texts[0]).startswith("Run ")
        assert texts[1:] == [
            "Phase: A",
            "Phase finished: A",
            "Phase: B",
            "Phase finished: B",
            "Workflow finished",
        ]
        for call in agent_calls(turn):
            assert call.get("displayName")
            assert call.get("pastTenseMessage")
        assert "```json" in text_of(turn)
    finally:
        await fx.shut()


async def test_typed_progress_awaiting_input(tmp_path: Path, node_path: str) -> None:
    fx = await boot(tmp_path, node_path)
    try:
        script = "```js\nreturn await agent('approve me', {label: 'confirm-1'})\n```"
        await start_turn(fx.wire, fx.chat, "t1", script)

        def awaiting() -> bool:
            steps = folded(fx).steps
            return len(steps) == 1 and steps[0].awaiting

        assert await fx.wire.until(awaiting), state(fx.host, fx.chat)
        assert state(fx.host, SESSION).get("inputNeeded")
        assert await fx.wire.until(lambda: len(worker_chats(fx.host, SESSION)) == 1)
        worker = str(worker_chats(fx.host, SESSION)[0]["resource"])
        await fx.wire.request("subscribe", {"channel": worker})

        def pending() -> list[Mapping[str, Any]]:
            active = state(fx.host, worker).get("activeTurn") or {}
            return found(active, lambda m: m.get("status") == "pending-confirmation")

        assert await fx.wire.until(lambda: bool(pending()))
        await fx.wire.dispatch(
            worker,
            {
                "type": "chat/toolCallConfirmed",
                "turnId": state(fx.host, worker)["activeTurn"]["id"],
                "toolCallId": pending()[0].get("toolCallId", "echo-tool-1"),
                "approved": True,
                "confirmed": "user-action",
            },
        )
        turn = await wait_turn(fx.wire, fx.chat, "t1")
        assert final_json(turn) == "You said: approve me"
        (step,) = folded(fx).steps
        assert step.status == "ok"
        assert step.awaiting is False
    finally:
        await fx.shut()


async def test_typed_progress_skip_and_error(tmp_path: Path, node_path: str) -> None:
    fx = await boot(tmp_path, node_path)
    try:
        script = f"""```js
const [a, b] = await parallel([
  () => agent('wait', {{label: 'slow-1'}}),
  () => agent('{{"n": "three"}}', {{label: 'srt-x', schema: {SCHEMA}}}),
]);
return {{a, b}}
```"""
        await start_turn(fx.wire, fx.chat, "t1", script)

        def slow_chat() -> str | None:
            return next((s.chat for s in folded(fx).steps if s.ordinal == 0), None)

        assert await fx.wire.until(lambda: slow_chat() is not None)
        worker = str(slow_chat())
        await fx.wire.request("subscribe", {"channel": worker})
        assert await fx.wire.until(lambda: bool(state(fx.host, worker).get("activeTurn")))
        await fx.wire.dispatch(
            worker,
            {"type": "chat/turnCancelled", "turnId": state(fx.host, worker)["activeTurn"]["id"]},
        )
        await wait_turn(fx.wire, fx.chat, "t1")
        view = folded(fx)
        assert view.status == "ok"
        assert [(s.ordinal, s.status) for s in view.steps] == [(0, "skipped"), (1, "error")]
    finally:
        await fx.shut()


async def test_typed_progress_script_error(tmp_path: Path, node_path: str) -> None:
    fx = await boot(tmp_path, node_path)
    try:
        script = "```js\nphase('P'); await agent('a'); throw new Error('nope')\n```"
        turn = await run_turn(fx.wire, fx.chat, "t1", script)
        assert turn["state"] == "error"
        assert notification_kinds(turn) == [
            "runStarted",
            "phaseStarted",
            "phaseFinished",
            "runFinished",
        ]
        parts = descriptors(turn)
        phase_end = [d for d in parts if "phaseFinished" in d]
        assert phase_end[-1]["phaseFinished"]["title"] == "P"
        end = [d for d in parts if "runFinished" in d][-1]["runFinished"]
        assert end["status"] == "error"
        assert "nope" in end["error"]
        assert folded(fx).status == "error"
    finally:
        await fx.shut()
