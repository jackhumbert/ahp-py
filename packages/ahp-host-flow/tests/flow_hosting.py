"""A booted `FlowProvider` host for the flow integration tests; not a test module."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ahp_host import Host, LoopbackSingleUserPolicy
from ahp_protocol.ext.flow import FLOW_META_KEY

from ahp_host_flow import FlowProvider, load_config
from tests.hosting import Wire, connect, found, open_session, shut, text_of

SESSION = "flow:/s1"
ECHO = "ahp_host.provider.echo:EchoProvider"


@dataclass
class FlowFixture:
    base: Path
    host: Host
    wire: Wire
    serving: asyncio.Task[None]
    chat: str
    workspace: Path

    async def shut(self) -> None:
        await shut(self.host, self.wire, self.serving)


def write_toml(base: Path, node: str, body: str) -> Path:
    """`base/flow.toml`: the state dir and node, then *body*."""
    q = json.dumps
    path = base / "flow.toml"
    path.write_text(
        f"state_dir = {q(str(base / 'state'))}\nnode = {q(node)}\n{body}", encoding="utf-8"
    )
    return path


async def boot_flow(
    base: Path,
    node: str,
    body: str,
    *,
    workspace: Path | None = None,
    session_config: Mapping[str, Any] | None = None,
) -> FlowFixture:
    workspace = workspace if workspace is not None else base / "ws"
    (workspace / ".claude" / "workflows").mkdir(parents=True, exist_ok=True)
    config = load_config(write_toml(base, node, body))
    host = Host(FlowProvider(config, user_workflows_dir=base / "user"), LoopbackSingleUserPolicy())
    wire, serving = await connect(host)
    chat = await open_session(
        wire,
        SESSION,
        "flow",
        working_directories=[workspace.as_uri()],
        config=session_config,
    )
    return FlowFixture(base, host, wire, serving, chat, workspace)


def final_json(turn: Mapping[str, Any]) -> Any:
    text = text_of(turn)
    start = text.index("```json\n") + len("```json\n")
    return json.loads(text[start : text.rindex("\n```")])


def descriptors(value: Any) -> list[Mapping[str, Any]]:
    """Every `_meta["ahp-py/flow"]` found anywhere in *value*."""
    return [
        m["_meta"][FLOW_META_KEY]
        for m in found(
            value, lambda m: isinstance(m.get("_meta"), Mapping) and FLOW_META_KEY in m["_meta"]
        )
    ]
