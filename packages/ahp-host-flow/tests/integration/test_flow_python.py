"""`.py` workflows as slash commands: the catalog, the trust rules and cross-language calls."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from ahp_host import Host

from ahp_host_flow.engine import load_journal
from tests.flow_hosting import SESSION, boot_flow, final_json
from tests.hosting import found, notifications, run_turn, state, text_of

pytestmark = pytest.mark.node

TOML = '[providers.echo]\nload = "ahp_host.provider.echo:EchoProvider"\n'
REVIEW = (
    "meta = {'name': 'review', 'description': 'd', 'phases': [{'title': 'Find'}]}\n"
    "\n"
    "async def main(args):\n"
    "    phase('Find')\n"
    "    return {'reply': await agent(f\"hi {args['who']}\")}\n"
)
WS_PY = "meta = {'name': 'ws', 'description': 'd'}\n\nasync def main(args):\n    return 'py'\n"
WS_PATH = ".claude/workflows/ws.py"
TRUSTED = '\n[workflows]\npython_sources = ["workspace", "user", "host"]\n'


def containers(host: Host) -> list[dict[str, Any]]:
    return list(state(host, SESSION).get("customizations", []))


def user_container(host: Host) -> list[dict[str, Any]]:
    return [c for c in containers(host) if c["id"] == "ahp-host-flow.user" and c["children"]]


def write(directory: Path, filename: str, body: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / filename).write_text(body, encoding="utf-8")


async def test_flow_python_slash_command(tmp_path: Path, node_path: str) -> None:
    write(tmp_path / "user", "review.py", REVIEW)
    fx = await boot_flow(tmp_path, node_path, TOML)
    try:
        # The (empty) workspace container is published too, so pick the user one by id.
        assert await fx.wire.until(lambda: bool(user_container(fx.host))), json.dumps(
            containers(fx.host)
        )
        (container,) = user_container(fx.host)
        (child,) = container["children"]
        assert child["_meta"]["ahp-py/flow"] == {
            "kind": "workflow",
            "phases": [{"title": "Find"}],
            "source": "user",
            "language": "python",
        }
        turn = await run_turn(fx.wire, fx.chat, "t1", '/review {"who": "bob"}')
        assert final_json(turn) == {"reply": "You said: hi bob"}
        assert "Phase: Find" in notifications(turn)
        assert found(turn, lambda m: m.get("type") == "subagent")
        (run,) = (tmp_path / "state" / "runs").iterdir()
        header = load_journal(tmp_path / "state", run.name).header
        assert header.filename.endswith("review.py")
        assert header.source == REVIEW
    finally:
        await fx.shut()


async def test_flow_python_workspace_untrusted(tmp_path: Path, node_path: str) -> None:
    workflows = tmp_path / "ws" / ".claude" / "workflows"
    write(workflows, "ws.py", WS_PY)
    write(
        workflows,
        "viaws.js",
        "try { return await workflow({scriptPath: '" + WS_PATH + "'}) }\n"
        "catch (e) { return String(e.message) }\n",
    )
    fx = await boot_flow(tmp_path, node_path, TOML)
    try:
        assert await fx.wire.until(lambda: len(containers(fx.host)) == 1), json.dumps(
            containers(fx.host)
        )
        listed = json.dumps(containers(fx.host))
        assert "viaws" in listed
        assert "ws.py" not in listed
        assert '"ws"' not in listed
        turn = await run_turn(fx.wire, fx.chat, "t1", "/ws")
        assert text_of(turn).startswith("Unknown workflow: /ws.")
        result = final_json(await run_turn(fx.wire, fx.chat, "t2", "/viaws"))
        assert f"Python workflows are not allowed from '{WS_PATH}'" in result
    finally:
        await fx.shut()


async def test_flow_python_workspace_trusted(tmp_path: Path, node_path: str) -> None:
    write(tmp_path / "ws" / ".claude" / "workflows", "ws.py", WS_PY)
    fx = await boot_flow(tmp_path, node_path, TOML + TRUSTED)
    try:
        assert await fx.wire.until(lambda: len(containers(fx.host)) == 1), json.dumps(
            containers(fx.host)
        )
        assert final_json(await run_turn(fx.wire, fx.chat, "t1", "/ws")) == "py"
    finally:
        await fx.shut()


async def test_flow_python_workspace_trust_is_the_workflows_dir_only(
    tmp_path: Path, node_path: str
) -> None:
    # `workspace` in python_sources trusts `.claude/workflows`, not the whole repository.
    write(tmp_path / "ws", "stray.py", WS_PY)
    write(tmp_path / "ws" / ".claude" / "workflows", "ws.py", WS_PY)
    write(
        tmp_path / "ws" / ".claude" / "workflows",
        "viastray.js",
        "try { return await workflow({scriptPath: 'stray.py'}) }\n"
        "catch (e) { return String(e.message) }\n",
    )
    fx = await boot_flow(tmp_path, node_path, TOML + TRUSTED)
    try:
        assert final_json(await run_turn(fx.wire, fx.chat, "t1", "/ws")) == "py"
        result = final_json(await run_turn(fx.wire, fx.chat, "t2", "/viastray"))
        assert "Python workflows are not allowed from 'stray.py'" in result
    finally:
        await fx.shut()


async def test_flow_python_sources_cannot_grant_an_excluded_workspace(
    tmp_path: Path, node_path: str
) -> None:
    # sources leaves the workspace out, so python_sources naming it grants nothing.
    write(tmp_path / "ws" / ".claude" / "workflows", "ws.py", WS_PY)
    write(
        tmp_path / "user",
        "viaws.js",
        "try { return await workflow({scriptPath: '" + WS_PATH + "'}) }\n"
        "catch (e) { return String(e.message) }\n",
    )
    body = (
        TOML + '\n[workflows]\nsources = ["user", "host"]\npython_sources = ["workspace", "user"]\n'
    )
    fx = await boot_flow(tmp_path, node_path, body)
    try:
        result = final_json(await run_turn(fx.wire, fx.chat, "t1", "/viaws"))
        assert f"Python workflows are not allowed from '{WS_PATH}'" in result
    finally:
        await fx.shut()


async def test_flow_python_mixed(tmp_path: Path, node_path: str) -> None:
    write(
        tmp_path / "user",
        "mix.py",
        "async def main(args):\n"
        "    a = await agent('a')\n"
        "    r = await workflow('jchild')\n"
        "    return [a, r, await agent('c')]\n",
    )
    write(tmp_path / "user", "jchild.js", "return await agent('b')\n")
    fx = await boot_flow(tmp_path, node_path, TOML)
    try:
        turn = await run_turn(fx.wire, fx.chat, "t1", "/mix")
        assert final_json(turn) == ["You said: a", "You said: b", "You said: c"]
        (run,) = (tmp_path / "state" / "runs").iterdir()
        journal = load_journal(tmp_path / "state", run.name)
        assert sorted(r.ordinal for r in journal.agents) == [0, 1, 2]
    finally:
        await fx.shut()
