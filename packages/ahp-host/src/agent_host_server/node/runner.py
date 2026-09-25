"""Serve every agent a machine runs from one AHP host.

    python -m agent_host_server.node --config ~/.config/agent-host/node.toml
    agent-host-node --config ~/.config/agent-host/node.toml

One process, one port, one folder tree and one session store for the machine;
the agents are listed in the config's `[[agents]]` tables (see
`agent_host_server.node.config`). Each `type` names an installed agent package,
found through the `agent_host_server.agents` entry-point group:

    # the agent package's pyproject.toml
    [project.entry-points."agent_host_server.agents"]
    claude = "agent_host_server_claude.agent:create"

The factory is called as ``create(options, node)`` -- the agent's table without
`type`, and a `NodeContext` -- and returns an `AgentProvider`, or an awaitable
of one. Binds loopback unless told otherwise. The token is read from a file,
never taken on the command line, so it stays out of process listings and logs.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import inspect
import logging
import os
import signal
import sys
from dataclasses import dataclass
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

from agent_host_server.core import Host, HostInfo, LoopbackSingleUserPolicy
from agent_host_server.core.resources import ResourceProvider, RootedFilesystemResourceProvider
from agent_host_server.core.store import FileSessionStore
from agent_host_server.node.config import ConfigError, NodeSettings, load
from agent_host_server.node.roots import NamedRootsResourceProvider, Roots
from agent_host_server.provider.base import AgentProvider
from agent_host_server.ws import serve_websocket

AGENTS_GROUP = "agent_host_server.agents"

_log = logging.getLogger("agent_host_server.node")


@dataclass(frozen=True)
class NodeContext:
    """What the node hands every agent factory."""

    #: The folders the machine serves; sessions work inside them.
    roots: Roots
    #: A directory of the agent's own, for anything it keeps beyond sessions.
    state_dir: Path


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="agent-host-node")
    parser.add_argument("--config", type=Path, required=True, help="the node's TOML settings")
    parser.add_argument(
        "--root",
        action="append",
        metavar="[NAME=]PATH",
        help="a folder sessions may work in (overrides the config's); repeat as NAME=PATH",
    )
    parser.add_argument("--port", type=int)
    parser.add_argument("--bind")
    parser.add_argument("--token-file", type=Path, help="require this connection token")
    parser.add_argument(
        "--state-dir", type=Path, help="where sessions and the sequence counter persist"
    )
    parser.add_argument("-v", "--verbose", action="store_true", default=None)
    return parser.parse_args(argv)


def _factories() -> dict[str, Any]:
    return {entry.name: entry for entry in entry_points(group=AGENTS_GROUP)}


async def create_agents(settings: NodeSettings) -> list[AgentProvider]:
    """Build every configured agent, in order, through its package's factory."""
    available = _factories()
    providers: list[AgentProvider] = []
    for spec in settings.agents:
        entry = available.get(spec.type)
        if entry is None:
            known = ", ".join(sorted(available)) or "none installed"
            raise ConfigError(f"no agent package provides type {spec.type!r} (installed: {known})")
        factory = entry.load()
        provider_id = str(spec.options.get("provider_id", spec.type))
        state_dir = settings.state_dir / "agents" / provider_id
        state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        made = factory(dict(spec.options), NodeContext(roots=settings.roots, state_dir=state_dir))
        provider = await made if inspect.isawaitable(made) else made
        _log.info(
            "agent %s (%s): %s", provider.agent.provider, spec.type, provider.agent.display_name
        )
        providers.append(provider)
    return providers


def resources_for(roots: Roots) -> ResourceProvider | None:
    """Read-only browsing, so clients can pick a folder; None with no jail here."""
    if not _jail_supported():
        return None
    if roots.is_named:
        return NamedRootsResourceProvider(roots)
    return RootedFilesystemResourceProvider(roots.primary)


def _jail_supported() -> bool:
    """Whether the host's folder-browsing jail can run on this OS.

    On POSIX, `RootedFilesystemResourceProvider` walks paths with `openat` and
    `O_NOFOLLOW`; on Windows the same class uses `core.resources_windows`
    (handle-relative `NtCreateFile` opens). Without either, clients cannot
    browse for a folder and sessions start in the primary root.
    """
    if sys.platform == "win32":
        try:
            import agent_host_server.core.resources_windows  # noqa: F401
        except ImportError:
            supported = False
        else:
            supported = True
    else:
        supported = os.open in os.supports_dir_fd and hasattr(os, "O_NOFOLLOW")
    if not supported:
        _log.warning("folder browsing is off: no filesystem jail for this OS")
    return supported


async def run(settings: NodeSettings, info: HostInfo | None = None) -> None:
    token = settings.token_file.read_text().strip() if settings.token_file else None
    state = settings.state_dir
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    providers = await create_agents(settings)

    from agent_host_server import __version__

    host = Host(
        providers,
        LoopbackSingleUserPolicy(),
        info=info or HostInfo(name="agent-host-node", version=__version__),
        resources=resources_for(settings.roots),
        default_directory=settings.roots.default_directory(),
        store=FileSessionStore(state / "sessions"),
        sequence_file=state / "sequence",
    )
    restored = await host.restore()
    _log.info("restored %d session(s)", restored)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        # Windows' event loops have no signal handlers; there Ctrl-C arrives
        # as KeyboardInterrupt and a service manager simply ends the process.
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    async with serve_websocket(
        host, bind=settings.bind, port=settings.port, connection_token=token
    ) as server:
        roots = settings.roots
        served = (
            ", ".join(f"{n}={p}" for n, p in zip(roots.names, roots.paths, strict=True))
            if roots.is_named
            else str(roots.primary)
        )
        agents = ", ".join(p.agent.provider for p in providers)
        _log.info("serving %s on %s:%s, root %s", agents, settings.bind, server.bound_port, served)
        with contextlib.suppress(asyncio.CancelledError):
            await stop.wait()
    await host.aclose()


def main(argv: list[str] | None = None) -> None:
    try:
        settings = load(_parse_args(argv))
    except ConfigError as exc:
        raise SystemExit(f"agent-host-node: {exc}") from None
    logging.basicConfig(
        level=logging.DEBUG if settings.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    try:
        with contextlib.suppress(KeyboardInterrupt):
            asyncio.run(run(settings))
    except ConfigError as exc:
        raise SystemExit(f"agent-host-node: {exc}") from None


__all__ = ["AGENTS_GROUP", "NodeContext", "create_agents", "main", "resources_for", "run"]
