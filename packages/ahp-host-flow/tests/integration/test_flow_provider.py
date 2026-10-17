"""`FlowProvider` over a real in-memory host: a run as a session.

Inner providers are the echo provider and the scripted fakes in `tests/flow_fakes.py`, loaded by
import path from a flow.toml, never a live model.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest
from ahp_host import ROOT_URI, Host, LoopbackSingleUserPolicy

from ahp_host_flow import FlowProvider, load_config
from ahp_host_flow.engine import load_journal
from ahp_host_flow.provider import FlowSession
from tests.flow_fakes import GatedProvider, HangingProvider, RecordingProvider, reset_fakes
from tests.hosting import (
    Wire,
    cancel_turn,
    connect,
    flow_tasks,
    found,
    notifications,
    open_session,
    run_turn,
    shut,
    start_turn,
    state,
    text_of,
    wait_turn,
    worker_chats,
)

pytestmark = pytest.mark.node

SESSION = "flow:/s1"
ECHO = "ahp_host.provider.echo:EchoProvider"
SCHEMA = '{type: "object", properties: {n: {type: "integer"}}, required: ["n"]}'


@pytest.fixture(autouse=True)
def _reset() -> Iterator[None]:
    reset_fakes()
    yield
    reset_fakes()


def write_config(base: Path, node: str, *, host_dirs: list[Path] | None = None) -> Path:
    q = json.dumps
    text = f"""
state_dir = {q(str(base / "state"))}
node = {q(node)}

[providers.echo]
load = {q(ECHO)}

[providers.rec]
load = "tests.flow_fakes:RecordingProvider"

[providers.hang]
load = "tests.flow_fakes:HangingProvider"

[providers.gate]
load = "tests.flow_fakes:GatedProvider"

[providers.srt]
load = {q(ECHO)}
config = {{ structured = true }}

[routing]
default = "echo"
rules = [
  {{ provider = "rec", label = "rec*" }},
  {{ provider = "hang", label = "slow*" }},
  {{ provider = "srt", label = "srt*" }},
  {{ provider = "gate", label = "gate*" }},
]

[workflows]
dirs = [{", ".join(q(str(d)) for d in host_dirs or [])}]
"""
    path = base / "flow.toml"
    path.write_text(text, encoding="utf-8")
    return path


class Fixture:
    def __init__(self, base: Path, host: Host, wire: Wire, serving: asyncio.Task[None]) -> None:
        self.base = base
        self.host = host
        self.wire = wire
        self.serving = serving
        self.chat = ""
        self.workspace = base / "ws"

    async def shut(self) -> None:
        await shut(self.host, self.wire, self.serving)


async def boot(
    base: Path,
    node: str,
    *,
    host_dirs: list[Path] | None = None,
    session_config: Mapping[str, Any] | None = None,
) -> Fixture:
    (base / "ws" / ".claude" / "workflows").mkdir(parents=True, exist_ok=True)
    config = load_config(write_config(base, node, host_dirs=host_dirs))
    provider = FlowProvider(config, user_workflows_dir=base / "user")
    host = Host(provider, LoopbackSingleUserPolicy())
    wire, serving = await connect(host)
    fixture = Fixture(base, host, wire, serving)
    fixture.chat = await open_session(
        wire,
        SESSION,
        "flow",
        working_directories=[fixture.workspace.as_uri()],
        config=session_config,
    )
    return fixture


def write_workflow(directory: Path, name: str, body: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.js").write_text(body, encoding="utf-8")


def final_json(turn: Mapping[str, Any]) -> Any:
    text = text_of(turn)
    start = text.index("```json\n") + len("```json\n")
    return json.loads(text[start : text.rindex("\n```")])


async def subscribe_worker(fx: Fixture) -> str:
    """Wait for the one worker chat and subscribe to it; returns its channel."""
    assert await fx.wire.until(lambda: len(worker_chats(fx.host, SESSION)) == 1)
    worker = str(worker_chats(fx.host, SESSION)[0]["resource"])
    await fx.wire.request("subscribe", {"channel": worker})
    return worker


def containers(fx: Fixture) -> dict[str, Mapping[str, Any]]:
    return {str(c["id"]): c for c in state(fx.host, SESSION).get("customizations", [])}


async def test_customizations_and_precedence(tmp_path: Path, node_path: str) -> None:
    host_dir = tmp_path / "host"
    write_workflow(tmp_path / "ws" / ".claude" / "workflows", "greet", "return 'workspace'")
    write_workflow(tmp_path / "user", "greet", "return 'user'")
    write_workflow(host_dir, "fanout", "return 'host'")
    fx = await boot(tmp_path, node_path, host_dirs=[host_dir])
    try:
        assert await fx.wire.until(lambda: len(containers(fx)) == 3), containers(fx)
        assert set(containers(fx)) == {
            "ahp-host-flow.workspace.0",
            "ahp-host-flow.user",
            "ahp-host-flow.host",
        }
        (child,) = containers(fx)["ahp-host-flow.user"]["children"]
        assert child["_meta"]["ahp-py/flow"] == {
            "kind": "workflow",
            "phases": [],
            "source": "user",
            "shadowedBy": "workspace",
        }
        assert final_json(await run_turn(fx.wire, fx.chat, "t1", "/greet")) == "workspace"
        assert final_json(await run_turn(fx.wire, fx.chat, "t2", "/user:greet")) == "user"
        assert final_json(await run_turn(fx.wire, fx.chat, "t3", "/fanout")) == "host"
    finally:
        await fx.shut()


async def test_slash_run_with_phase_and_log(tmp_path: Path, node_path: str) -> None:
    write_workflow(
        tmp_path / "ws" / ".claude" / "workflows",
        "greet2",
        "phase('Hello'); log('starting'); return { reply: await agent(`hi ${args.who}`) }",
    )
    fx = await boot(tmp_path, node_path)
    try:
        turn = await run_turn(fx.wire, fx.chat, "t1", '/greet2 {"who": "bob"}')
        assert final_json(turn) == {"reply": "You said: hi bob"}
        notes = notifications(turn)
        assert "Phase: Hello" in notes
        assert "starting" in notes
        assert len([n for n in notes if n.startswith("Run ")]) == 1
        assert found(turn, lambda m: m.get("type") == "subagent")

        again = await run_turn(fx.wire, fx.chat, "t2", '/workflow greet2 {"who": "al"}')
        assert final_json(again) == {"reply": "You said: hi al"}
    finally:
        await fx.shut()


async def test_fenced_block(tmp_path: Path, node_path: str) -> None:
    fx = await boot(tmp_path, node_path)
    try:
        turn = await run_turn(fx.wire, fx.chat, "t1", "```js\nreturn await agent('x')\n```")
        assert final_json(turn) == "You said: x"
        with_args = await run_turn(
            fx.wire, fx.chat, "t2", '```js\nreturn args.n * 2\n```\n{"n": 21}'
        )
        assert final_json(with_args) == 42
    finally:
        await fx.shut()


async def test_help_and_unknown(tmp_path: Path, node_path: str) -> None:
    write_workflow(tmp_path / "ws" / ".claude" / "workflows", "greet", "return 1")
    fx = await boot(tmp_path, node_path)
    try:
        assert "/greet" in text_of(await run_turn(fx.wire, fx.chat, "t1", "hello"))
        unknown = text_of(await run_turn(fx.wire, fx.chat, "t2", "/nope"))
        assert unknown.startswith("Unknown workflow: /nope.")
    finally:
        await fx.shut()


async def test_models_advertised(tmp_path: Path, node_path: str) -> None:
    fx = await boot(tmp_path, node_path)
    try:
        agents = state(fx.host, ROOT_URI)["agents"]
        (flow,) = [a for a in agents if a["provider"] == "flow"]
        models = {m["id"]: m for m in flow["models"]}
        assert "echo-1" in models
        assert "vendor" not in models["echo-1"]
        assert models["rec-large"]["vendor"] == "Acme"
    finally:
        await fx.shut()


async def test_journal_written(tmp_path: Path, node_path: str) -> None:
    fx = await boot(tmp_path, node_path)
    try:
        turn = await run_turn(
            fx.wire, fx.chat, "t1", "```js\nreturn [await agent('a'), await agent('b')]\n```"
        )
        assert final_json(turn) == ["You said: a", "You said: b"]
        runs = list((tmp_path / "state" / "runs").iterdir())
        assert len(runs) == 1
        journal = load_journal(tmp_path / "state", runs[0].name)
        assert len(journal.agents) == 2
        assert all(record.status == "ok" for record in journal.agents)
    finally:
        await fx.shut()


async def test_start_record_names_the_worker_chat(tmp_path: Path, node_path: str) -> None:
    fx = await boot(tmp_path, node_path)
    try:
        turn = await run_turn(fx.wire, fx.chat, "t1", "```js\nreturn await agent('a')\n```")
        assert final_json(turn) == "You said: a"
        (run,) = (tmp_path / "state" / "runs").iterdir()
        journal = load_journal(tmp_path / "state", run.name)
        (worker,) = worker_chats(fx.host, SESSION)
        assert journal.started[0].provider == "echo"
        assert journal.started[0].chat == worker["resource"]
        assert journal.unfinished == ()
    finally:
        await fx.shut()


async def test_structured_failure_in_run(tmp_path: Path, node_path: str) -> None:
    fx = await boot(tmp_path, node_path)
    try:
        script = (
            "```js\n"
            f"return await agent('{{\"n\": \"three\"}}', {{label: 'srt-x', schema: {SCHEMA}}})\n"
            "```"
        )
        turn = await run_turn(fx.wire, fx.chat, "t1", script)
        assert final_json(turn) is None
        assert any(
            n.startswith("agent() #0 failed: No valid structured result after 3 attempt(s)")
            for n in notifications(turn)
        )
    finally:
        await fx.shut()


async def test_skip(tmp_path: Path, node_path: str) -> None:
    fx = await boot(tmp_path, node_path)
    try:
        script = (
            "```js\n"
            "const a = await agent('hold', {label: 'slow'}); "
            "const b = await agent('after'); return {a, b}\n"
            "```"
        )
        await start_turn(fx.wire, fx.chat, "t1", script)
        worker = await subscribe_worker(fx)

        def running() -> bool:
            if not HangingProvider.instances or not HangingProvider.instances[0].sessions:
                return False
            session = HangingProvider.instances[0].sessions[0]
            return session.started.is_set() and state(fx.host, worker).get("activeTurn") is not None

        assert await fx.wire.until(running)
        await cancel_turn(fx.wire, worker, state(fx.host, worker)["activeTurn"]["id"])
        turn = await wait_turn(fx.wire, fx.chat, "t1")
        assert final_json(turn) == {"a": None, "b": "You said: after"}
        assert "agent() #0 skipped" in notifications(turn)
        assert await fx.wire.until(lambda: HangingProvider.instances[0].sessions[0].closed)
        assert await fx.wire.until(lambda: not flow_tasks())
    finally:
        await fx.shut()


async def test_abort(tmp_path: Path, node_path: str) -> None:
    fx = await boot(tmp_path, node_path)
    try:
        script = "```js\nreturn await agent('hold', {label: 'slow'})\n```"
        await start_turn(fx.wire, fx.chat, "t1", script)
        worker = await subscribe_worker(fx)

        def running() -> bool:
            return bool(
                HangingProvider.instances
                and HangingProvider.instances[0].sessions
                and HangingProvider.instances[0].sessions[0].started.is_set()
            )

        assert await fx.wire.until(running)
        await cancel_turn(fx.wire, fx.chat, "t1")
        turn = await wait_turn(fx.wire, fx.chat, "t1")
        assert turn["state"] == "cancelled"
        session = HangingProvider.instances[0].sessions[0]
        assert await fx.wire.until(lambda: session.cancelled == ["run aborted"])
        assert await fx.wire.until(lambda: state(fx.host, worker).get("activeTurn") is None)
        assert await fx.wire.until(lambda: session.closed)
        assert await fx.wire.until(lambda: not flow_tasks())
    finally:
        await fx.shut()


async def test_chat_config_overlay_forwarded(tmp_path: Path, node_path: str) -> None:
    fx = await boot(tmp_path, node_path)
    try:
        script = "```js\nreturn await agent('wait-overlay', {label: 'rec-w'})\n```"
        await start_turn(fx.wire, fx.chat, "t1", script)
        worker = await subscribe_worker(fx)

        def recorded() -> bool:
            return bool(
                RecordingProvider.instances
                and RecordingProvider.instances[0].sessions
                and RecordingProvider.instances[0].sessions[0].messages
            )

        assert await fx.wire.until(recorded)
        await fx.wire.dispatch(worker, {"type": "chat/configChanged", "config": {"effort": "high"}})
        turn = await wait_turn(fx.wire, fx.chat, "t1")
        assert final_json(turn) == 'rec: wait-overlay overlay={"effort": "high"}'
        session = RecordingProvider.instances[0].sessions[0]
        assert session.overlays == [(worker, {"effort": "high"})]
    finally:
        await fx.shut()


async def test_invalid_effort_logs_one_line(tmp_path: Path, node_path: str) -> None:
    fx = await boot(tmp_path, node_path)
    try:
        script = "```js\nreturn await agent('hi', {label: 'rec-e', effort: 'turbo'})\n```"
        turn = await run_turn(fx.wire, fx.chat, "t1", script)
        assert final_json(turn) == "rec: hi"
        lines = [n for n in notifications(turn) if n.startswith("agent() #0: effort")]
        assert len(lines) == 1
        assert lines[0].startswith("agent() #0: effort 'turbo' is not one of")
        session = RecordingProvider.instances[0].sessions[0]
        assert session.context.config.get("effort") != "turbo"
    finally:
        await fx.shut()


async def test_overlay_isolation_between_workers(tmp_path: Path, node_path: str) -> None:
    fx = await boot(tmp_path, node_path)
    try:
        script = (
            "```js\n"
            "const [a, b] = await parallel([() => agent('wait-overlay a', {label: 'rec-a'}), "
            "() => agent('hold b', {label: 'rec-b'})]); return {a, b}\n"
            "```"
        )
        await start_turn(fx.wire, fx.chat, "t1", script)

        def sessions() -> list[Any]:
            return [s for p in RecordingProvider.instances for s in p.sessions]

        assert await fx.wire.until(
            lambda: len(sessions()) == 2 and all(s.messages for s in sessions())
        )
        by_title = {str(c["title"]): str(c["resource"]) for c in worker_chats(fx.host, SESSION)}
        by_chat = {s.context.chat_uri: s for s in sessions()}
        a_chat, b_chat = by_title["rec-a"], by_title["rec-b"]
        session_a, session_b = by_chat[a_chat], by_chat[b_chat]
        await fx.wire.request("subscribe", {"channel": a_chat})
        await fx.wire.dispatch(a_chat, {"type": "chat/configChanged", "config": {"effort": "high"}})
        assert await fx.wire.until(session_a.overlay_seen.is_set)
        session_b.released.set()
        turn = await wait_turn(fx.wire, fx.chat, "t1")
        assert final_json(turn) == {
            "a": 'rec: wait-overlay a overlay={"effort": "high"}',
            "b": "rec: hold b",
        }
        assert session_a.overlays == [(a_chat, {"effort": "high"})]
        assert session_b.overlays == []
        assert session_b.config_changes == []
    finally:
        await fx.shut()


async def test_pending_overlay_applied_on_registration(
    tmp_path: Path, node_path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    applied = asyncio.Event()
    original = FlowSession.chat_config_changed

    async def wrapper(self: FlowSession, chat: str, values: Mapping[str, Any]) -> None:
        await original(self, chat, values)
        applied.set()

    monkeypatch.setattr(FlowSession, "chat_config_changed", wrapper)
    fx = await boot(tmp_path, node_path)
    try:
        script = "```js\nreturn await agent('hi', {label: 'gate-1'})\n```"
        await start_turn(fx.wire, fx.chat, "t1", script)
        worker = await subscribe_worker(fx)
        assert await fx.wire.until(
            lambda: bool(GatedProvider.instances) and GatedProvider.instances[0].creating.is_set()
        )
        await fx.wire.dispatch(worker, {"type": "chat/configChanged", "config": {"effort": "high"}})
        await asyncio.wait_for(applied.wait(), timeout=10)
        GatedProvider.instances[0].release.set()
        turn = await wait_turn(fx.wire, fx.chat, "t1")
        assert final_json(turn) == 'rec: hi overlay={"effort": "high"}'
        (session,) = GatedProvider.instances[0].sessions
        assert session.overlays == [(worker, {"effort": "high"})]
    finally:
        await fx.shut()


async def test_customizations_republished_when_a_source_changes(
    tmp_path: Path, node_path: str
) -> None:
    workflows = tmp_path / "ws" / ".claude" / "workflows"
    write_workflow(workflows, "one", "return 1")
    fx = await boot(tmp_path, node_path)

    def names() -> set[str]:
        container = containers(fx).get("ahp-host-flow.workspace.0", {})
        return {str(c["name"]) for c in container.get("children", [])}

    try:
        assert await fx.wire.until(lambda: names() == {"one"}), containers(fx)
        write_workflow(workflows, "two", "return 2")
        await run_turn(fx.wire, fx.chat, "t1", "hello")
        assert await fx.wire.until(lambda: names() == {"one", "two"}), containers(fx)
    finally:
        await fx.shut()


async def test_workflow_named_workflow_runs(tmp_path: Path, node_path: str) -> None:
    write_workflow(tmp_path / "ws" / ".claude" / "workflows", "workflow", "return 'named'")
    fx = await boot(tmp_path, node_path)
    try:
        assert final_json(await run_turn(fx.wire, fx.chat, "t1", "/workflow workflow")) == "named"
    finally:
        await fx.shut()


async def test_session_config_reaches_inner(tmp_path: Path, node_path: str) -> None:
    fx = await boot(tmp_path, node_path, session_config={"permissionMode": "plan"})
    try:
        script = "```js\nreturn await agent('hi', {label: 'rec-c'})\n```"
        assert final_json(await run_turn(fx.wire, fx.chat, "t1", script)) == "rec: hi"
        session = RecordingProvider.instances[0].sessions[0]
        assert session.context.config["permissionMode"] == "plan"
    finally:
        await fx.shut()


@pytest.mark.skipif(
    not os.environ.get("AHP_FLOW_LIVE_CONFIG"), reason="AHP_FLOW_LIVE_CONFIG names no flow.toml"
)
async def test_live_smoke(tmp_path: Path) -> None:
    config = load_config(Path(os.environ["AHP_FLOW_LIVE_CONFIG"]))
    host = Host(
        FlowProvider(config, user_workflows_dir=tmp_path / "user"), LoopbackSingleUserPolicy()
    )
    wire, serving = await connect(host)
    try:
        chat = await open_session(wire, SESSION, "flow", working_directories=[tmp_path.as_uri()])
        script = (
            "```js\n"
            "const a = await agent('Reply with the single word: ping');\n"
            "const b = await agent(`Repeat this back in one word: ${a}`);\n"
            "return {a, b}\n"
            "```"
        )
        await start_turn(wire, chat, "t1", script)
        assert await wire.until(
            lambda: any(t.get("id") == "t1" for t in state(host, chat).get("turns", [])),
            timeout=300,
        )
        turn = await wait_turn(wire, chat, "t1")
        result = final_json(turn)
        assert isinstance(result["a"], str)
        assert isinstance(result["b"], str)
        assert len(worker_chats(host, SESSION)) == 2
    finally:
        await shut(host, wire, serving)
