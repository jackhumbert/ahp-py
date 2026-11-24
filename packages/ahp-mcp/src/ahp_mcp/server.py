"""The MCP half: the tools, and the HTTP app that serves them.

The tools are thin. Each one calls a `Fleet` method and turns its answer into
text for the model, and its refusal (`FleetError`, or an AHP error) into a
tool error the model can read and act on. Anything else stays on the server.

The descriptions are the model's only manual, so they say what each tool is
for and what it will not do, not how it works.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator, Awaitable
from typing import Any, Final
from urllib.parse import urlsplit

from ahp_protocol import AhpError
from mcp.server.auth.provider import TokenVerifier
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import AnyHttpUrl
from starlette.applications import Starlette

from ahp_mcp.auth import OwnerAuthorizationServer
from ahp_mcp.fleet import Fleet, FleetError

__all__ = ["MCP_PATH", "build_server", "http_app"]

#: Where the MCP endpoint is served. A connector is added with `<public url>/mcp`.
MCP_PATH: Final = "/mcp"

INSTRUCTIONS: Final = (
    "These tools drive coding agent sessions on the user's own machines through the Agent "
    "Host Protocol. Start with list_hosts. A session runs on its own once started; check on "
    "it with wait_for_session or read_session. A session that is waiting on the user needs "
    "them to answer in one of their AHP apps: these tools cannot approve anything."
)

_READ: Final = ToolAnnotations(read_only_hint=True, open_world_hint=False)
_WRITE: Final = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True)
_STOP: Final = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=True)


async def _run(operation: Awaitable[Any]) -> str:
    try:
        result = await operation
    except FleetError as exc:
        raise ToolError(str(exc)) from exc
    except AhpError as exc:
        raise ToolError(exc.message) from exc
    return result if isinstance(result, str) else json.dumps(result, indent=2)


def build_server(
    fleet: Fleet,
    *,
    public_url: str | None = None,
    auth: OwnerAuthorizationServer | None = None,
    token_verifier: TokenVerifier | None = None,
) -> MCPServer[Any]:
    """An `MCPServer` whose tools drive *fleet*.

    With *auth*, the server is its own OAuth authorization server, and
    *public_url* (the origin the connector reaches, `https://mcp.example.com`)
    is its issuer. With *token_verifier*, tokens come from an authorization
    server elsewhere, and *public_url* names that issuer's resource. With
    neither, there is no authentication: only for a loopback bind.
    """
    settings: AuthSettings | None = None
    if auth is not None or token_verifier is not None:
        if public_url is None:
            raise ValueError("an authenticated server needs its public_url")
        base = public_url.rstrip("/")
        settings = AuthSettings(
            issuer_url=AnyHttpUrl(auth.issuer_url if auth is not None else base),
            resource_server_url=AnyHttpUrl(f"{base}{MCP_PATH}"),
            # Every token the built-in server issues names this resource, so
            # one minted for another resource is refused at the door.
            validate_token_resource=auth is not None,
            client_registration_options=ClientRegistrationOptions(enabled=True)
            if auth is not None
            else None,
            revocation_options=RevocationOptions(enabled=True) if auth is not None else None,
        )

    @contextlib.asynccontextmanager
    async def lifespan(_: MCPServer[Any]) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await fleet.aclose()

    server: MCPServer[Any] = MCPServer(
        name="ahp-mcp",
        title="Agent Host Protocol",
        instructions=INSTRUCTIONS,
        auth_server_provider=auth,
        token_verifier=token_verifier,
        auth=settings,
        lifespan=lifespan,
    )
    if auth is not None:
        for route in auth.routes():
            server.custom_route(route.path, methods=sorted(route.methods or ()))(route.endpoint)
    _register(server, fleet)
    return server


def _register(server: MCPServer[Any], fleet: Fleet) -> None:
    @server.tool(title="List hosts", annotations=_READ)
    async def list_hosts() -> str:
        """List the machines sessions can run on, and the agents they can be started with.

        Each host has an id (use it as `host` in the other tools) and its root folder.
        """
        return await _run(fleet.list_hosts())

    @server.tool(title="List folder", annotations=_READ)
    async def list_folder(host: str, folder: str = "") -> str:
        """List what is in a folder on a host.

        `folder` is a path relative to the host's root, an absolute path, or a `uri`
        from an earlier listing; leave it empty for the host's root.
        """
        return await _run(fleet.list_folder(host, folder or None))

    @server.tool(title="List sessions", annotations=_READ)
    async def list_sessions(limit: int = 50) -> str:
        """List the user's sessions on every host, most recent first."""
        return await _run(fleet.list_sessions(limit))

    @server.tool(title="Start session", annotations=_WRITE)
    async def start_session(
        host: str, prompt: str, folder: str = "", agent: str = "", title: str = ""
    ) -> str:
        """Start a coding session on a host and send it its first message.

        `folder` is where it works (as for list_folder; empty is the host's root).
        `agent` is one from list_hosts, needed when there is more than one.
        The session runs on its own; anything it needs approved waits for the user in
        their AHP app. Returns the session id for the other tools.
        """
        return await _run(
            fleet.start_session(
                host, prompt, folder=folder or None, agent=agent or None, title=title or None
            )
        )

    @server.tool(title="Send prompt", annotations=_WRITE)
    async def send_prompt(session: str, prompt: str) -> str:
        """Send another message to a session. It must not be running a turn."""
        return await _run(fleet.send_prompt(session, prompt))

    @server.tool(title="Read session", annotations=_READ)
    async def read_session(session: str, turns: int = 2) -> str:
        """Read a session: its status, what it is waiting on, and its last turns
        (each message, the agent's answer and a line per tool it used)."""
        return await _run(fleet.read_session(session, turns))

    @server.tool(title="Wait for session", annotations=_READ)
    async def wait_for_session(session: str, timeout_seconds: float = 60) -> str:
        """Wait until a session stops working: done, failed, stopped, or waiting on the user.

        Returns its status, with `timed_out` true if it was still working at the timeout
        (at most 300 seconds). Read it afterwards for what it said.
        """
        return await _run(fleet.wait_for_session(session, timeout_seconds))

    @server.tool(title="Cancel session", annotations=_STOP)
    async def cancel_session(session: str) -> str:
        """Stop the turn a session is running. The session stays and can be prompted again."""
        return await _run(fleet.cancel_session(session))


def http_app(
    server: MCPServer[Any],
    *,
    bind: str,
    public_url: str | None = None,
    allowed_origins: list[str] | None = None,
) -> Starlette:
    """The streamable HTTP app, with the Host check widened to *public_url*.

    The SDK checks `Host` against loopback names when bound to loopback, which
    is right for a local server and refuses every request a reverse proxy
    forwards for the public name. So the public name is allowed too. A request
    with no `Origin` (a server-side client) passes; one from a browser passes
    only from the public origin or one in *allowed_origins*.
    """
    security: TransportSecuritySettings | None = None
    if public_url is not None:
        public = urlsplit(public_url)
        security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[public.netloc, "127.0.0.1:*", "localhost:*", "[::1]:*"],
            allowed_origins=[f"{public.scheme}://{public.netloc}", *(allowed_origins or ())],
        )
    return server.streamable_http_app(
        streamable_http_path=MCP_PATH,
        host=bind,
        transport_security=security,
    )
