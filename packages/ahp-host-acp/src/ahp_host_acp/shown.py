"""Images an agent shows in its reply, as `contentRef` response parts.

An `agent_message_chunk` carries one ACP content block. Text goes on as text;
this module handles the two blocks that show a picture:

- `image` (base64 `data` and a `mimeType`): the bytes go in the session's
  content store through `TurnSink.content_part`. Lasts until the host restarts.
- `resource_link` to an image file (`file:` `uri`, `image/*` `mimeType`): the
  part names the file itself through `TurnSink.content_ref`, so nothing is
  copied and it lasts as long as the file. A client reads it with
  `resourceRead`, so it shows only for a file under a folder the host serves.

An agent that makes image files should send the link: a full-size render fits,
and it is still there after a restart. What cannot be shown (a missing file,
one too large to read, bytes that are not base64) is named in a line of text
instead of being dropped.
"""

from __future__ import annotations

import base64
import binascii
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlsplit
from urllib.request import url2pathname

from ahp_host.provider.base import TurnSink

__all__ = ["MAX_LINKED_IMAGE", "show_block"]

log = logging.getLogger(__name__)

#: The largest image file shown by reference. `resourceRead` returns at most
#: 16 MiB, and the base64 of it has to fit one 16 MiB WebSocket message, so
#: about 12 MiB of file is the real ceiling; this leaves room for the frame.
MAX_LINKED_IMAGE: Final = 11 * 1024 * 1024


async def show_block(block: Mapping[str, Any], sink: TurnSink) -> bool:
    """Show *block* if it is an image; returns whether it was one of ours."""
    kind = block.get("type")
    mime = block.get("mimeType")
    if not (isinstance(mime, str) and mime.startswith("image/")):
        return False
    if kind == "image":
        await _show_inline(block.get("data"), mime, sink)
        return True
    if kind == "resource_link":
        uri = block.get("uri")
        if isinstance(uri, str) and uri.lower().startswith("file:"):
            await _show_file(uri, mime, str(block.get("name") or uri), sink)
            return True
    return False


async def _show_inline(data: Any, mime: str, sink: TurnSink) -> None:
    try:
        raw = base64.b64decode(data if isinstance(data, str) else "", validate=True)
        if not raw:
            raise ValueError("no image data")
        await sink.content_part(raw, content_type=mime)
    except (ValueError, binascii.Error) as exc:
        log.info("an image the agent sent was not shown: %s", exc)
        await sink.text_delta(f"\n[{mime} image not shown: {exc}]\n")


async def _show_file(uri: str, mime: str, name: str, sink: TurnSink) -> None:
    path = Path(url2pathname(urlsplit(uri).path))
    try:
        stat = path.stat()
    except OSError:
        await sink.text_delta(f"\n[image not found: {name}]\n")
        return
    if stat.st_size > MAX_LINKED_IMAGE:
        await sink.text_delta(
            f"\n[image too large to show ({stat.st_size // (1024 * 1024)} MiB): {name}]\n"
        )
        return
    # The nonce changes when the file is written again under the same name, so a
    # client caching by URI does not keep showing the old picture.
    await sink.content_ref(
        path.as_uri(),
        content_type=mime,
        size_hint=stat.st_size,
        nonce=f"{stat.st_mtime_ns:x}-{stat.st_size:x}",
    )
