"""A client that offers tools to a flow session; not a test module."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

import ahp_client
from ahp_client import Client
from ahp_client.serve import ClientToolHost
from ahp_host import Host
from ahp_protocol.transport import memory_pair

from tests.fleet_fakes import _follow_chats

_SERVING: set[asyncio.Task[None]] = set()


def _executor(name: str, answer: str) -> Any:
    async def execute(action: Mapping[str, Any]) -> Mapping[str, Any]:
        return {
            "success": True,
            "pastTenseMessage": f"Ran {name}",
            "content": [{"type": "text", "text": answer}],
        }

    return execute


async def join_tool_client(
    host: Host, session_uri: str, tools: Mapping[str, str], *, client_id: str = "editor"
) -> Client:
    """A client joined to *session_uri* as an active client offering *tools* (name -> answer)."""
    client_end, server_end = memory_pair()
    serving = asyncio.create_task(host.serve(server_end))
    _SERVING.add(serving)
    serving.add_done_callback(_SERVING.discard)
    client: Client = await ahp_client.connect(
        transport=client_end, reconnect=False, client_id=client_id
    )
    host_tools = ClientToolHost(client.protocol, client_id=client.client_id)
    for name, answer in tools.items():
        host_tools.register(
            {
                "name": name,
                "description": f"Test tool {name}",
                "inputSchema": {"type": "object"},
            },
            _executor(name, answer),
        )
    session = await client.open_session(session_uri, tools=host_tools)
    # The pump answers calls on the chats it has mirrored.
    await session.chat()
    watching = asyncio.create_task(_follow_chats(client, session))
    _SERVING.add(watching)
    watching.add_done_callback(_SERVING.discard)
    return client
