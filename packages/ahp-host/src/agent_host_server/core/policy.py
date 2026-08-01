"""The trust boundary.

AHP defines **no security model**, and says so: connection admission is
explicitly outside the wire protocol, and there is no server capability object,
so a host cannot negotiate a dangerous surface away -- it can only refuse it.
The `authenticate` command is not a login; it forwards tokens for services the
*agent* talks to.

So this library draws the line here. The core guarantees **protocol**
invariants: sequencing, reducer parity, state-transition validity, action-origin
stamping, client-dispatch gating. Every **trust** decision -- who may connect,
who may see which channel, who may approve a tool call -- is delegated to a
policy the embedding application supplies. There is no default, and
:class:`~agent_host_server.core.host.Host` will not construct without one.

That is deliberately inconvenient. Both known existing hosts are
single-trust-domain, and a Python library that *looks* safe to expose would be
worse than one that says it is not.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

__all__ = ["ConnectionInfo", "LoopbackSingleUserPolicy", "Policy"]


@dataclass(frozen=True)
class ConnectionInfo:
    """What is known about a peer at admission time.

    ``client_id`` is **client-asserted and validated nowhere** by the protocol,
    and ``reconnect`` resumes on it alone with no credential. Treat it as an
    identifier, never as an authenticator.
    """

    client_id: str
    peer: str | None = None
    token: str | None = None
    headers: Mapping[str, str] | None = None


@runtime_checkable
class Policy(Protocol):
    """Every trust decision the core refuses to make for you."""

    def authorize_connection(self, info: ConnectionInfo) -> bool:
        """Whether this peer may complete ``initialize`` at all."""
        ...

    def may_see_channel(self, info: ConnectionInfo, channel: str) -> bool:
        """Whether this peer may subscribe to, or receive, a channel.

        Note `subscribe` is the only major command the spec gives no
        `PermissionDenied` path for, and `listSessions` has no filter parameter
        -- so if sessions must be partitioned, it happens here.
        """
        ...

    def may_dispatch(self, info: ConnectionInfo, channel: str, action: Mapping[str, Any]) -> bool:
        """Whether this peer may originate this action.

        Distinct from client-dispatchability, which is a protocol invariant
        checked unconditionally before this is consulted. This is where a
        deployment decides, for example, that only one client may approve a tool
        call -- the protocol's own validation table conditions
        `chat/toolCallConfirmed` solely on the call's status, never on identity.
        """
        ...

    def may_create_session(self, info: ConnectionInfo, params: Mapping[str, Any]) -> bool:
        """Whether this peer may create a session.

        A session is arbitrary code execution against a workspace.
        """
        ...

    def may_access_resource(self, info: ConnectionInfo, operation: str, uri: str) -> bool:
        """Whether this peer may `resolve`, `read` or `list` a resource.

        The **only** per-resource gate that exists. Every `resource*` command
        targets `ahp-root://`, so `may_see_channel` sees the same URI for all of
        them and cannot distinguish a source file from a private key.

        `uri` is the **canonical** one -- after the provider has resolved
        symlinks -- so this decides about the file that will actually be read
        rather than the name the peer used to ask for it.
        """
        ...

    def may_invoke_operation(self, info: ConnectionInfo, changeset: str, operation: str) -> bool:
        """Whether this peer may run a changeset operation.

        An operation is whatever the embedder registered -- there are no
        built-in ones, because the names VS Code uses (`commit`, `create-pr`,
        `discard-changes`) include a credentialed network call and an
        irreversible destruction of work.
        """
        ...

    def may_set_root_config(self, info: ConnectionInfo, key: str, value: Any) -> bool:
        """Whether this peer may change a host-wide configuration value.

        Consulted **after** the schema has already rejected unknown keys,
        read-only properties and type mismatches -- so this is for decisions the
        schema cannot express, like "only the client that owns this host may
        change it". A host that publishes no config schema never reaches here.
        """
        ...

    def may_grant_working_directory(
        self, info: ConnectionInfo, session: str, directory: str
    ) -> bool:
        """Whether this peer may give the agent tool access to `directory`.

        `session/workingDirectorySet` is client-dispatchable, so without this a
        peer names the filesystem roots the agent operates on. The reducer
        applies such an action verbatim -- upstream is explicit that the
        `immutablePrimary` guarantee "lives at the dispatch-validation / host
        acceptance layer, not in the reducer".

        Today the set is state the host merely records. The moment the
        `resource*` family or a terminal backend lands it becomes an authorization
        decision, so it is gated now rather than retrofitted onto an established
        wire behaviour.
        """
        ...


class LoopbackSingleUserPolicy:
    """Everything permitted. **Single trust domain only.**

    Correct for a host bound to loopback, a Unix socket or a named pipe, where
    the OS is the access control -- which is exactly what the VS Code reference
    host does, adding a bearer token on the WebSocket upgrade.

    It is *not* multi-tenant and is not safe to expose to an untrusted network.
    The name is deliberately unwieldy so nobody reaches for it by accident.
    """

    def authorize_connection(self, info: ConnectionInfo) -> bool:
        return True

    def may_see_channel(self, info: ConnectionInfo, channel: str) -> bool:
        return True

    def may_dispatch(self, info: ConnectionInfo, channel: str, action: Mapping[str, Any]) -> bool:
        return True

    def may_create_session(self, info: ConnectionInfo, params: Mapping[str, Any]) -> bool:
        return True

    def may_access_resource(self, info: ConnectionInfo, operation: str, uri: str) -> bool:
        return True

    def may_invoke_operation(self, info: ConnectionInfo, changeset: str, operation: str) -> bool:
        return True

    def may_set_root_config(self, info: ConnectionInfo, key: str, value: Any) -> bool:
        return True

    def may_grant_working_directory(
        self, info: ConnectionInfo, session: str, directory: str
    ) -> bool:
        return True
