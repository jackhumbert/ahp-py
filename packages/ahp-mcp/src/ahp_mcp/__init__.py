"""A remote MCP server for the Agent Host Protocol.

An MCP client (a chat app's custom connector, an IDE) gets tools to list the
machines behind an AHP endpoint, start a session on one in a chosen folder,
prompt it, read it and stop it. Behind the tools is one supervised
`ahp_client` connection: to an `ahp-gateway` the machines are its nodes, and a
lone host is one machine.

The server is meant to be reachable from the internet, so it carries its own
OAuth 2.1 authorization server (metadata, dynamic client registration, PKCE)
with one owner, who signs in with a passphrase. An embedder can swap it for any
`mcp` `TokenVerifier`.
"""

from __future__ import annotations

from ahp_mcp.fleet import Fleet, FleetError
from ahp_mcp.server import build_server

__version__ = "0.1.0"

__all__ = ["Fleet", "FleetError", "__version__", "build_server"]
