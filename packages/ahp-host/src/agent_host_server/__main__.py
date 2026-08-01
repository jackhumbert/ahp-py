"""Run a demo host: ``python -m agent_host_server``.

Serves the offline echo provider over WebSocket on loopback and prints the
VS Code settings to paste. This is a **demonstration**, not a deployment: it
uses :class:`LoopbackSingleUserPolicy`, which permits everything and is safe
only because the socket is bound to loopback.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import functools
import json
import secrets

from agent_host_server.core import Host, HostInfo, LoopbackSingleUserPolicy
from agent_host_server.core.versions import DEFAULT_SUPPORTED_VERSIONS
from agent_host_server.provider import EchoProvider
from agent_host_server.ws import serve_websocket


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="agent-host-server", description=__doc__)
    parser.add_argument("--port", type=int, default=4321)
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument(
        "--token",
        nargs="?",
        const="",
        default=None,
        help="require a connection token on the upgrade; omit the value to generate one",
    )
    parser.add_argument(
        "--allow-remote",
        action="store_true",
        help="bind off-loopback. AHP defines no authentication -- read docs/research.md §8.",
    )
    parser.add_argument("--delay", type=float, default=0.05, help="echo delta delay, seconds")
    return parser.parse_args()


async def _run() -> None:
    args = _parse_args()
    token = args.token
    if token == "":
        token = secrets.token_urlsafe(24)

    host = Host(
        EchoProvider(delay=args.delay),
        LoopbackSingleUserPolicy(),
        info=HostInfo(name="agent-host-server (demo)"),
    )

    async with serve_websocket(
        host,
        bind=args.bind,
        port=args.port,
        connection_token=token,
        allow_remote=args.allow_remote,
    ) as server:
        emit = functools.partial(print, flush=True)
        emit(f"agent-host-server listening on {server.url}")
        emit(f"speaking protocol {', '.join(DEFAULT_SUPPORTED_VERSIONS)}\n")

        # `chat.remoteAgentHosts` holds IRawRemoteAgentHostEntry objects, not
        # URLs: `address` and `name` are both required strings, `connectionToken`
        # is separate and VS Code appends it as `?tkn=` itself. The address is
        # stored scheme-less because the transport defaults to `ws://`; only
        # `wss://` is preserved.
        entry: dict[str, object] = {
            "address": f"{args.bind}:{server.bound_port}",
            "name": "Echo (agent-host-server)",
        }
        if token:
            entry["connectionToken"] = token

        emit("To connect VS Code (1.131+), add to settings.json:\n")
        emit(
            json.dumps(
                {"chat.remoteAgentHostsEnabled": True, "chat.remoteAgentHosts": [entry]},
                indent=2,
            )
        )
        emit("\nThen open the Agent Sessions view and pick 'Echo'.  Ctrl-C to stop.")
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.Event().wait()

    await host.aclose()


def main() -> None:
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_run())


if __name__ == "__main__":
    main()
