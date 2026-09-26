"""Tools a client offers to run, given to Claude as an in-process MCP server.

A client publishes tools on `SessionActiveClient.tools`, and the host's
`TurnSink.run_client_tool` asks it to run one: the call executes in the
client's process, not on this machine. Claude Code only calls tools it was
started with, so each published tool becomes a tool of one SDK MCP server
(`mcp__client__<name>`) whose handler hands the call to the client and waits.

Approval belongs to the client. For client-provided tools "the server
typically sets `confirmed` to `'not-needed'`" (`ChatToolCallReadyAction`), and
the host does exactly that: whoever runs the tool decides what it may do, so
this host's own approval gate lets these calls through (`is_client_tool`).
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from ahp_host.provider.base import ToolResult
from claude_agent_sdk import McpSdkServerConfig, SdkMcpTool, create_sdk_mcp_server

#: The MCP server's name, and so the middle of every tool name Claude sees.
SERVER: Final = "client"
_PREFIX: Final = f"mcp__{SERVER}__"
#: What Claude accepts as a tool name, less the prefix it will carry.
_UNSAFE: Final = re.compile(r"[^A-Za-z0-9_-]")
_MAX_NAME: Final = 64 - len(_PREFIX)


@dataclass(frozen=True)
class ClientTool:
    """One published tool, and which client runs it."""

    #: The name Claude calls it by (without the `mcp__client__` prefix).
    name: str
    #: The name the client published, which is what it is asked to run.
    published: str
    title: str
    description: str
    input_schema: Mapping[str, Any] = field(default_factory=dict)
    client_id: str = field(default="", compare=False)


def is_client_tool(tool_name: str) -> bool:
    """Whether Claude's name for a tool is one of these."""
    return tool_name.startswith(_PREFIX)


def offered(clients: Sequence[Mapping[str, Any]]) -> tuple[ClientTool, ...]:
    """Every tool the session's clients publish, each name once.

    Two clients publishing the same tool (two windows of one editor) run the
    same thing, so the first in `activeClients` order runs it. A name Claude
    cannot use is made safe rather than dropped; one that then collides with
    another is dropped, because a call to it could not say which was meant.
    """
    tools: dict[str, ClientTool] = {}
    for client in clients:
        client_id = client.get("clientId")
        published = client.get("tools")
        if not isinstance(client_id, str) or not isinstance(published, list):
            continue
        for tool in published:
            if not isinstance(tool, Mapping) or not isinstance(tool.get("name"), str):
                continue
            name = _UNSAFE.sub("_", tool["name"])[:_MAX_NAME]
            if not name or name in tools:
                continue
            title = tool.get("title")
            description = tool.get("description")
            tools[name] = ClientTool(
                name=name,
                published=tool["name"],
                title=title if isinstance(title, str) and title else tool["name"],
                description=description if isinstance(description, str) else "",
                input_schema=_schema(tool.get("inputSchema")),
                client_id=client_id,
            )
    return tuple(tools.values())


def _schema(value: Any) -> dict[str, Any]:
    """An object schema the SDK passes through as it is.

    The SDK reads a dict without both `type` and `properties` as a map of
    parameter names to Python types, and would turn `{"type": "object"}` into a
    required parameter called `type`.
    """
    schema = dict(value) if isinstance(value, Mapping) else {}
    schema.setdefault("type", "object")
    schema.setdefault("properties", {})
    return schema


Runner = Callable[[ClientTool, dict[str, Any]], Awaitable[dict[str, Any]]]


def server(tools: Sequence[ClientTool], run: Runner) -> McpSdkServerConfig:
    """The MCP server Claude Code is started with; `run` executes each call."""

    def handler(tool: ClientTool) -> Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]:
        async def call(arguments: dict[str, Any]) -> dict[str, Any]:
            return await run(tool, arguments)

        return call

    return create_sdk_mcp_server(
        SERVER,
        tools=[
            SdkMcpTool(
                name=tool.name,
                description=tool.description or tool.title,
                input_schema=dict(tool.input_schema),
                handler=handler(tool),
            )
            for tool in tools
        ],
    )


def mcp_result(result: ToolResult) -> dict[str, Any]:
    """The client's `ToolCallResult`, as the MCP result Claude reads.

    Claude reads text. Content it cannot take as it is - a worker chat the
    tool started, a file it edited - is described, so the agent knows what the
    tool did and where to find it.
    """
    if not result.accepted:
        return error(result.reason or "The client did not run this tool.")
    value = result.value if isinstance(result.value, Mapping) else {}
    texts = [text for part in value.get("content") or () if (text := _text(part))]
    if not texts and value.get("structuredContent") is not None:
        texts.append(json.dumps(value["structuredContent"]))
    if not texts:
        past = value.get("pastTenseMessage")
        texts.append(past if isinstance(past, str) and past else "Done.")
    return {"content": [{"type": "text", "text": text} for text in texts]}


def _text(part: Any) -> str | None:
    if not isinstance(part, Mapping):
        return None
    kind = part.get("type")
    if kind == "text":
        text = part.get("text")
        return text if isinstance(text, str) else None
    if kind == "subagent":
        # `ToolResultSubagentContent`: a chat the tool started, and its URI.
        lines = [f"Started {part.get('title') or 'a worker'}: {part.get('resource')}"]
        if isinstance(part.get("description"), str):
            lines.append(part["description"])
        return "\n".join(lines)
    return json.dumps(dict(part))


def error(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}], "is_error": True}
