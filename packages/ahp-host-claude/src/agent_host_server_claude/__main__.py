"""Serve Claude as an AHP host.

    python -m agent_host_server_claude --config ~/.config/agent-host/node.toml
    python -m agent_host_server_claude --root ~/Github --token-file ~/.config/agent-host/node.token

Settings come from the config file (see `agent_host_server_claude.config`),
with flags taking precedence. Binds loopback only. The token is read from a
file, never taken on the command line, so it stays out of process listings
and logs.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import signal
import sys
from pathlib import Path

from agent_host_server.core import Host, HostInfo, LoopbackSingleUserPolicy
from agent_host_server.core.resources import ResourceProvider, RootedFilesystemResourceProvider
from agent_host_server.core.store import FileSessionStore
from agent_host_server.ws import serve_websocket

from agent_host_server_claude import __version__
from agent_host_server_claude.claude_ai import LOCAL, Api
from agent_host_server_claude.config import ConfigError, Settings, load
from agent_host_server_claude.provider import (
    ClaudeProvider,
    discover,
    is_valid_provider_id,
)
from agent_host_server_claude.roots import NamedRootsResourceProvider


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="agent-host-server-claude")
    parser.add_argument("--config", type=Path, help="a TOML settings file; flags override it")
    parser.add_argument(
        "--root",
        action="append",
        metavar="[NAME=]PATH",
        help="a folder sessions may work in, browsable read-only by clients; repeat as "
        "NAME=PATH to serve several named folders",
    )
    parser.add_argument("--port", type=int)
    parser.add_argument("--bind")
    parser.add_argument("--token-file", type=Path, help="require this connection token")
    parser.add_argument(
        "--state-dir", type=Path, help="where sessions and the sequence counter persist"
    )
    parser.add_argument("--agent-name")
    parser.add_argument("--provider-id", help="the agent's id (default: claude)")
    parser.add_argument(
        "--remote-control",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="put new sessions on claude.ai (default: whatever Claude Code does, i.e. "
        "your remoteControlAtStartup setting)",
    )
    parser.add_argument(
        "--claude-ai-sessions",
        nargs="?",
        const="local",
        choices=["local", "all", "off"],
        help="also list this machine's other Remote Control sessions (terminal, desktop app) "
        "through claude.ai; 'all' lists every machine's (on one machine only)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", default=None)
    return parser.parse_args(argv)


def _resources(settings: Settings) -> ResourceProvider | None:
    """Read-only browsing: clients pick a folder. The agent's own edits go
    through Claude Code's tools, each approved by a human first."""
    if not _jail_supported():
        return None
    if settings.roots.is_named:
        return NamedRootsResourceProvider(settings.roots)
    return RootedFilesystemResourceProvider(settings.roots.primary)


async def _run(settings: Settings) -> None:
    if not is_valid_provider_id(settings.provider_id):
        raise SystemExit(f"provider id {settings.provider_id!r}: use letters, digits, '-' and '_'")
    token = settings.token_file.read_text().strip() if settings.token_file else None
    state = settings.state_dir
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    log = logging.getLogger(__name__)

    # Once, at start-up: the picker offers what Claude Code offers this account,
    # and new sessions go on claude.ai if Claude Code's own would.
    found = await discover(settings.roots.primary)
    models = found.models
    remote_control = (
        found.remote_control if settings.remote_control is None else settings.remote_control
    )
    log.info("models: %s", ", ".join(m.id for m in models) or "none")
    log.info("Remote Control for new sessions: %s", "on" if remote_control else "off")
    host = Host(
        ClaudeProvider(
            settings.roots,
            display_name=settings.agent_name,
            models=models,
            provider_id=settings.provider_id,
            remote_control=remote_control,
            claude_ai=Api() if settings.claude_ai_sessions else None,
            claude_ai_scope=settings.claude_ai_sessions or LOCAL,
            state_dir=state,
        ),
        LoopbackSingleUserPolicy(),
        info=HostInfo(name="agent-host-server-claude", version=__version__),
        resources=_resources(settings),
        default_directory=settings.roots.default_directory(),
        store=FileSessionStore(state / "sessions"),
        sequence_file=state / "sequence",
    )
    # Bring back the sessions saved before the last stop. Without this they
    # were written to `state/sessions` and never read again, so every restart
    # emptied the session list. Their Claude clients start on their first turn,
    # or straight away for those on claude.ai.
    restored = await host.restore()
    log.info("restored %d session(s)", restored)
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
        log.info("serving Claude on %s:%s, root %s", settings.bind, server.bound_port, served)
        with contextlib.suppress(asyncio.CancelledError):
            await stop.wait()
    await host.aclose()


def _jail_supported() -> bool:
    """Whether the host's folder-browsing jail can run on this OS.

    On POSIX, `RootedFilesystemResourceProvider` walks paths with `openat` and
    `O_NOFOLLOW`. On Windows, agent-host-server has its own read-only jail
    (`core.resources_windows`, handle-relative `NtCreateFile` opens) that the
    same class selects there. With neither - POSIX without `openat`, or an
    agent-host-server from before the Windows jail - clients cannot browse for
    a folder and sessions start in `--root`.
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
        logging.getLogger(__name__).warning(
            "folder browsing is off: no filesystem jail for this OS in this agent-host-server"
        )
    return supported


def main(argv: list[str] | None = None) -> None:
    try:
        settings = load(_parse_args(argv))
    except ConfigError as exc:
        raise SystemExit(f"agent-host-server-claude: {exc}") from None
    logging.basicConfig(
        level=logging.DEBUG if settings.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    # One line per request is a line every few seconds per claude.ai session.
    logging.getLogger("httpx").setLevel(logging.DEBUG if settings.verbose else logging.WARNING)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_run(settings))


if __name__ == "__main__":
    main()
