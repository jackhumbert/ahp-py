"""A worked `Policy` for a multi-user deployment. An example, not a core concept.

:class:`~agent_host_server.core.policy.LoopbackSingleUserPolicy` permits
everything, which is right for what it is named after and useless as a starting
point for anything else. An embedder partitioning sessions between users
otherwise starts from a blank file with seven hooks and no map of which paths
consult which.

**Partitioning is not one check.** The obvious guess -- add an ownership test to
`may_create_session` -- protects nothing, because creation is not how a peer
reaches somebody else's session. It is spread across three hooks, and the
`may_see_channel` docstring says why:

> `subscribe` is the only major command the spec gives no `PermissionDenied`
> path for, and `listSessions` has no filter parameter -- so if sessions must be
> partitioned, it happens here.

So `may_see_channel` does the work for both catalogue visibility *and* channel
access, `may_dispatch` decides who may act on a session they can see, and
`may_create_session` only records the owner.

**The identity comes from the embedder.** `principal_of` is handed a
:class:`ConnectionInfo` and returns whatever the deployment calls a user. For a
host behind an identity-aware proxy that is a forwarded header; for a host with
per-user connection tokens it is the token. This module has no opinion, because
a library that guessed would be wrong for everyone -- and a forwarded header is
evidence only if the socket cannot be reached except through the proxy that set
it, which is the embedder's problem to guarantee.

**`clientId` is not an identity.** It is client-asserted, validated nowhere, and
`reconnect` resumes on it alone with no credential. A policy that partitions on
it partitions on nothing.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from agent_host_server.core.policy import ConnectionInfo

__all__ = ["OwnedSessionPolicy", "principal_from_header"]


def principal_from_header(header: str) -> Callable[[ConnectionInfo], str | None]:
    """Read a principal out of one forwarded request header.

    The header name is the embedder's to choose, and trusting it is the
    embedder's to justify: this returns whatever the proxy said. If the socket
    is reachable without traversing that proxy, a peer sets the header itself.
    """
    wanted = header.lower()

    def read(info: ConnectionInfo) -> str | None:
        if info.headers is None:
            return None
        value = info.headers.get(wanted)
        return value or None

    return read


class OwnedSessionPolicy:
    """Every session belongs to the principal that created it.

    Sessions, their chats, their annotations and their changesets are visible
    and writable only to their owner. The root channel is visible to everyone --
    it carries the agent list and the session *count*, not session identities --
    and `listSessions` is filtered by `may_see_channel`, which is the only place
    it can be.

    Resources and root config are refused outright. A multi-user host has no
    single filesystem to expose and no user who owns the host-wide settings; an
    embedder that means to allow either should say so explicitly rather than
    inherit it.
    """

    def __init__(
        self,
        principal_of: Callable[[ConnectionInfo], str | None],
        *,
        allow_resources: bool = False,
    ) -> None:
        self.principal_of = principal_of
        self.allow_resources = allow_resources
        #: Channel URI -> owning principal. Every channel a session owns is
        #: registered here, not just the session URI, because a peer reaches a
        #: chat by naming the chat.
        self._owners: dict[str, str] = {}

    # ─── ownership bookkeeping ───────────────────────────────────────────

    def claim(self, principal: str, *channels: str) -> None:
        """Record that `principal` owns these channels."""
        for channel in channels:
            self._owners[channel] = principal

    def release(self, *channels: str) -> None:
        for channel in channels:
            self._owners.pop(channel, None)

    def owner_of(self, channel: str) -> str | None:
        """The owner of a channel, or of the session it belongs to.

        The prefix walk is over channels this policy was *told about*, never a
        parse of the URI -- session and chat URIs are client-chosen and opaque,
        and a derived one would be a second, unchecked way to name a session.
        """
        owner = self._owners.get(channel)
        if owner is not None:
            return owner
        for owned, principal in self._owners.items():
            if channel.startswith(f"{owned}/"):
                return principal
        return None

    # ─── the hooks ───────────────────────────────────────────────────────

    def authorize_connection(self, info: ConnectionInfo) -> bool:
        """No principal, no connection. An anonymous peer is not a user."""
        return self.principal_of(info) is not None

    def may_see_channel(self, info: ConnectionInfo, channel: str) -> bool:
        if channel == "ahp-root://":
            return True
        owner = self.owner_of(channel)
        if owner is None:
            # An unowned channel is one nobody claimed. Refused rather than
            # shared: the failure mode of the other choice is a leak.
            return False
        return owner == self.principal_of(info)

    def may_dispatch(self, info: ConnectionInfo, channel: str, action: Mapping[str, Any]) -> bool:
        return self.may_see_channel(info, channel)

    def may_create_session(self, info: ConnectionInfo, params: Mapping[str, Any]) -> bool:
        return self.principal_of(info) is not None

    def may_access_resource(self, info: ConnectionInfo, operation: str, uri: str) -> bool:
        return self.allow_resources and operation != "write"

    def may_create_terminal(self, info: ConnectionInfo, params: Mapping[str, Any]) -> bool:
        """Refused. A terminal is arbitrary command execution and this policy
        exists for hosts serving people who do not trust each other."""
        return False

    def may_restore_session(self, session: Mapping[str, Any]) -> bool:
        """Refused. A restored session has no owner until somebody claims it,
        and `may_see_channel` refuses unowned channels -- so restoring one would
        produce a session nobody, including its author, can reach. An embedder
        that persists ownership alongside the session overrides this."""
        return False

    def may_push_token(self, info: ConnectionInfo, resource: str) -> bool:
        """Refused. The token store is host-global, so a token one user pushes
        is used on every user's behalf -- a deployment that partitions users
        partitions token stores, and one shared store cannot."""
        return False

    def may_invoke_operation(self, info: ConnectionInfo, changeset: str, operation: str) -> bool:
        return self.may_see_channel(info, changeset)

    def may_set_root_config(self, info: ConnectionInfo, key: str, value: Any) -> bool:
        """Refused. Nobody owns a shared host's settings."""
        return False

    def may_grant_working_directory(
        self, info: ConnectionInfo, session: str, directory: str
    ) -> bool:
        return self.allow_resources and self.may_see_channel(info, session)
