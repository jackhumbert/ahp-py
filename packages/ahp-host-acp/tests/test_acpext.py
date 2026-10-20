"""`AgentSpec.ext_handlers`: an embedder answers the agent's `_`-prefixed requests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from ahp_acp import AcpError
from ahp_host.provider.base import UserMessage

from ahp_host_acp.agent import provider_from_options
from ahp_host_acp.client_methods import ExtRequestContext
from ahp_host_acp.config import ConfigError
from ahp_host_acp.provider import AcpProvider, AcpSession, AgentSpec
from ahp_host_acp.roots import Roots

from .fakes import FAKE_AGENT, RecordingSink, context

SECRET = "do-not-leak-this"


async def _workspace(ctx: ExtRequestContext, params: dict[str, Any]) -> dict[str, Any]:
    return {
        "cwd": str(ctx.cwd),
        "roots": [str(p) for p in ctx.roots.paths],
        "session": ctx.session_id,
        "method": ctx.method,
        "echo": params.get("value"),
    }


async def _resolve(ctx: ExtRequestContext, params: dict[str, Any]) -> dict[str, Any]:
    try:
        return {"path": str(ctx.resolve_path(params.get("path")))}
    except AcpError:
        return {"refused": True}


async def _boom(ctx: ExtRequestContext, params: dict[str, Any]) -> dict[str, Any]:
    raise RuntimeError(SECRET)


async def _session(tmp_path: Path) -> tuple[AcpSession, Path]:
    ws = tmp_path / "ws"
    ws.mkdir()
    handlers = {"_acme/workspace": _workspace, "_acme/resolve": _resolve, "_acme/boom": _boom}
    spec = AgentSpec(FAKE_AGENT, env={}, ext_handlers=handlers)
    return await AcpProvider(ws, spec).create_session(context(ws)), ws


async def _calls(session: AcpSession, items: list[dict[str, Any]]) -> list[Any]:
    sink = RecordingSink()
    await session.send_user_message(UserMessage(text="clients " + json.dumps(items)), sink)
    got: list[Any] = json.loads("".join(str(e[1]) for e in sink.events if e[0] == "text"))
    return got


async def test_ext_handler_answers(tmp_path: Path) -> None:
    session, ws = await _session(tmp_path)
    try:
        [got] = await _calls(session, [{"method": "_acme/workspace", "params": {"value": 7}}])
    finally:
        await session.aclose()
    assert got["method"] == "_acme/workspace"
    assert got["echo"] == 7
    assert Path(got["cwd"]).resolve() == ws.resolve()
    assert [Path(p).resolve() for p in got["roots"]] == [ws.resolve()]
    assert isinstance(got["session"], str)
    assert got["session"]


async def test_ext_handler_resolve_path_is_the_jail(tmp_path: Path) -> None:
    session, ws = await _session(tmp_path)
    try:
        inside, outside, relative = await _calls(
            session,
            [
                {"method": "_acme/resolve", "params": {"path": str(ws / "a.txt")}},
                {"method": "_acme/resolve", "params": {"path": str(tmp_path / "x.txt")}},
                {"method": "_acme/resolve", "params": {"path": "a.txt"}},
            ],
        )
    finally:
        await session.aclose()
    assert Path(inside["path"]) == (ws / "a.txt").resolve()
    assert outside == {"refused": True}
    assert relative == {"refused": True}


async def test_unregistered_ext_method_is_unchanged(tmp_path: Path) -> None:
    session, _ = await _session(tmp_path)
    try:
        [got] = await _calls(session, [{"method": "_acme/other", "params": {}}])
    finally:
        await session.aclose()
    assert got["error"]["code"] == -32601


async def test_ext_handler_error_is_generic(tmp_path: Path) -> None:
    session, _ = await _session(tmp_path)
    try:
        [got] = await _calls(session, [{"method": "_acme/boom", "params": {}}])
    finally:
        await session.aclose()
    assert got["error"] == {"code": -32603, "message": "extension request failed"}
    assert SECRET not in json.dumps(got)


def test_ext_handlers_option_is_validated(tmp_path: Path) -> None:
    roots = Roots((tmp_path,))
    provider = provider_from_options(
        {"command": ["agent"], "ext_handlers": {"_acme/workspace": _workspace}}, roots, tmp_path
    )
    assert isinstance(provider, AcpProvider)
    for bad in ({"fs/read": _workspace}, {"_acme/x": "not callable"}, ["_acme/x"]):
        with pytest.raises(ConfigError):
            provider_from_options({"command": ["agent"], "ext_handlers": bad}, roots, tmp_path)
