"""Serve workflow scripts as an AHP host.

    python -m ahp_host_flow --config flow.toml

Providers, routing and workflow sources come from the config file (see
`ahp_host_flow.config`). Binds loopback by default; any other `--bind` needs
`--token-file`. The token is read from a file, never taken on the command line,
so it stays out of process listings and logs. A client sends it as `?tkn=` or as
`Authorization: Bearer <token>`; the header keeps it out of a proxy's access log.
`--ping-interval` sets the keepalive ping in seconds (0 turns pings off) and
`--max-size` the largest incoming message in bytes (0 means no limit).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Final

from ahp_host.core import Host, HostInfo, LoopbackSingleUserPolicy, Policy
from ahp_host.core.store import FileSessionStore, SessionStore
from ahp_host.node import Roots
from ahp_host.ws import (
    DEFAULT_PING_INTERVAL,
    bearer_token_validator,
    is_loopback,
    serve_websocket,
)

from ahp_host_flow import __version__, redaction
from ahp_host_flow.config import ConfigError, FlowConfig, load_config
from ahp_host_flow.provider import FlowProvider

DEFAULT_PORT = 4323
# The client's own `max_size` (WebSocketClientTransport.connect), so the server accepts
# every frame the client is willing to send; websockets' 1 MiB default refused some.
DEFAULT_MAX_SIZE: Final = 16 * 1024 * 1024


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="ahp-host-flow")
    parser.add_argument("--config", type=Path, required=True, help="the flow.toml to serve")
    parser.add_argument(
        "--root",
        action="append",
        type=Path,
        help="a folder the inner providers may work in",
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--token-file", type=Path, help="require this connection token")
    parser.add_argument(
        "--ping-interval",
        type=float,
        default=DEFAULT_PING_INTERVAL,
        help="seconds between keepalive pings; 0 turns them off (default: %(default)s)",
    )
    parser.add_argument(
        "--max-size",
        type=int,
        default=DEFAULT_MAX_SIZE,
        help="largest incoming message in bytes; 0 means no limit (default: %(default)s)",
    )
    parser.add_argument(
        "--no-store",
        action="store_true",
        help="keep no sessions on disk: nothing survives a restart",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args(argv)


def build_host(
    config: FlowConfig,
    *,
    roots: Sequence[Path] = (),
    store: bool = True,
    policy: Policy | None = None,
    session_store: SessionStore | None = None,
) -> Host:
    """The flow host. An embedder passes its own *policy* (default: loopback, one user) and
    *session_store* (default: files under ``state_dir`` unless *store* is false)."""
    # The store is what makes a restart resumable: `Host.restore()` reads back the sessions,
    # their resume state (runs, kept worktrees) and the sequence counter.
    stored: dict[str, Any] = {}
    if store:
        config.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        stored = {
            "store": FileSessionStore(config.state_dir / "sessions"),
            "sequence_file": config.state_dir / "sequence",
        }
    if session_store is not None:
        stored["store"] = session_store
    served: Roots | None = None
    if len(roots) == 1:
        served = Roots.single(roots[0].expanduser())
    elif roots:
        served = Roots(tuple(p.expanduser().resolve() for p in roots))
    provider = FlowProvider(config, roots=served)
    return Host(
        provider,
        policy if policy is not None else LoopbackSingleUserPolicy(),
        info=HostInfo(name="ahp-host-flow", version=__version__),
        **stored,
    )


async def _serve(
    host: Host,
    *,
    bind: str,
    port: int,
    token: str | None,
    ping_interval: float | None = DEFAULT_PING_INTERVAL,
    max_size: int | None = DEFAULT_MAX_SIZE,
    stop: asyncio.Event | None = None,
    on_ready: Callable[[int], None] | None = None,
) -> None:
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
    await host.restore()
    async with serve_websocket(
        host,
        bind=bind,
        port=port,
        connection_token=bearer_token_validator(token) if token is not None else None,
        allow_remote=not is_loopback(bind),
        ping_interval=ping_interval,
        max_size=max_size,
    ) as server:
        log.info("serving workflows on %s:%s", bind, server.bound_port)
        if on_ready is not None:
            on_ready(server.bound_port)
        poller = redaction.start_poller(host)
        try:
            with contextlib.suppress(asyncio.CancelledError):
                await stop.wait()
        finally:
            if poller is not None:
                poller.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await poller
    await host.aclose()


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        raise SystemExit(f"ahp-host-flow: {exc}") from None
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    if args.ping_interval < 0:
        raise SystemExit("ahp-host-flow: --ping-interval must be >= 0")
    if args.max_size < 0:
        raise SystemExit("ahp-host-flow: --max-size must be >= 0")
    if args.token_file is None and not is_loopback(args.bind):
        raise SystemExit(
            f"ahp-host-flow: refusing to serve on {args.bind} without --token-file; "
            "bind a loopback address or require a connection token"
        )
    token = args.token_file.read_text().strip() if args.token_file else None
    if args.token_file is not None and not token:
        raise SystemExit(f"ahp-host-flow: the token file {args.token_file} is empty")
    host = build_host(config, roots=args.root or (), store=not args.no_store)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(
            _serve(
                host,
                bind=args.bind,
                port=args.port,
                token=token,
                ping_interval=args.ping_interval or None,
                max_size=args.max_size or None,
            )
        )


if __name__ == "__main__":
    main()
