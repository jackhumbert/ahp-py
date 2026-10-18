"""A run on the flow node drives calls on other nodes through an in-memory gateway (plan §8 M7)."""

from __future__ import annotations

import asyncio
from pathlib import Path

from ahp_client import TurnCompleted, TurnFailed

from tests.fleet_hosting import (
    FleetFixture,
    active_count,
    boot_fleet,
    chat_state,
    final_json_of,
    messages_of,
    session_uris,
    start_run,
)
from tests.hosting import notifications

RUN = "flow:/run-1"
SCHEMA = "{'type': 'object', 'properties': {'n': {'type': 'integer'}}, 'required': ['n']}"


def workflow(name: str, body: str) -> str:
    return f"meta = {{'name': {name!r}, 'description': 'd'}}\n\nasync def main(args):\n{body}"


WORKFLOWS = {
    "fan": workflow(
        "fan",
        "    return await parallel([\n"
        "        lambda: agent('one', label='A'),\n"
        "        lambda: agent('two', label='B'),\n"
        "    ])\n",
    ),
    "good": workflow(
        "good", f"    return await agent('{{\"n\": 4}}', label='A', schema={SCHEMA})\n"
    ),
    "bad": workflow(
        "bad", f"    return await agent('{{\"n\": \"x\"}}', label='A', schema={SCHEMA})\n"
    ),
    "three": workflow(
        "three",
        "    return await parallel([\n"
        "        lambda: agent('x', label='A'),\n"
        "        lambda: agent('y', label='A'),\n"
        "        lambda: agent('z', label='A'),\n"
        "    ])\n",
    ),
    "locked": workflow("locked", "    return await agent('secret', label='V')\n"),
}


async def boot(base: Path, **options: object) -> FleetFixture:
    fx = await boot_fleet(base, gateway_options=options)
    for name, source in WORKFLOWS.items():
        (base / "user" / f"{name}.py").write_text(source, encoding="utf-8")
    return fx


def run_turn(fx: FleetFixture) -> dict[str, object]:
    """The flow run's last turn, as its host holds it."""
    turns = chat_state(fx.hosts["flow"], RUN)["turns"]
    return dict(turns[-1])


async def test_fleet_e2e_run_fans_out_to_two_nodes(tmp_path: Path) -> None:
    fx = await boot(tmp_path)
    try:
        async with fx.surface() as surface:
            end = await start_run(surface, RUN, "/fan")
            assert isinstance(end, TurnCompleted), end
            assert final_json_of(end) == ["You said: one", "You said: two"]
            (on_alpha,) = session_uris(fx.hosts["alpha"], "claude")
            (on_beta,) = session_uris(fx.hosts["beta"], "claude")
            assert messages_of(fx.hosts["alpha"], on_alpha) == ["one"]
            assert messages_of(fx.hosts["beta"], on_beta) == ["two"]
            async with asyncio.timeout(10):
                listed = {item["resource"] for item in (await surface.sessions())["items"]}
            assert {on_alpha, on_beta, RUN} <= listed
        orchestrator = fx.gateway.orchestrator
        assert orchestrator is not None
        adopted = orchestrator._adopted[RUN]
        assert adopted.kind == "flow"
        assert set(adopted.children) == {on_alpha, on_beta}
    finally:
        await fx.aclose()


async def test_fleet_e2e_run_structured_results_are_validated(tmp_path: Path) -> None:
    fx = await boot(tmp_path)
    try:
        async with fx.surface() as surface:
            good = await start_run(surface, RUN, "/good")
            assert isinstance(good, TurnCompleted), good
            assert final_json_of(good) == {"n": 4}
            bad = await start_run(surface, "flow:/run-2", "/bad")
            assert isinstance(bad, TurnCompleted), bad
            assert final_json_of(bad) is None
        turns = chat_state(fx.hosts["flow"], "flow:/run-2")["turns"]
        assert any("failed" in note for note in notifications(turns[-1]))
    finally:
        await fx.aclose()


async def test_fleet_e2e_run_max_running_bounds_concurrency(tmp_path: Path) -> None:
    fx = await boot(tmp_path, max_running=1)
    fx.alpha.gate.clear()
    peak = 0
    done = asyncio.Event()

    async def sample() -> None:
        nonlocal peak
        while not done.is_set():
            peak = max(peak, active_count(fx.hosts["alpha"], "claude"))
            await asyncio.sleep(0.01)

    async def open_gate() -> None:
        # Starting the sandboxed run takes longer than 0.2 s here, so a fixed delay from the
        # start would open the gate before any call reached the node and sample nothing. Hold
        # the gate until the first call is running on alpha, then for 0.2 s (~20 samples).
        async with asyncio.timeout(10):
            while not active_count(fx.hosts["alpha"], "claude"):
                await asyncio.sleep(0.01)
        await asyncio.sleep(0.2)
        fx.alpha.gate.set()

    sampler = asyncio.create_task(sample())
    opener = asyncio.create_task(open_gate())
    try:
        async with fx.surface() as surface:
            async with asyncio.timeout(10):
                end = await start_run(surface, RUN, "/three")
            assert isinstance(end, TurnCompleted), end
            assert sorted(final_json_of(end)) == ["You said: x", "You said: y", "You said: z"]
        assert peak == 1
    finally:
        done.set()
        await sampler
        await opener
        await fx.aclose()


async def test_fleet_e2e_run_another_user_cannot_prompt_the_run(tmp_path: Path) -> None:
    fx = await boot(tmp_path)
    try:
        async with fx.surface() as owner:
            assert isinstance(await start_run(owner, RUN, "/fan"), TurnCompleted)
        async with fx.surface("surface-2") as other:
            session = await other.open_session(RUN)
            chat = await session.chat()
            async with asyncio.timeout(10):
                refused = await chat.prompt("/fan")
        assert isinstance(refused, TurnFailed), refused
        assert "another user" in refused.reason
    finally:
        await fx.aclose()


async def test_fleet_e2e_run_a_node_not_admitted_is_refused(tmp_path: Path) -> None:
    fx = await boot(tmp_path)
    try:
        async with fx.surface() as surface:
            end = await start_run(surface, RUN, "/locked")
            assert isinstance(end, TurnCompleted), end
            assert final_json_of(end) is None
        assert session_uris(fx.hosts["vault"], "claude") == []
        assert any("no machine" in note for note in notifications(run_turn(fx)))
    finally:
        await fx.aclose()


STRICT_TOML = """
[providers.alpha]
via = "fleet"
node = "alpha"
agent = "claude"
approval_mode = "auto"
models = { quick = "model-q" }

[routing]
default = "alpha"
"""


async def boot_strict(base: Path) -> FleetFixture:
    """alpha asks for `auto`; the gateway's `child_config` is `{}`, so only plan/default pass."""
    fx = await boot_fleet(base, flow_toml_body=STRICT_TOML)
    for name, source in WORKFLOWS.items():
        (base / "user" / f"{name}.py").write_text(source, encoding="utf-8")
    return fx


async def test_fleet_e2e_run_a_refused_approval_mode_is_said_clearly(tmp_path: Path) -> None:
    fx = await boot_strict(tmp_path)
    try:
        async with fx.surface() as surface:
            end = await start_run(surface, RUN, "/good")
            assert isinstance(end, TurnCompleted), end
            assert final_json_of(end) is None
        assert session_uris(fx.hosts["alpha"], "claude") == []
        notes = notifications(run_turn(fx))
        assert any(
            "the gateway refused approval mode 'auto' for provider 'alpha' (start_session: "
            "permissionMode 'auto' is looser than this gateway allows (default)); choose a "
            "stricter one" in note
            for note in notes
        ), notes
    finally:
        await fx.aclose()


async def test_fleet_e2e_run_the_runs_model_reaches_the_node(tmp_path: Path) -> None:
    """A call naming no model runs on the run's own model selection, when the provider has it."""
    fx = await boot_strict(tmp_path)
    try:
        async with fx.surface() as surface:
            session = await surface.create_session(
                provider="flow", uri=RUN, config={"permissionMode": "plan"}
            )
            chat = await session.chat()
            async with asyncio.timeout(10):
                end = await chat.prompt("/fan", model="model-q")
            assert isinstance(end, TurnCompleted), end
        uris = session_uris(fx.hosts["alpha"], "claude")
        assert len(uris) == 2
        for uri in uris:
            turn = chat_state(fx.hosts["alpha"], uri)["turns"][-1]
            assert (turn["message"].get("model") or {}).get("id") == "model-q", turn["message"]
    finally:
        await fx.aclose()
