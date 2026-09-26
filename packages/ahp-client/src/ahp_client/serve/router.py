"""Answering the host's requests.

AHP is symmetric: ``ServerCommandMap`` has ten methods a *host* issues against a
*client* -- the nine ``resource*`` calls plus ``createResourceWatch``. No
reference client implements more than two of them, read-only.

This is not optional polish. Publishing a ``virtual://`` plugin makes the host
call ``resourceList``/``resourceRead`` straight back at you, so a forward-only
client silently publishes plugins whose children never render.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final, Protocol, runtime_checkable

from ahp_protocol.types import JsonObject

from ahp_client.client.errors import InvalidParams, MethodNotFound, NotFound

__all__ = ["REVERSE_METHODS", "ResourceRouter", "ResourceServer"]

#: Every method a host may call on us. Anything else is `-32601`, which is how
#: a peer declines in a protocol with no capability object.
REVERSE_METHODS: Final[frozenset[str]] = frozenset(
    {
        "resourceRead",
        "resourceWrite",
        "resourceList",
        "resourceCopy",
        "resourceDelete",
        "resourceMove",
        "resourceResolve",
        "resourceMkdir",
        "resourceRequest",
        "createResourceWatch",
    }
)


@runtime_checkable
class ResourceServer(Protocol):
    """Something that can answer a subset of :data:`REVERSE_METHODS`.

    A method it does not implement should raise :class:`MethodNotFound`; the
    router turns that into the wire error.
    """

    async def handle(self, method: str, params: Mapping[str, Any]) -> JsonObject: ...


class ResourceRouter:
    """Longest-prefix mount table over ``params["uri"]``.

    Longest-prefix rather than scheme: a client commonly serves
    ``virtual://plugins/`` from memory and ``file:///home/me/project`` from
    disk, and those need different servers under overlapping schemes.
    """

    def __init__(self) -> None:
        self._mounts: dict[str, ResourceServer] = {}

    def mount(self, prefix: str, server: ResourceServer) -> None:
        self._mounts[prefix] = server

    def unmount(self, prefix: str) -> None:
        self._mounts.pop(prefix, None)

    @property
    def mounts(self) -> Mapping[str, ResourceServer]:
        return self._mounts

    def _resolve(self, uri: str) -> ResourceServer | None:
        best: tuple[int, ResourceServer] | None = None
        for prefix, server in self._mounts.items():
            if uri.startswith(prefix) and (best is None or len(prefix) > best[0]):
                best = (len(prefix), server)
        return None if best is None else best[1]

    async def handle(self, method: str, params: Mapping[str, Any]) -> JsonObject:
        if method not in REVERSE_METHODS:
            raise MethodNotFound(-32601, f'no handler for server method "{method}"')
        # `resourceCopy`/`resourceMove` key on the source; a mount that can read
        # the source is the one that has to be asked, even if the destination
        # lives elsewhere -- and it will refuse a cross-mount destination itself.
        keyed_on = "source" if method in {"resourceCopy", "resourceMove"} else "uri"
        uri = params.get(keyed_on)
        if not isinstance(uri, str) or not uri:
            # Coercing the absent case to `""` and reporting the mount miss tells
            # the caller its URI was fine and the resource was gone, so it stops
            # asking -- when what it must do is send the required param.
            raise InvalidParams(-32602, f"{method} requires {keyed_on!r}")
        server = self._resolve(uri)
        if server is None:
            raise NotFound(-32008, f"nothing mounted for {uri!r}")
        return await server.handle(method, params)

    async def __call__(self, method: str, params: Mapping[str, Any]) -> JsonObject:
        """The `ServerRequestHandler` spelling of :meth:`handle`.

        The router has to satisfy both protocols: `set_server_request_handler`
        wants a callable, and `connect(resources=...)` calls `.handle`. Offering
        only one of them made the documented multi-mount composition answer
        -32603 `'ResourceRouter' object has no attribute 'handle'` on every
        reverse call.
        """
        return await self.handle(method, params)
