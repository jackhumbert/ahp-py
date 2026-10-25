"""A session-level ACP client over `AcpConnection`.

It does the protocol's bookkeeping (`initialize`, capability gating, routing the
agent's inbound requests) and nothing else: no policy. The caller decides what a
permission request gets and what to do with each update.

Every session method takes `meta` (sent as `_meta`) and `**extra` (more
top-level params), so a field the agent understands that this module does not
model is still reachable, and `request`/`notify` are public for extension
methods (`_example/...`).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from os import PathLike
from types import TracebackType
from typing import Any, Final

from ahp_acp.connection import AcpConnection, NotificationHandler, RequestHandler
from ahp_acp.errors import (
    AcpError,
    MethodNotFoundError,
    ProtocolVersionError,
    UnsupportedError,
)
from ahp_acp.updates import SessionUpdate, parse_update

log = logging.getLogger(__name__)

UpdateHandler = Callable[[SessionUpdate], Awaitable[None]]
PermissionHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
ElicitationHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
ExtensionRequestHandler = Callable[[str, Any], Awaitable[Any]]
ExtensionNotificationHandler = Callable[[str, Any], Awaitable[None]]
ConnectionFactory = Callable[[NotificationHandler, RequestHandler], Awaitable[AcpConnection]]

#: The outcome that answers a permission request whose turn was cancelled.
CANCELLED: Final[dict[str, Any]] = {"outcome": "cancelled"}


class AcpClient:
    """Spawn (or be given) an agent connection and speak ACP to it.

    Pass `argv` (plus `spawn_options`, the keyword arguments of
    `AcpConnection.spawn`) or a `connection_factory` that takes the two inbound
    handlers and returns a connection. Use it as an async context manager:
    entering connects, and exiting closes the agent. `initialize()` is separate,
    so the caller can inspect the agent before sending anything.
    """

    def __init__(
        self,
        argv: Sequence[str] | None = None,
        *,
        connection_factory: ConnectionFactory | None = None,
        spawn_options: Mapping[str, Any] | None = None,
        client_info: Mapping[str, Any],
        client_capabilities: Mapping[str, Any] | None = None,
        on_update: UpdateHandler,
        on_permission: PermissionHandler,
        on_request: ExtensionRequestHandler | None = None,
        on_notification: ExtensionNotificationHandler | None = None,
        on_elicitation: ElicitationHandler | None = None,
        protocol_version: int = 1,
        strict_version: bool = False,
    ) -> None:
        if (argv is None) == (connection_factory is None):
            raise ValueError("give exactly one of argv and connection_factory")
        self._argv = argv
        self._factory = connection_factory
        self._spawn_options = dict(spawn_options or {})
        self._client_info = dict(client_info)
        capabilities: dict[str, Any] = (
            {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False}
            if client_capabilities is None
            else dict(client_capabilities)
        )
        if on_elicitation is not None:
            capabilities.setdefault("elicitation", {"form": {}, "url": {}})
        self._client_capabilities = capabilities
        self._on_update = on_update
        self._on_permission = on_permission
        self._on_request = on_request
        self._on_notification = on_notification
        self._on_elicitation = on_elicitation
        self._protocol_version = protocol_version
        self._strict_version = strict_version
        self._connection: AcpConnection | None = None
        self._permissions: dict[str, set[asyncio.Future[dict[str, Any]]]] = {}
        #: Filled by `initialize()`.
        self.agent_capabilities: dict[str, Any] = {}
        self.agent_info: dict[str, Any] | None = None
        self.auth_methods: list[Any] = []
        self.protocol_version: int | None = None

    # -- lifecycle -------------------------------------------------------------

    async def __aenter__(self) -> AcpClient:
        await self.connect()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def connect(self) -> None:
        if self._connection is not None:
            return
        if self._factory is not None:
            self._connection = await self._factory(self._handle_notification, self._handle_request)
        else:
            assert self._argv is not None
            self._connection = await AcpConnection.spawn(
                self._argv,
                on_notification=self._handle_notification,
                on_request=self._handle_request,
                **self._spawn_options,
            )

    async def aclose(self) -> None:
        connection, self._connection = self._connection, None
        if connection is not None:
            await connection.aclose()

    @property
    def connection(self) -> AcpConnection:
        if self._connection is None:
            raise AcpError(message="the client is not connected; use `async with` or connect()")
        return self._connection

    # -- passthroughs ----------------------------------------------------------

    async def request(
        self, method: str, params: Mapping[str, Any] | None = None, *, timeout: float | None = None
    ) -> Any:
        """Any request, including extension methods."""
        return await self.connection.request(method, dict(params or {}), timeout=timeout)

    async def notify(self, method: str, params: Mapping[str, Any] | None = None) -> None:
        await self.connection.notify(method, dict(params or {}))

    # -- initialize ------------------------------------------------------------

    async def initialize(self, *, timeout: float | None = None) -> dict[str, Any]:
        result = await self.request(
            "initialize",
            {
                "protocolVersion": self._protocol_version,
                "clientCapabilities": self._client_capabilities,
                "clientInfo": self._client_info,
            },
            timeout=timeout,
        )
        result = result if isinstance(result, dict) else {}
        capabilities = result.get("agentCapabilities")
        self.agent_capabilities = capabilities if isinstance(capabilities, dict) else {}
        info = result.get("agentInfo")
        self.agent_info = info if isinstance(info, dict) else None
        methods = result.get("authMethods")
        self.auth_methods = methods if isinstance(methods, list) else []
        agreed = result.get("protocolVersion")
        self.protocol_version = agreed if isinstance(agreed, int) else None
        if agreed != self._protocol_version:
            if self._strict_version:
                raise ProtocolVersionError(self._protocol_version, agreed)
            log.warning(
                "asked for ACP version %s, the agent chose %s", self._protocol_version, agreed
            )
        return result

    # -- sessions --------------------------------------------------------------

    def supports(self, capability: str) -> bool:
        """Whether the agent advertised `capability`: `load`, `resume`, `fork`,
        `close`, `list` (or any other `sessionCapabilities` key)."""
        if capability == "load":
            return self.agent_capabilities.get("loadSession") is True
        session = self.agent_capabilities.get("sessionCapabilities")
        return isinstance(session, dict) and isinstance(session.get(capability), dict)

    def _require(self, method: str, capability: str) -> None:
        if not self.supports(capability):
            where = "loadSession" if capability == "load" else f"sessionCapabilities.{capability}"
            raise UnsupportedError(method, where)

    async def _call(
        self,
        method: str,
        params: dict[str, Any],
        meta: Mapping[str, Any] | None,
        extra: Mapping[str, Any],
        timeout: float | None = None,
    ) -> dict[str, Any]:
        params.update(extra)
        if meta is not None:
            params["_meta"] = dict(meta)
        result = await self.request(method, params, timeout=timeout)
        return result if isinstance(result, dict) else {}

    async def new_session(
        self,
        cwd: str | PathLike[str],
        mcp_servers: Iterable[Mapping[str, Any]] = (),
        *,
        meta: Mapping[str, Any] | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        params = {"cwd": str(cwd), "mcpServers": [dict(s) for s in mcp_servers]}
        return await self._call("session/new", params, meta, extra)

    async def load_session(
        self,
        session_id: str,
        cwd: str | PathLike[str],
        mcp_servers: Iterable[Mapping[str, Any]] = (),
        *,
        meta: Mapping[str, Any] | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        self._require("session/load", "load")
        params = {
            "sessionId": session_id,
            "cwd": str(cwd),
            "mcpServers": [dict(s) for s in mcp_servers],
        }
        return await self._call("session/load", params, meta, extra)

    async def resume_session(
        self,
        session_id: str,
        cwd: str | PathLike[str],
        mcp_servers: Iterable[Mapping[str, Any]] = (),
        *,
        meta: Mapping[str, Any] | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        self._require("session/resume", "resume")
        params = {
            "sessionId": session_id,
            "cwd": str(cwd),
            "mcpServers": [dict(s) for s in mcp_servers],
        }
        return await self._call("session/resume", params, meta, extra)

    async def fork_session(
        self,
        session_id: str,
        cwd: str | PathLike[str],
        mcp_servers: Iterable[Mapping[str, Any]] = (),
        *,
        meta: Mapping[str, Any] | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        # `session/fork` is on the schema's unstable track; the capability is
        # `sessionCapabilities.fork`.
        self._require("session/fork", "fork")
        params = {
            "sessionId": session_id,
            "cwd": str(cwd),
            "mcpServers": [dict(s) for s in mcp_servers],
        }
        return await self._call("session/fork", params, meta, extra)

    async def close_session(
        self, session_id: str, *, meta: Mapping[str, Any] | None = None, **extra: Any
    ) -> dict[str, Any]:
        self._require("session/close", "close")
        return await self._call("session/close", {"sessionId": session_id}, meta, extra)

    async def list_sessions(
        self,
        cwd: str | PathLike[str] | None = None,
        cursor: str | None = None,
        *,
        meta: Mapping[str, Any] | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        self._require("session/list", "list")
        params: dict[str, Any] = {}
        if cwd is not None:
            params["cwd"] = str(cwd)
        if cursor is not None:
            params["cursor"] = cursor
        return await self._call("session/list", params, meta, extra)

    async def set_mode(
        self,
        session_id: str,
        mode_id: str,
        *,
        meta: Mapping[str, Any] | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        params = {"sessionId": session_id, "modeId": mode_id}
        return await self._call("session/set_mode", params, meta, extra)

    async def set_config_option(
        self,
        session_id: str,
        config_id: str,
        value: str | bool,
        *,
        meta: Mapping[str, Any] | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        """Set an option; a bool is sent as the schema's `type: "boolean"` form."""
        params: dict[str, Any] = {"sessionId": session_id, "configId": config_id, "value": value}
        if isinstance(value, bool):
            params["type"] = "boolean"
        return await self._call("session/set_config_option", params, meta, extra)

    async def prompt(
        self,
        session_id: str,
        blocks: Iterable[Mapping[str, Any]],
        *,
        meta: Mapping[str, Any] | None = None,
        timeout: float | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        """Run a turn; the result is the whole response (`stopReason`, `_meta`, ...)."""
        params = {"sessionId": session_id, "prompt": [dict(b) for b in blocks]}
        return await self._call("session/prompt", params, meta, extra, timeout)

    def settle_permissions(self, session_id: str) -> None:
        """Answer every permission request of `session_id` still waiting on
        `on_permission` as `cancelled`, as a cancel requires."""
        for future in list(self._permissions.get(session_id, ())):
            future.cancel()

    async def cancel(self, session_id: str, *, meta: Mapping[str, Any] | None = None) -> None:
        """Cancel a turn. Permission requests still waiting on `on_permission` are
        answered `cancelled` first (`settle_permissions`), as the protocol
        requires, then the `session/cancel` notification is sent."""
        self.settle_permissions(session_id)
        params: dict[str, Any] = {"sessionId": session_id}
        if meta is not None:
            params["_meta"] = dict(meta)
        await self.notify("session/cancel", params)

    # -- inbound ---------------------------------------------------------------

    async def _handle_notification(self, method: str, params: Any) -> None:
        if method == "session/update":
            await self._on_update(parse_update(params))
        elif self._on_notification is not None:
            await self._on_notification(method, params)

    async def _handle_request(self, method: str, params: Any) -> Any:
        if method == "session/request_permission":
            return await self._permission(params if isinstance(params, dict) else {})
        if method == "elicitation/create" and self._on_elicitation is not None:
            return await self._on_elicitation(params if isinstance(params, dict) else {})
        if method != "elicitation/create" and self._on_request is not None:
            return await self._on_request(method, params)
        raise MethodNotFoundError(method)

    async def _permission(self, params: dict[str, Any]) -> dict[str, Any]:
        session_id = params.get("sessionId")
        key = session_id if isinstance(session_id, str) else ""
        task: asyncio.Future[dict[str, Any]] = asyncio.ensure_future(self._on_permission(params))
        self._permissions.setdefault(key, set()).add(task)
        try:
            try:
                await asyncio.wait([task])
            except asyncio.CancelledError:
                task.cancel()
                raise
            if task.cancelled():
                return {"outcome": dict(CANCELLED)}
            outcome = task.result()
        finally:
            pending = self._permissions.get(key)
            if pending is not None:
                pending.discard(task)
                if not pending:
                    del self._permissions[key]
        inner = outcome.get("outcome")
        return outcome if isinstance(inner, dict) else {"outcome": outcome}


# -- helpers -------------------------------------------------------------------


def _pick(options: Iterable[Mapping[str, Any]], kind: str) -> dict[str, Any]:
    for option in options:
        if option.get("kind") == kind and isinstance(option.get("optionId"), str):
            return {"outcome": "selected", "optionId": option["optionId"]}
    return dict(CANCELLED)


def allow_once(options: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """The outcome selecting the agent's `allow_once` option, or `cancelled` if it
    offered none (never a broader grant in its place)."""
    return _pick(options, "allow_once")


def reject_once(options: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """The outcome selecting the agent's `reject_once` option, or `cancelled`."""
    return _pick(options, "reject_once")


def _pairs(values: Mapping[str, str] | None) -> list[dict[str, str]]:
    return [{"name": name, "value": value} for name, value in (values or {}).items()]


def mcp_server_http(
    name: str, url: str, headers: Mapping[str, str] | None = None
) -> dict[str, Any]:
    """An HTTP MCP server entry for `mcpServers` (the agent must advertise
    `mcpCapabilities.http`)."""
    return {"type": "http", "name": name, "url": url, "headers": _pairs(headers)}


def mcp_server_stdio(
    name: str,
    command: str,
    args: Sequence[str] = (),
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """A stdio MCP server entry for `mcpServers` (every agent supports it)."""
    return {"name": name, "command": command, "args": list(args), "env": _pairs(env)}
