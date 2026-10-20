"""Serve an ACP agent as an AHP host.

    python -m ahp_host_acp --config ~/.config/ahp/openclaw.toml
    python -m ahp_host_acp --root ~/Github --command "openclaw acp" \\
        --model ollama/glm-5.3-flash:cloud --model-command "/model {model} -s"

Settings come from the config file (see `ahp_host_acp.config`), with
flags taking precedence. Binds loopback by default; any other `--bind` (or `bind`
in the file) needs `--token-file`, and then the host accepts remote connections.
The token is read from a file, never taken on the command line, so it stays out
of process listings and logs. A client sends it as `?tkn=` or as
`Authorization: Bearer <token>`; the header keeps it out of a proxy's access log.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import signal
import sys
from collections.abc import Callable
from pathlib import Path

from ahp_host.core import Host, HostInfo, LoopbackSingleUserPolicy
from ahp_host.core.resources import ResourceProvider, RootedFilesystemResourceProvider
from ahp_host.core.store import FileSessionStore
from ahp_host.ws import bearer_token_validator, is_loopback, serve_websocket

from ahp_host_acp import __version__
from ahp_host_acp.config import ConfigError, Settings, load
from ahp_host_acp.provider import (
    CATALOGUE_FILE,
    DEFAULT_DESCRIPTION,
    AcpProvider,
    AgentSpec,
    is_valid_provider_id,
)
from ahp_host_acp.roots import NamedRootsResourceProvider


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="ahp-host-acp")
    parser.add_argument("--config", type=Path, help="a TOML settings file; flags override it")
    parser.add_argument(
        "--root",
        action="append",
        metavar="[NAME=]PATH",
        help="a folder sessions may work in, browsable read-only by clients; repeat as "
        "NAME=PATH to serve several named folders",
    )
    parser.add_argument("--command", help='the ACP agent to run, e.g. "openclaw acp"')
    parser.add_argument(
        "--model", action="append", metavar="ID", help="a model to offer; the first is the default"
    )
    parser.add_argument(
        "--model-command",
        metavar="TEMPLATE",
        help='a prompt that switches the agent\'s model, e.g. "/model {model} -s"',
    )
    parser.add_argument("--port", type=int)
    parser.add_argument(
        "--bind",
        help="the address to listen on (default: 127.0.0.1); a non-loopback one needs "
        "--token-file and accepts remote connections",
    )
    parser.add_argument("--token-file", type=Path, help="require this connection token")
    parser.add_argument(
        "--state-dir", type=Path, help="where sessions and the sequence counter persist"
    )
    parser.add_argument("--agent-name")
    parser.add_argument("--provider-id", help="the agent's id (default: acp)")
    parser.add_argument("-v", "--verbose", action="store_true", default=None)
    return parser.parse_args(argv)


def _resources(settings: Settings) -> ResourceProvider | None:
    """Read-only browsing: clients pick a folder."""
    if not _jail_supported():
        return None
    if settings.roots.is_named:
        return NamedRootsResourceProvider(settings.roots)
    return RootedFilesystemResourceProvider(settings.roots.primary)


def build_host(settings: Settings) -> Host:
    """The ACP host *settings* describe: the provider, the store and the folder browsing."""
    if not is_valid_provider_id(settings.provider_id):
        raise SystemExit(f"provider id {settings.provider_id!r}: use letters, digits, '-' and '_'")
    state = settings.state_dir
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    provider = AcpProvider(
        settings.roots,
        AgentSpec(
            command=settings.command,
            launcher=settings.launcher,
            env_allow=settings.env_allow,
            relay_command=settings.relay_command,
            relay_host=settings.relay_host,
            policy=settings.policy,
            client_methods=settings.client_methods,
            decline_feedback=settings.decline_feedback,
            env=settings.env,
            model_command=settings.model_command,
            config_options=settings.config_options,
            mcp_servers=settings.mcp_servers,
        ),
        display_name=settings.agent_name,
        models=settings.models,
        provider_id=settings.provider_id,
        description=settings.description or DEFAULT_DESCRIPTION,
        catalogue_file=state / CATALOGUE_FILE,
    )
    return Host(
        provider,
        LoopbackSingleUserPolicy(),
        info=HostInfo(name="ahp-host-acp", version=__version__),
        resources=_resources(settings),
        default_directory=settings.roots.default_directory(),
        store=FileSessionStore(state / "sessions"),
        sequence_file=state / "sequence",
        # No `completion_trigger_characters`: the provider declares `/` for
        # its slash commands, and the host advertises what providers declare.
    )


def read_token(settings: Settings) -> str | None:
    """The connection token, or None; refuses an off-loopback bind without one."""
    if settings.token_file is None:
        if not is_loopback(settings.bind):
            raise SystemExit(
                f"ahp-host-acp: refusing to serve on {settings.bind} without --token-file; "
                "bind a loopback address or require a connection token"
            )
        return None
    token = settings.token_file.expanduser().read_text().strip()
    if not token:
        raise SystemExit(f"ahp-host-acp: the token file {settings.token_file} is empty")
    return token


async def serve(
    host: Host,
    *,
    bind: str,
    port: int,
    token: str | None,
    stop: asyncio.Event | None = None,
    on_ready: Callable[[int], None] | None = None,
    banner: str = "",
) -> None:
    """Serve *host* on *bind*:*port* until *stop* (default: SIGTERM or SIGINT), then close it.

    A non-loopback *bind* needs a *token* and accepts remote connections; the token is
    accepted as `?tkn=` or as an `Authorization: Bearer` header (`bearer_token_validator`).
    """
    if not is_loopback(bind) and token is None:
        raise ValueError(f"refusing to serve on {bind} without a token")
    log = logging.getLogger(__name__)
    if stop is None:
        # A caller-supplied event means an embedder or test owns shutdown.
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            # Windows' event loops have no signal handlers; there Ctrl-C arrives
            # as KeyboardInterrupt and a service manager simply ends the process.
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, stop.set)
    async with serve_websocket(
        host,
        bind=bind,
        port=port,
        connection_token=bearer_token_validator(token) if token is not None else None,
        allow_remote=not is_loopback(bind),
    ) as server:
        log.info("serving %s on %s:%s", banner or "ahp-host-acp", bind, server.bound_port)
        if on_ready is not None:
            on_ready(server.bound_port)
        with contextlib.suppress(asyncio.CancelledError):
            await stop.wait()
    await host.aclose()


async def _run(settings: Settings) -> None:
    token = read_token(settings)
    host = build_host(settings)
    roots = settings.roots
    served = (
        ", ".join(f"{n}={p}" for n, p in zip(roots.names, roots.paths, strict=True))
        if roots.is_named
        else str(roots.primary)
    )
    banner = "{} ({}), root {}, models: {}".format(
        settings.agent_name,
        " ".join(settings.command),
        served,
        ", ".join(m.id for m in settings.models) or "agent default",
    )
    await serve(host, bind=settings.bind, port=settings.port, token=token, banner=banner)


def _jail_supported() -> bool:
    """Whether the host's folder-browsing jail can run on this OS.

    Same check as ahp-host-claude: POSIX needs `openat` with
    `O_NOFOLLOW`; Windows needs ahp-host's `resources_windows`.
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
        logging.getLogger(__name__).warning(
            "folder browsing is off: no filesystem jail for this OS in this ahp-host"
        )
    return supported


def main(argv: list[str] | None = None) -> None:
    try:
        settings = load(_parse_args(argv))
    except ConfigError as exc:
        raise SystemExit(f"ahp-host-acp: {exc}") from None
    logging.basicConfig(
        level=logging.DEBUG if settings.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    # Before anything starts: an off-loopback bind with no token is refused outright.
    read_token(settings)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_run(settings))


if __name__ == "__main__":
    main()
