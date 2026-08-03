"""Typed wrappers for the client→server commands.

23 of the 27 live here; `initialize`, `ping`, `reconnect` and `subscribe` are
connection machinery and live on `AhpClient` itself. Generated from one table
rather than hand-written, so the parity matrix is *derived* from the code and
cannot drift from it. The TypeScript client ships wrappers for twelve; the rest
it leaves to `request()`.

**Channel scoping is the thing to get right.** Seventeen commands are declared
``channel: 'ahp-root://'`` in the protocol's own types and are forced to it here
regardless of what a caller passes. Ten carry a caller-chosen URI. The
membership below is not remembered -- it is asserted against the vendored
``ts/messages.ts`` by ``tests/docs/test_parity_matrix_is_true.py``.

The one that catches people: ``completions`` is **caller-scoped**. Forcing it to
root silently breaks every @-mention picker, and it is the exception all three
reference clients comment on.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from types import EllipsisType
from typing import Any, Final

from agent_host_protocol.channels import ROOT_URI
from agent_host_protocol.types import JsonObject

__all__ = ["CALLER_SCOPED", "COMMANDS", "ROOT_SCOPED", "CommandsMixin"]

#: Declared ``channel: 'ahp-root://'`` upstream. The wrapper overwrites whatever
#: the caller passed, because sending anything else is a protocol error the host
#: is entitled to reject.
ROOT_SCOPED: Final[frozenset[str]] = frozenset(
    {
        "initialize",
        "ping",
        "reconnect",
        "createResourceWatch",
        "listSessions",
        "resourceRead",
        "resourceWrite",
        "resourceList",
        "resourceCopy",
        "resourceDelete",
        "resourceMove",
        "resourceResolve",
        "resourceMkdir",
        "resourceRequest",
        "authenticate",
        "resolveSessionConfig",
        "sessionConfigCompletions",
    }
)

#: Carry a caller-chosen URI. `completions` is here, and that is the point.
CALLER_SCOPED: Final[frozenset[str]] = frozenset(
    {
        "subscribe",
        "createSession",
        "disposeSession",
        "createChat",
        "disposeChat",
        "createTerminal",
        "disposeTerminal",
        "fetchTurns",
        "completions",
        "invokeChangesetOperation",
    }
)

#: Every client→server request. 27 of them; `unsubscribe` and `dispatchAction`
#: are notifications and live on the client itself.
COMMANDS: Final[frozenset[str]] = ROOT_SCOPED | CALLER_SCOPED


def _omit_none(params: Mapping[str, Any]) -> JsonObject:
    """Drop absent optionals rather than serialising them as ``null``.

    Shallow on purpose. A *nested* explicit ``null`` is load-bearing -- a JS
    unconditional spread writes it through, and reducers act on the difference --
    so a recursive strip would silently rewrite the wire.
    """
    return {key: value for key, value in params.items() if value is not None}


class CommandsMixin:
    """23 wrappers, mixed into :class:`~agent_host_client.client.AhpClient`.

    The other four of the 27 commands -- `initialize`, `ping`, `reconnect`,
    `subscribe` -- are connection machinery and live on `AhpClient` itself.
    Kept in its own module so the command surface can be read, counted and
    tested without wading through that machinery.
    """

    async def request(  # pragma: no cover - provided by AhpClient
        self,
        method: str,
        params: Mapping[str, Any],
        *,
        timeout: float | EllipsisType | None = ...,
    ) -> Any: ...

    async def _root(self, method: str, params: Mapping[str, Any]) -> JsonObject:
        result = await self.request(method, {**_omit_none(params), "channel": ROOT_URI})
        # A `result: null` is legal for several commands; callers get `{}` so
        # they never have to distinguish "no payload" from "no answer".
        return result if isinstance(result, dict) else {}

    async def _scoped(self, method: str, channel: str, params: Mapping[str, Any]) -> JsonObject:
        result = await self.request(method, {**_omit_none(params), "channel": channel})
        return result if isinstance(result, dict) else {}

    # ── sessions ─────────────────────────────────────────────────────────────

    async def list_sessions(
        self, *, limit: int | None = None, cursor: str | None = None
    ) -> JsonObject:
        """One page of sessions.

        Cursors are **opaque and server-defined**: the spec says clients MUST NOT
        parse, modify, or persist them across connections, and an unrecognised
        one is `InvalidParams`. VS Code sends no pagination params at all, so a
        client that only mirrors VS Code silently truncates at the host's default
        page size.
        """
        return await self._root("listSessions", {"limit": limit, "cursor": cursor})

    async def create_session(
        self,
        session: str,
        *,
        provider: str | None = None,
        working_directories: Sequence[str] | None = None,
        config: Mapping[str, Any] | None = None,
        active_client: Mapping[str, Any] | None = None,
        progress_token: str | None = None,
        fork: Mapping[str, Any] | None = None,
        **extra: Any,
    ) -> JsonObject:
        """Create a session on a client-minted URI.

        ``provider`` is optional because `CreateSessionParams.provider` is
        (`provider?: string`): a *fork* inherits it from the source session, so
        requiring it here made the fork flow unexpressible without lying to the
        annotation. ``workingDirectories`` is **plural** since 0.7.0.
        ``progressToken`` is what makes `root/progress` fire at all -- a client
        that never sends one has a progress surface that can never receive
        anything.
        """
        return await self._scoped(
            "createSession",
            session,
            {
                "provider": provider,
                "workingDirectories": list(working_directories)
                if working_directories is not None
                else None,
                "config": dict(config) if config is not None else None,
                "activeClient": dict(active_client) if active_client is not None else None,
                "progressToken": progress_token,
                "fork": dict(fork) if fork is not None else None,
                **extra,
            },
        )

    async def dispose_session(self, session: str) -> JsonObject:
        return await self._scoped("disposeSession", session, {})

    async def resolve_session_config(self, **extra: Any) -> JsonObject:
        return await self._root("resolveSessionConfig", extra)

    async def session_config_completions(self, **extra: Any) -> JsonObject:
        return await self._root("sessionConfigCompletions", extra)

    # ── chats ────────────────────────────────────────────────────────────────

    async def create_chat(self, session: str, chat: str, **extra: Any) -> JsonObject:
        """Create a chat inside *session*.

        **Two URIs, and they are different things.** `CreateChatParams` requires
        both: `channel` is the *session* that will contain the chat, `chat` is
        the new chat's own URI. This took one argument until the interop run,
        and `_scoped` wrote it into `channel` -- so no `chat` key was ever
        emitted, every host answered `-32602`, and there was no call shape that
        worked: `channel=` in the kwargs was overwritten, `chat=` collided with
        the positional. The only caller-scoped command whose caller-chosen URI
        is *not* the thing being created.

        The spec contradicts itself on who allocates the URI -- `chat-channel.md`
        says the server does, while `CreateChatParams.chat` is documented
        client-chosen and VS Code sends one. We send one.
        """
        return await self._scoped("createChat", session, {"chat": chat, **extra})

    async def dispose_chat(self, chat: str) -> JsonObject:
        """Dispose a chat.

        `chat-channel.md` claims the protocol exposes no such command. It is in
        `CommandMap` with a `DisposeChatParams`, and the reference host
        implements it. The types win.
        """
        return await self._scoped("disposeChat", chat, {})

    async def fetch_turns(
        self, chat: str, *, cursor: str | None = None, **extra: Any
    ) -> JsonObject:
        """Load older turns into a chat.

        *chat* is the **chat channel** -- `FetchTurnsParams.channel` is
        documented "Chat URI", the URI from `ChatState` / the session's
        `defaultChat`, never the session URI. The reference host rejects a
        session URI with InvalidParams ("… is not a chat channel"), so a
        signature that named this `session` taught every caller the argument
        that cannot work.

        **The result is empty**, and that is not a stub. The host MUST dispatch
        `chat/turnsLoaded` *before* responding, so the turns arrive through the
        action stream and the mirror -- a caller that awaits this and reads the
        result gets nothing, and one whose mirror is not wired before the call
        loses them entirely.
        """
        return await self._scoped("fetchTurns", chat, {"cursor": cursor, **extra})

    async def completions(self, channel: str, **extra: Any) -> JsonObject:
        """Completion items for a partially-typed input.

        **Caller-scoped**, unlike every other query-shaped command. Forcing this
        to root silently breaks @-mention pickers. Note that offsets in the
        params and the results are **UTF-16 code units**, not Python string
        indices.
        """
        return await self._scoped("completions", channel, extra)

    # ── terminals ────────────────────────────────────────────────────────────

    async def create_terminal(
        self, terminal: str, *, claim: Mapping[str, Any], **extra: Any
    ) -> JsonObject:
        """Create a terminal on a client-minted URI.

        ``claim`` is keyword-**required** because `CreateTerminalParams` requires
        it and because omitting it is not a recoverable mistake: it is the
        terminal's initial owner, and the only claim that lets this client type
        is its own ``{kind: 'client', clientId}``. A host that receives no claim
        answers `InvalidParams`, so the alternative to this signature is learning
        the field name from a -32602.

        Refusal is `PermissionDenied` (-32009), never `MethodNotFound`: a host
        with no terminal backend has the method and declined the request, and
        -32601 would tell a client to stop asking for terminals at all.
        """
        return await self._scoped("createTerminal", terminal, {"claim": dict(claim), **extra})

    async def dispose_terminal(self, terminal: str) -> JsonObject:
        """Dispose a terminal and kill its process.

        `DisposeTerminalParams` carries a channel and nothing else -- there is no
        ownership rule attached to it anywhere upstream, and the sibling host
        deliberately does not gate it on the claim: a session-claimed terminal is
        held by no client, so a gate would make it immortal.
        """
        return await self._scoped("disposeTerminal", terminal, {})

    # ── changesets ───────────────────────────────────────────────────────────

    async def invoke_changeset_operation(
        self, changeset: str, *, operation_id: str, **extra: Any
    ) -> JsonObject:
        """Run a server-declared changeset operation.

        ``operation_id`` is keyword-**required**, for the reason `actions.py`'s
        docstring gives about `turnId`: it is required by
        `InvokeChangesetOperationParams`, the host answers -32602 without it,
        and a `**extra`-only signature type-checks clean under `mypy --strict`
        while omitting it. Every other command carrying a required field --
        `create_session`, `create_chat`, `create_terminal` -- names it. This one
        did not, and it is the sixth call site that pattern exists to stop.
        """
        return await self._scoped(
            "invokeChangesetOperation", changeset, {"operationId": operation_id, **extra}
        )

    # ── resources (forward direction) ────────────────────────────────────────
    #
    # The same nine methods exist in reverse, host→client; `serve/` answers
    # those. Symmetry is the reason they are not named `read_file`.

    async def resource_read(self, uri: str, **extra: Any) -> JsonObject:
        return await self._root("resourceRead", {"uri": uri, **extra})

    async def resource_write(self, uri: str, **extra: Any) -> JsonObject:
        return await self._root("resourceWrite", {"uri": uri, **extra})

    async def resource_list(self, uri: str, **extra: Any) -> JsonObject:
        return await self._root("resourceList", {"uri": uri, **extra})

    async def resource_copy(self, source: str, destination: str, **extra: Any) -> JsonObject:
        return await self._root(
            "resourceCopy", {"source": source, "destination": destination, **extra}
        )

    async def resource_delete(self, uri: str, **extra: Any) -> JsonObject:
        return await self._root("resourceDelete", {"uri": uri, **extra})

    async def resource_move(self, source: str, destination: str, **extra: Any) -> JsonObject:
        return await self._root(
            "resourceMove", {"source": source, "destination": destination, **extra}
        )

    async def resource_resolve(self, uri: str, **extra: Any) -> JsonObject:
        """`stat` + `realpath`.

        Upstream calls the result the canonical URI after symlink resolution;
        the reference host returns the requested URI verbatim and ignores
        `followSymlinks`. Do not assume canonicalisation happened.
        """
        return await self._root("resourceResolve", {"uri": uri, **extra})

    async def resource_mkdir(self, uri: str, **extra: Any) -> JsonObject:
        return await self._root("resourceMkdir", {"uri": uri, **extra})

    async def resource_request(self, uri: str, **extra: Any) -> JsonObject:
        """Ask for access rather than retrying a denial forever."""
        return await self._root("resourceRequest", {"uri": uri, **extra})

    async def create_resource_watch(self, uri: str, **extra: Any) -> JsonObject:
        """Ask the host to watch a path.

        The returned ``channel`` is **receiver-assigned** and opaque -- do not
        derive it, subscribe to what comes back.
        """
        return await self._root("createResourceWatch", {"uri": uri, **extra})

    # ── auth ─────────────────────────────────────────────────────────────────

    async def authenticate(
        self, *, resource: str, token: str, scopes: Sequence[str] | None = None
    ) -> JsonObject:
        """Push a token for an upstream service the agent talks to.

        **Not a login.** It never gates the AHP connection itself; connection
        admission is explicitly outside the wire protocol.
        """
        return await self._root(
            "authenticate",
            {
                "resource": resource,
                "token": token,
                "scopes": list(scopes) if scopes is not None else None,
            },
        )
