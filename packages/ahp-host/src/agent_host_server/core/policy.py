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

__all__ = [
    "ConnectionInfo",
    "LoopbackSingleUserPolicy",
    "Policy",
    "TracksChannels",
]


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

    def may_create_terminal(self, info: ConnectionInfo, params: Mapping[str, Any]) -> bool:
        """Whether this peer may open a terminal.

        The sharpest hook here, even with no backend installed. A terminal is
        arbitrary command execution with a client-chosen working directory, and
        `Policy` cannot authenticate a peer at all -- `reconnect` resumes on a
        client-asserted `clientId` with no credential. A host that installs a
        real backend without also narrowing this has granted a shell to anyone
        who can reach the socket.
        """
        ...

    def may_restore_session(self, session: Mapping[str, Any]) -> bool:
        """Whether a session persisted by a previous run may come back.

        Called at startup with the stored record, before any connection exists
        -- so it takes no :class:`ConnectionInfo`. It is the gate on the one
        thing a restart makes possible: state written by a host that may have
        been configured differently, restored into one that is not.
        """
        ...

    def may_push_token(self, info: ConnectionInfo, resource: str) -> bool:
        """Whether this peer may push a credential for `resource`.

        The only trust decision in the `authenticate` flow, and it is a real
        one: the token store is host-global, so a token pushed by one client is
        used by the agent on every client's behalf. A deployment that partitions
        users partitions token stores, and this is where it says so.
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

    def may_create_terminal(self, info: ConnectionInfo, params: Mapping[str, Any]) -> bool:
        return True

    def may_restore_session(self, session: Mapping[str, Any]) -> bool:
        return True

    def may_push_token(self, info: ConnectionInfo, resource: str) -> bool:
        return True

    def may_invoke_operation(self, info: ConnectionInfo, changeset: str, operation: str) -> bool:
        return True

    def may_set_root_config(self, info: ConnectionInfo, key: str, value: Any) -> bool:
        return True

    def may_grant_working_directory(
        self, info: ConnectionInfo, session: str, directory: str
    ) -> bool:
        return True


@runtime_checkable
class TracksChannels(Protocol):
    """A policy that needs to know which channels exist, and who made them.

    Feature-detected, never required -- the same shape as
    :class:`~agent_host_server.core.resources.WritableResourceProvider`. A
    policy with no opinion about ownership implements nothing and is unaffected.

    This exists because ownership was **inexpressible**.
    :class:`~agent_host_server.core.policies.OwnedSessionPolicy` keeps a
    channel-to-principal map and nothing could populate it:
    :meth:`Policy.may_create_session` is consulted BEFORE the host mints a
    session's chat and annotations channels, so at decision time those URIs do
    not exist, and nothing told the policy afterwards. The shipped multi-user
    example could not do the thing it was an example of.

    The host calls these for every channel it registers or drops on a peer's
    behalf, so the layer making trust decisions learns the URIs without parsing
    one (invariant 15) or reaching into private state.
    """

    def channel_created(
        self, info: ConnectionInfo | None, channel: str, *, session: str | None
    ) -> None:
        """A channel now exists.

        *info* is the connection that caused it, or ``None`` when the host made
        it on its own account -- the root channel, or a session restored from a
        store before any peer connected. *session* is the session the channel
        belongs to, or ``None`` for the session channel itself and for root.

        Called BEFORE the first `subscribe` can arrive, so a policy that
        refuses unowned channels never has a window where its own session is
        unreachable.
        """
        ...

    def channel_dropped(self, channel: str) -> None:
        """A channel is gone. Forget anything recorded about it."""
        ...

    def session_metadata(self, session: str) -> Mapping[str, Any] | None:
        """What to persist alongside this session, or ``None``.

        Round-tripped verbatim onto :class:`StoredSession.metadata` and never
        interpreted by the library -- the same treatment `resume_state` gets
        for the provider. It comes back on the payload handed to
        :meth:`Policy.may_restore_session`, which is where a policy re-claims
        what it owned.

        Without it, durability and partitioning could not both be on: a stored
        session carried everything except who it belonged to, so a restored one
        had no owner and `may_see_channel` refused it to everybody, including
        its author.
        """
        ...
