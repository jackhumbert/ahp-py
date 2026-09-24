"""Serve Claude as an AHP host.

    python -m agent_host_server_claude --root ~/Github --token-file ~/.config/agent-host/node.token

Binds loopback only. The token is read from a file, never taken on the command
line, so it stays out of process listings and logs.
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
from agent_host_server.core.resources import RootedFilesystemResourceProvider
from agent_host_server.core.store import FileSessionStore
from agent_host_server.ws import serve_websocket

from agent_host_server_claude import __version__
from agent_host_server_claude.provider import (
    PROVIDER_ID,
    ClaudeProvider,
    discover_models,
    is_valid_provider_id,
)

DEFAULT_STATE = Path.home() / ".local/state/agent-host-server-claude"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="agent-host-server-claude")
    parser.add_argument(
        "--root",
        type=Path,
        required=True,
        help="the directory sessions may work in; browsable (read-only) by clients",
    )
    parser.add_argument("--port", type=int, default=4321)
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--token-file", type=Path, help="require this connection token")
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=DEFAULT_STATE,
        help="where sessions and the sequence counter persist across restarts",
    )
    parser.add_argument("--agent-name", default="Claude")
    parser.add_argument(
        "--provider-id",
        default=PROVIDER_ID,
        help="the agent's id; give each machine behind one broker its own (e.g. claude-laptop)",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args()


async def _run(args: argparse.Namespace) -> None:
    root = args.root.expanduser().resolve()
    if not root.is_dir():
        raise SystemExit(f"--root {root} is not a directory")
    token = args.token_file.read_text().strip() if args.token_file else None
    state = args.state_dir.expanduser()
    state.mkdir(parents=True, exist_ok=True, mode=0o700)

    if not is_valid_provider_id(args.provider_id):
        raise SystemExit(f"--provider-id {args.provider_id!r}: use letters, digits, '-' and '_'")

    # Once, at start-up: the picker offers what Claude Code offers this account.
    models = await discover_models(root)
    logging.getLogger(__name__).info("models: %s", ", ".join(m.id for m in models) or "none")
    host = Host(
        ClaudeProvider(
            root, display_name=args.agent_name, models=models, provider_id=args.provider_id
        ),
        LoopbackSingleUserPolicy(),
        info=HostInfo(name="agent-host-server-claude", version=__version__),
        # Read-only: clients browse to pick a folder. The agent's own edits go
        # through Claude Code's tools, each approved by a human first.
        resources=RootedFilesystemResourceProvider(root) if _jail_supported() else None,
        default_directory=root.as_uri(),
        store=FileSessionStore(state / "sessions"),
        sequence_file=state / "sequence",
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        # Windows' event loops have no signal handlers; there Ctrl-C arrives
        # as KeyboardInterrupt and a service manager simply ends the process.
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    async with serve_websocket(
        host, bind=args.bind, port=args.port, connection_token=token
    ) as server:
        logging.getLogger(__name__).info(
            "serving Claude on %s:%s, root %s", args.bind, server.bound_port, root
        )
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


def main() -> None:
    args = _parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_run(args))


if __name__ == "__main__":
    main()
