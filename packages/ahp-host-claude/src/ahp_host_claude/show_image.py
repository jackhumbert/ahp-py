"""`show_image`: a tool Claude calls to put a picture in the chat.

Claude replies in text, and a client has no image syntax in a reply's
markdown, so an image the agent wants the user to see -- a chart it rendered,
a screenshot it took, a file it found -- goes through this tool: one tool of
an in-process SDK MCP server (`mcp__ahp__show_image`) whose handler reads the
file and hands the bytes to `TurnSink.content_part`, which shows them as a
`contentRef` part of the reply, below the call.

It reads a file, so it is filed as a read (`permissions.POLICY_TOOLS`): the
host policy judges its path as it judges Read's, and the approval gate lets it
through without asking only for a file inside the session's folders -- what
Claude Code does for Read itself.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Final

from claude_agent_sdk import McpSdkServerConfig, SdkMcpTool, create_sdk_mcp_server

SERVER: Final = "ahp"
TOOL: Final = "show_image"
#: The name Claude calls it by.
NAME: Final = f"mcp__{SERVER}__{TOOL}"

#: Leading bytes -> content type, for the formats every client can draw.
_MAGIC: Final = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)

DESCRIPTION: Final = (
    "Show an image file to the user, inline in the chat below this call. Use it when the "
    "user should see a picture (a chart or diagram you made, a screenshot, an image in the "
    "project); describing it in text is not the same. PNG, JPEG, GIF or WebP, up to 4 MiB. "
    "Reading the image yourself still needs Read."
)

_SCHEMA: Final = {
    "type": "object",
    "properties": {
        "file_path": {
            "type": "string",
            "description": "The image file: absolute, or relative to the working directory.",
        }
    },
    "required": ["file_path"],
}


def content_type(data: bytes) -> str | None:
    """The image type *data* is, by its leading bytes; None for anything else."""
    for magic, kind in _MAGIC:
        if data.startswith(magic):
            return kind
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def resolve(file_path: Any, cwd: Path) -> Path | None:
    """The file *file_path* names, relative paths taken from *cwd*."""
    if not isinstance(file_path, str) or not file_path:
        return None
    path = Path(file_path).expanduser()
    return (path if path.is_absolute() else cwd / path).resolve()


def inside(path: Path, folders: tuple[Path, ...]) -> bool:
    """Whether *path* (resolved) is in one of *folders*."""
    return any(path.is_relative_to(folder.resolve()) for folder in folders)


def server(handler: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]) -> McpSdkServerConfig:
    """The MCP server Claude Code is started with; *handler* runs each call."""
    return create_sdk_mcp_server(
        SERVER,
        tools=[
            SdkMcpTool(name=TOOL, description=DESCRIPTION, input_schema=_SCHEMA, handler=handler)
        ],
    )
