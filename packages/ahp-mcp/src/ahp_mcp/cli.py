"""`ahp-mcp`: serve the tools over streamable HTTP.

Secrets come from the environment or from files, never from arguments, which
any user on the machine can read in the process list:

* `AHP_MCP_ENDPOINT_TOKEN` (or `--endpoint-token-file`): the token the AHP
  endpoint expects, if any.
* `AHP_MCP_OWNER_PASSPHRASE` (or `--passphrase-file`): what the owner types
  on the sign-in page.
"""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import logging
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from ahp_client import connect
from ahp_client.api import ClientContext

from ahp_mcp import __version__
from ahp_mcp.auth import OwnerAuthorizationServer
from ahp_mcp.fleet import Fleet
from ahp_mcp.server import MCP_PATH, build_server, http_app

__all__ = ["main", "parse_args"]

_log = logging.getLogger("ahp_mcp")


def _default_state() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "ahp-mcp" / "oauth.json"


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="ahp-mcp",
        description="A remote MCP server that drives AHP sessions through a host or a gateway.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--endpoint",
        default=os.environ.get("AHP_MCP_ENDPOINT"),
        help="the AHP WebSocket URL: an ahp-gateway, or a lone host (env AHP_MCP_ENDPOINT)",
    )
    parser.add_argument("--endpoint-token-file", type=Path, help="file holding the endpoint token")
    parser.add_argument("--client-id", default="ahp-mcp", help="the AHP clientId to connect as")
    parser.add_argument("--bind", default="127.0.0.1", help="address to listen on")
    parser.add_argument("--port", type=int, default=8765, help="port to listen on")
    parser.add_argument(
        "--allow-remote",
        action="store_true",
        help="allow a bind address other than loopback (normally a reverse proxy is in front)",
    )
    parser.add_argument(
        "--public-url",
        default=os.environ.get("AHP_MCP_PUBLIC_URL"),
        help="the https origin clients reach this server at, e.g. https://mcp.example.com "
        "(env AHP_MCP_PUBLIC_URL); the connector URL is this plus /mcp",
    )
    parser.add_argument("--passphrase-file", type=Path, help="file holding the owner passphrase")
    parser.add_argument(
        "--state",
        type=Path,
        default=_default_state(),
        help="where registered clients and token digests are kept (default: %(default)s)",
    )
    parser.add_argument(
        "--allow-redirect",
        action="append",
        default=[],
        metavar="URI",
        help="a redirect URI clients may register (repeatable; a trailing * is a prefix). "
        "Without any, every client may register and the sign-in page is the only gate",
    )
    parser.add_argument(
        "--allow-origin",
        action="append",
        default=[],
        metavar="ORIGIN",
        help="a browser origin allowed to call the MCP endpoint, besides the public URL's",
    )
    parser.add_argument(
        "--session-config",
        type=json.loads,
        default={},
        metavar="JSON",
        help='config for every session started, e.g. \'{"permissionMode": "auto"}\'',
    )
    parser.add_argument("--agent", help="the agent to start sessions with when none is named")
    parser.add_argument(
        "--no-auth",
        action="store_true",
        help="serve without authentication; refused unless bound to loopback",
    )
    parser.add_argument("--log-level", default="INFO")
    return parser.parse_args(argv)


def _secret(env: str, path: Path | None) -> str | None:
    if path is not None:
        return path.read_text(encoding="utf-8").strip() or None
    return os.environ.get(env) or None


def _is_loopback(bind: str) -> bool:
    if bind == "localhost":
        return True
    try:
        return ipaddress.ip_address(bind).is_loopback
    except ValueError:
        return False


def build(args: argparse.Namespace) -> tuple[Any, str]:
    """The ASGI app and a line saying where it is, from parsed arguments."""
    if not args.endpoint:
        raise SystemExit("ahp-mcp: --endpoint (or AHP_MCP_ENDPOINT) is required")
    if not _is_loopback(args.bind) and not args.allow_remote:
        raise SystemExit(f"ahp-mcp: {args.bind} is not loopback; pass --allow-remote to bind it")
    if not isinstance(args.session_config, dict):
        raise SystemExit("ahp-mcp: --session-config must be a JSON object")
    token = _secret("AHP_MCP_ENDPOINT_TOKEN", args.endpoint_token_file)

    def connector() -> ClientContext:
        return connect(str(args.endpoint), token=token, client_id=args.client_id, label="ahp-mcp")

    fleet = Fleet(connector, session_config=args.session_config, default_agent=args.agent)
    auth: OwnerAuthorizationServer | None = None
    public_url: str | None = args.public_url.rstrip("/") if args.public_url else None
    if args.no_auth:
        if not _is_loopback(args.bind):
            raise SystemExit("ahp-mcp: --no-auth is only allowed on a loopback bind")
    else:
        passphrase = _secret("AHP_MCP_OWNER_PASSPHRASE", args.passphrase_file)
        if public_url is None:
            raise SystemExit("ahp-mcp: --public-url (or AHP_MCP_PUBLIC_URL) is required")
        if passphrase is None:
            raise SystemExit(
                "ahp-mcp: set AHP_MCP_OWNER_PASSPHRASE or --passphrase-file, or pass --no-auth"
            )
        try:
            auth = OwnerAuthorizationServer(
                passphrase,
                issuer_url=public_url,
                resource=f"{public_url}{MCP_PATH}",
                state_path=args.state,
                allowed_redirect_uris=args.allow_redirect,
            )
        except ValueError as exc:
            raise SystemExit(f"ahp-mcp: {exc}") from exc
    server = build_server(fleet, public_url=public_url, auth=auth)
    app = http_app(server, bind=args.bind, public_url=public_url, allowed_origins=args.allow_origin)
    where = f"{public_url or f'http://{args.bind}:{args.port}'}{MCP_PATH}"
    return app, where


def main(argv: Sequence[str] | None = None) -> None:
    import uvicorn

    args = parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(), stream=sys.stderr)
    app, where = build(args)
    _log.info("serving MCP at %s", where)
    config = uvicorn.Config(app, host=args.bind, port=args.port, log_level=args.log_level.lower())
    asyncio.run(uvicorn.Server(config).serve())
