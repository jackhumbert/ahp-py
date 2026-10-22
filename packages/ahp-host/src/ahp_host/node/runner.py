"""Serve every agent a machine runs from one AHP host.

    python -m ahp_host.node --config ~/.config/ahp/node.toml
    ahp-node [run|supervise|restart|install|uninstall] --config ~/.config/ahp/node.toml

One process, one port, one folder tree and one session store for the machine;
the agents are listed in the config's `[[agents]]` tables (see
`ahp_host.node.config`). Each `type` names an installed agent package,
found through the `ahp_host.agents` entry-point group:

    # the agent package's pyproject.toml
    [project.entry-points."ahp_host.agents"]
    claude = "ahp_host_claude.agent:create"

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
import json
import logging
import os
import signal
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from importlib.metadata import entry_points
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from ahp_host.core import Host, HostInfo, LoopbackSingleUserPolicy
from ahp_host.core.automations import FileAutomationStore
from ahp_host.core.resources import ResourceProvider, RootedFilesystemResourceProvider
from ahp_host.core.store import FileSessionStore
from ahp_host.node.config import ConfigError, NodeSettings, load
from ahp_host.node.roots import NamedRootsResourceProvider, Roots
from ahp_host.node.service import (
    DEFAULT_LAUNCHD_LABEL,
    DEFAULT_SYSTEMD_UNIT,
    DEFAULT_TASK_NAME,
    RESTART_EXIT_CODE,
    STOP_REQUEST,
    Supervisor,
    install_linux,
    install_macos,
    install_windows,
    node_command,
    publish_drain_timeout,
    python_for_service,
    supervisor_log,
    uninstall_linux,
    uninstall_macos,
    uninstall_windows,
)
from ahp_host.provider.base import AgentProvider
from ahp_host.ws import serve_websocket

AGENTS_GROUP = "ahp_host.agents"

_log = logging.getLogger("ahp_host.node")


@dataclass(frozen=True)
class NodeContext:
    """What the node hands every agent factory."""

    #: The folders the machine serves; sessions work inside them.
    roots: Roots
    #: A directory of the agent's own, for anything it keeps beyond sessions.
    state_dir: Path


COMMANDS = ("run", "supervise", "restart", "install", "uninstall")

#: Files in the state directory that ask a running node to wind down. A file,
#: not just a signal, because Windows has no signal to send a console-less
#: process; the same files serve `ahp-node restart` on every OS.
RESTART_REQUEST = "restart-request"
REQUEST_POLL_SECONDS = 1.0


@dataclass(frozen=True)
class ShutdownRequest:
    """Why the node is winding down, and how long it may wait for turns."""

    #: "stop" exits 0; "restart" exits `RESTART_EXIT_CODE` for the supervisor.
    kind: str
    #: Seconds to drain; None means the config's `drain_timeout`.
    timeout: float | None = None


def write_restart_request(state_dir: Path, timeout: float | None = None) -> Path:
    """Ask the node using `state_dir` to restart once its turns finish."""
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = state_dir / RESTART_REQUEST
    path.write_text(json.dumps({} if timeout is None else {"timeout": timeout}), encoding="utf-8")
    return path


def read_request_timeout(path: Path) -> float | None:
    """The `{"timeout": seconds}` a request file carries; None if absent or malformed.

    The file may be empty (an `ahp-node` of another version, or `touch`), so
    anything unreadable means "use the configured timeout", never an error.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig") or "{}")
    except (OSError, ValueError):
        return None
    value = data.get("timeout") if isinstance(data, dict) else None
    if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
        return None
    return float(value)


def _waiting_request(state_dir: Path) -> tuple[ShutdownRequest, tuple[str, int]] | None:
    """The request waiting in `state_dir` and which file it is; a stop outranks a restart.

    The file's identity (name and modification time) lets a watcher tell a
    request it has already passed on from a new one written over it.
    """
    for name, kind in ((STOP_REQUEST, "stop"), (RESTART_REQUEST, "restart")):
        path = state_dir / name
        try:
            stamp = path.stat().st_mtime_ns
        except OSError:
            continue
        return ShutdownRequest(kind, read_request_timeout(path)), (name, stamp)
    return None


def check_request_files(state_dir: Path) -> ShutdownRequest | None:
    """The request waiting in `state_dir`, if any. A stop outranks a restart."""
    waiting = _waiting_request(state_dir)
    return waiting[0] if waiting is not None else None


def clear_request_files(state_dir: Path) -> None:
    for name in (STOP_REQUEST, RESTART_REQUEST):
        with contextlib.suppress(OSError):
            (state_dir / name).unlink()


async def watch_request_files(
    state_dir: Path,
    on_request: Callable[[ShutdownRequest], None],
    *,
    interval: float = REQUEST_POLL_SECONDS,
    once: bool = True,
) -> None:
    """Poll `state_dir`, handing each request file that appears to `on_request`.

    With *once* the watch ends at the first. Without, it goes on -- the files
    are only cleared when the node has finished winding down, so a stop that
    arrives while a restart is draining has to be noticed, and each file is
    passed on once however long it stays.
    """
    seen: set[tuple[str, int]] = set()
    while True:
        waiting = _waiting_request(state_dir)
        if waiting is not None and waiting[1] not in seen:
            seen.add(waiting[1])
            on_request(waiting[0])
            if once:
                return
        await asyncio.sleep(interval)


class Shutdown:
    """The node's shutdown: the first request starts it, later ones adjust it.

    Everything that can ask -- a signal, a request file -- reports here, from
    the event loop. What a later request may do depends on what is running:

    * a **stop during a restart's drain** makes it a stop: the drain carries on
      to its own deadline, but the node then exits 0 and stays down. Leaving it
      a restart would bring the node straight back after a stop it was asked to
      honour.
    * a **stop during a stop's drain** cuts the wait short (`cut_short`): the
      same Ctrl-C twice that ends any program that is taking its time. Turns
      still running are cut off and recorded for resuming, as at the deadline.
    * a restart during anything is a no-op: the node is already going down.
    """

    def __init__(self) -> None:
        self.request: ShutdownRequest | None = None
        self.begun = asyncio.Event()
        self.cut_short = asyncio.Event()

    def ask(self, request: ShutdownRequest) -> None:
        if self.request is None:
            self.request = request
            self.begun.set()
        elif request.kind != "stop":
            return
        elif self.request.kind == "restart":
            self.request = ShutdownRequest("stop", self.request.timeout)
        else:
            self.cut_short.set()


async def wind_down(
    host: Any,
    request: ShutdownRequest,
    default_timeout: float,
    state_dir: Path,
    shutdown: Shutdown | None = None,
) -> int:
    """Drain, close, delete the request files; the exit code the node should use.

    Called with the host still serving, so clients see queued messages and
    in-flight turns finish; `aclose` then cuts off whatever outlived the
    timeout (recorded on the session so it resumes after the restart). With
    *shutdown*, a later request can end the wait early or turn a restart into
    a stop (see `Shutdown`); the exit code is the one it holds at the end.
    """
    timeout = request.timeout if request.timeout is not None else default_timeout
    _log.info("%s requested: waiting up to %.0fs for turns to finish", request.kind, timeout)
    # What the supervisor waits for before it kills us: the drain this is, not
    # the one the config had when the supervisor started.
    publish_drain_timeout(state_dir, timeout)
    started = time.monotonic()
    draining = asyncio.ensure_future(host.drain(timeout))
    if shutdown is not None:
        cut = asyncio.ensure_future(shutdown.cut_short.wait())
        await asyncio.wait({draining, cut}, return_when=asyncio.FIRST_COMPLETED)
        cut.cancel()
        if not draining.done():
            _log.warning("stopped again: not waiting for the remaining turns")
            draining.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await draining
            # Idempotent, and a zero wait: it only records what is still running.
            interrupted = await host.drain(0.0)
        else:
            interrupted = draining.result()
    else:
        interrupted = await draining
    if interrupted:
        _log.warning(
            "%d turn(s) still running after %.0fs; they resume after the restart",
            len(interrupted),
            time.monotonic() - started,
        )
    await host.aclose()
    clear_request_files(state_dir)
    final = shutdown.request if shutdown is not None and shutdown.request is not None else request
    return RESTART_EXIT_CODE if final.kind == "restart" else 0


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="ahp-node",
        description="run: serve the agents (the default). supervise: run it, restarting it "
        "(and the config's tunnel) when it exits. restart: ask a running node to restart "
        "once its turns finish. install / uninstall: start the supervisor "
        "at login (a Scheduled Task on Windows, a launchd agent on macOS), or at boot "
        "(a systemd user unit with lingering on Linux).",
    )
    parser.add_argument("command", nargs="?", choices=COMMANDS, default="run")
    parser.add_argument("--config", type=Path, help="the node's TOML settings")
    parser.add_argument("--log-file", type=Path, help="log here, rotated (overrides the config's)")
    parser.add_argument(
        "--name",
        help=f"install/uninstall: the Scheduled Task name (default {DEFAULT_TASK_NAME}) "
        f", launchd label (default {DEFAULT_LAUNCHD_LABEL}) "
        f"or systemd unit (default {DEFAULT_SYSTEMD_UNIT})",
    )
    parser.add_argument(
        "--root",
        action="append",
        metavar="[NAME=]PATH",
        help="a folder sessions may work in (overrides the config's); repeat as NAME=PATH",
    )
    parser.add_argument(
        "--roots-layout",
        choices=("tree", "real"),
        help="named roots: 'tree' (default) serves file:///<name>/..., 'real' serves each "
        "root at its real path, browsable from / (overrides the config's roots_layout)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        metavar="SECONDS",
        help="restart: how long to wait for running turns (default: the config's drain_timeout)",
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
            import ahp_host.core.resources_windows  # noqa: F401
        except ImportError:
            supported = False
        else:
            supported = True
    else:
        supported = os.open in os.supports_dir_fd and hasattr(os, "O_NOFOLLOW")
    if not supported:
        _log.warning("folder browsing is off: no filesystem jail for this OS")
    return supported


async def run(settings: NodeSettings, info: HostInfo | None = None) -> int:
    """Serve until asked to stop; the exit code (0, or 75 for "restart me").

    SIGTERM / SIGINT or a `stop-request` file drain and exit 0; SIGHUP (POSIX)
    or a `restart-request` file drain and exit 75. Draining waits for running
    turns for up to `drain_timeout` while the host keeps serving. A second
    SIGTERM / SIGINT while a stop drains cuts the wait short; a stop while a
    restart drains makes the exit a 0 (see `Shutdown`).
    """
    token = settings.token_file.read_text().strip() if settings.token_file else None
    state = settings.state_dir
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    # A request left by a node that died mid-drain would end this one at once.
    clear_request_files(state)
    publish_drain_timeout(state, settings.drain_timeout)
    providers = await create_agents(settings)

    from ahp_host import __version__

    host = Host(
        providers,
        LoopbackSingleUserPolicy(),
        info=info or HostInfo(name="ahp-node", version=__version__),
        resources=resources_for(settings.roots),
        default_directory=settings.roots.default_directory(),
        store=FileSessionStore(state / "sessions"),
        sequence_file=state / "sequence",
        # A node is always on and single-user, which is what a schedule needs:
        # `restore()` below starts the scheduler before anyone connects.
        automations=FileAutomationStore(state / "automations"),
        # No `completion_trigger_characters`, deliberately: a node fronts
        # whatever agents its config names, and only they know what opens
        # their pickers (`/` for slash commands, ...). Left unset, the host
        # advertises each `Completes` agent's own `DeclaresCompletionTriggers`.
    )
    restored = await host.restore()
    _log.info("restored %d session(s)", restored)
    shutdown = Shutdown()
    request = shutdown.ask

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        # Windows' event loops have no signal handlers; there Ctrl-C arrives
        # as KeyboardInterrupt and a stop-request file stands in for SIGTERM.
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, request, ShutdownRequest("stop"))
    if hasattr(signal, "SIGHUP"):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(signal.SIGHUP, request, ShutdownRequest("restart"))
    watcher = asyncio.create_task(watch_request_files(state, request, once=False))
    try:
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
            _log.info(
                "serving %s on %s:%s, root %s", agents, settings.bind, server.bound_port, served
            )
            with contextlib.suppress(asyncio.CancelledError):
                await shutdown.begun.wait()
            return await wind_down(
                host,
                shutdown.request or ShutdownRequest("stop"),
                settings.drain_timeout,
                state,
                shutdown,
            )
    finally:
        watcher.cancel()


def configure_logging(settings: NodeSettings) -> None:
    """To `log_file`, rotated, when there is one; else stderr.

    Under `pythonw.exe` there is no stderr at all, so without a log file the
    node would log nowhere.
    """
    level = logging.DEBUG if settings.verbose else logging.INFO
    fmt = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    if settings.log_file is None:
        logging.basicConfig(level=level, format=fmt)
        return
    settings.log_file.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        settings.log_file, maxBytes=5_000_000, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter(fmt))
    logging.basicConfig(level=level, handlers=[handler])


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    if args.command == "uninstall":
        print(_uninstall(args.name))
        return
    if args.config is None:
        raise SystemExit(f"ahp-node {args.command}: --config is required")
    try:
        settings = load(args)
    except ConfigError as exc:
        raise SystemExit(f"ahp-node: {exc}") from None
    config = args.config.expanduser().resolve()

    if args.command == "install":
        print(_install(config, settings, args.name))
        return
    if args.command == "supervise":
        children = [("node", node_command(config, settings.log_file))]
        if settings.tunnel:
            children.append(("tunnel", list(settings.tunnel)))
        supervisor = Supervisor(
            children,
            supervisor_log(settings.log_file),
            state_dir=settings.state_dir,
            drain_timeout=settings.drain_timeout,
        )
        raise SystemExit(supervisor.run())
    if args.command == "restart":
        timeout = args.timeout if args.timeout is not None else settings.drain_timeout
        write_restart_request(settings.state_dir, args.timeout)
        print(
            "restart requested: the node will restart once its turns finish "
            f"(waiting up to {timeout:.0f}s)"
        )
        return

    configure_logging(settings)
    try:
        code = 0
        with contextlib.suppress(KeyboardInterrupt):
            code = asyncio.run(run(settings))
        if code:
            raise SystemExit(code)
    except ConfigError as exc:
        _log.error("%s", exc)
        raise SystemExit(f"ahp-node: {exc}") from None
    except Exception:
        _log.exception("the node stopped")
        raise


def _install(config: Path, settings: NodeSettings, name: str | None) -> str:
    command = node_command(config, settings.log_file, python=python_for_service(), verb="supervise")
    if sys.platform == "win32":
        return install_windows(command, name or DEFAULT_TASK_NAME, settings.state_dir)
    if sys.platform == "darwin":
        return install_macos(
            command,
            name or DEFAULT_LAUNCHD_LABEL,
            supervisor_log(settings.log_file),
            settings.drain_timeout,
        )
    if sys.platform.startswith("linux"):
        return install_linux(
            command, name or DEFAULT_SYSTEMD_UNIT, drain_timeout=settings.drain_timeout
        )
    raise SystemExit(f"ahp-node install: not supported on {sys.platform}")


def _uninstall(name: str | None) -> str:
    if sys.platform == "win32":
        return uninstall_windows(name or DEFAULT_TASK_NAME)
    if sys.platform == "darwin":
        return uninstall_macos(name or DEFAULT_LAUNCHD_LABEL)
    if sys.platform.startswith("linux"):
        return uninstall_linux(name or DEFAULT_SYSTEMD_UNIT)
    raise SystemExit(f"ahp-node uninstall: not supported on {sys.platform}")


__all__ = ["AGENTS_GROUP", "NodeContext", "create_agents", "main", "resources_for", "run"]
