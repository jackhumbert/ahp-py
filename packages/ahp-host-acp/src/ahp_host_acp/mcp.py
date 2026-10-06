"""MCP servers an ACP agent is given for each session.

ACP's `session/new`, `session/resume` and `session/load` carry `mcpServers`
(stable): the client names servers and the *agent* connects to them. The
transports are `stdio` (`name`, `command` -- an absolute path -- `args`,
`env`), which "all Agents MUST support", and `http` / `sse` (`name`, `url`,
`headers`), "only available when the Agent capabilities indicate
`mcpCapabilities.http`" (or `.sse`). A server the agent cannot take is left
out with a warning rather than failing the session.

These are servers the host's operator configures (`[[mcp_servers]]`), not
ones a client publishes: this adapter runs no MCP client of its own, and the
agent, not the host, owns their lifecycle -- so they are not published as AHP
`mcpServer` customizations, whose start and stop a client could ask for and
nothing here could honour.
"""

from __future__ import annotations

import logging
import shutil
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final

log = logging.getLogger(__name__)

TRANSPORTS: Final = ("stdio", "http", "sse")


@dataclass(frozen=True)
class McpServer:
    name: str
    transport: str = "stdio"
    #: stdio: the program and its arguments.
    command: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)
    #: http / sse.
    url: str | None = None
    headers: Mapping[str, str] = field(default_factory=dict)

    def to_acp(self, capabilities: Mapping[str, Any]) -> dict[str, Any] | None:
        """The `McpServer` for a session request, or None if the agent cannot take it."""
        if self.transport == "stdio":
            program = shutil.which(self.command[0]) or self.command[0]
            return {
                "name": self.name,
                "command": program,
                "args": list(self.command[1:]),
                "env": [{"name": k, "value": v} for k, v in self.env.items()],
            }
        supported = capabilities.get("mcpCapabilities")
        if not isinstance(supported, Mapping) or supported.get(self.transport) is not True:
            log.warning(
                "MCP server %s left out: the agent does not take %s servers",
                self.name,
                self.transport,
            )
            return None
        return {
            "type": self.transport,
            "name": self.name,
            "url": self.url,
            "headers": [{"name": k, "value": v} for k, v in self.headers.items()],
        }
