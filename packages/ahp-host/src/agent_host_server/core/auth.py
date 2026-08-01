"""The `authenticate` command: a token courier, not a login.

**`authenticate` does not authenticate the peer.** It is the client handing the
host a bearer token for some *upstream* service the **agent** talks to -- a
GitHub API, an MCP server -- obtained out-of-band through an OAuth flow the host
never starts, never redirects, and holds no client secret for. Nothing in this
module gates the AHP connection: a peer that never calls `authenticate` still
gets `initialize`, `subscribe`, `createSession` and the `resource*` family.
Connection admission is :class:`~agent_host_server.core.policy.Policy`, and the
spec puts it outside the wire protocol entirely. This is the single most misread
thing about the command, and reading it as a login produces a host that believes
it is protected and is not.

## Tokens do not leak from this module

`core/wirelog.py` redacts credentials on their way to disk; this is the
in-process half. A pushed token is wrapped in :class:`BearerToken`, whose
``repr`` -- and so ``str``, f-strings and ``%s`` -- is a fixed marker, whose
``__slots__`` leaves no ``__dict__`` for `vars()` to hand out, and whose only
exit is :meth:`BearerToken.reveal`. A :class:`TokenGrant` in a traceback, a log
line or an exception message therefore carries the resource and not the secret.
`reveal` is named for grepping: every disclosure is one search away.

:func:`auth_required` takes no token argument at all. A JSON-RPC error is the
worst available place for a credential -- it travels back to the peer, into its
logs, and into whatever bug report the user pastes it into.

## Host-global, with the client recorded -- and an unresolved contradiction

Upstream contradicts itself here. `docs/specification/authentication.md`
justifies the command's existence with "authentication is per-resource, not
per-connection ... clients may authenticate for multiple resources
independently", and keeps auth status out of root state *because* it is
per-connection. The reference host (`agentHostAuthenticationService.ts`) then
keys a single host-global `Map` and is handed no client identity to key by --
and `AuthenticateParams` carries none either, so a per-connection store is not
something a conformant client could observe.

We match the reference on the wire -- **one host-global store** -- and record
the pushing `clientId` on each grant. The consequence is explicit, and tested:
**a second client's agent runs on the token the first client pushed.** That is
correct for the single-trust-domain host both known implementations are, and
wrong the moment a host is multi-tenant, which is precisely why `Policy` exists
and has no default. An embedder that needs partitioning holds one
:class:`TokenStore` per trust domain; this module will not pretend to do it for
them.

This belongs upstream as an open protocol question (`docs/research.md` §11), not
in a private divergence.

## An absent `scopes` counts as satisfied -- deliberately

`AuthenticateParams.scopes` is optional, and the reference treats a token stored
without scopes as covering any scope request (its "compatibility for clients
that resolved the right token before scopes were forwarded"). Read literally,
that lets a client clear an `insufficientScope` challenge without proving it
obtained anything new.

We keep it, on by default, because **the host is not the resource server.**
Scopes here are a hint that lets the host skip a needless re-prompt; the
authority that actually enforces them is the upstream service, which answers
403 whatever we believed. Refusing a token on scope grounds turns someone else's
authorization decision into ours, on data we cannot verify, and its failure mode
is a client re-prompting forever because it has no other token to send.

Two things narrow it. :func:`scopes_satisfied` distinguishes an **absent**
`scopes` from an **empty** one -- the reference normalises both to `[]`, so a
client honestly reporting "this token grants nothing" gets the same free pass as
a legacy one; here it does not. And the fallback is a constructor flag
(`unscoped_satisfies_any=False`) for a host that has decided it *is* the
enforcement point.

## Keyed by resource

The reference keys `(resource, scopes)`, so it accumulates one entry per scope
set a client ever pushed and evicts none of them. We key by `resource` alone: a
second push replaces the first, which is what a refresh means, and one live
credential per resource is a smaller thing to have to keep secret. The composite
key only earns its complexity under 0.6.0 step-up auth, which this host does not
implement.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Literal

from agent_host_server.core.errors import AhpError
from agent_host_server.types import AHP_ERROR_CODES, ROOT_CHANNEL

__all__ = [
    "AUTH_REQUIRED_METHOD",
    "REDACTED_TOKEN",
    "AuthRequiredReason",
    "BearerToken",
    "ProtectedResource",
    "TokenGrant",
    "TokenStore",
    "auth_required",
    "auth_required_params",
    "scopes_satisfied",
]

#: The ephemeral server-to-client notification. Not replayed on reconnect --
#: like every protocol notification it is outside the `serverSeq` log, so a
#: client that missed one re-derives the requirement from root state.
AUTH_REQUIRED_METHOD: Final = "auth/required"

#: Stands in for a bearer token everywhere one would otherwise be printed.
REDACTED_TOKEN: Final = "<bearer token redacted>"

#: `AuthRequiredReason`. `required` is "no token yet"; `expired` is "the one you
#: gave me stopped working". There is no third value in 0.7.0 -- the
#: `insufficientScope` kind lives on `McpAuthRequiredReason`, in the step-up
#: branch this host does not implement.
AuthRequiredReason = Literal["required", "expired"]

#: Fields of `ProtectedResourceMetadata` this module models. Everything else
#: RFC 9728 defines (`jwks_uri`, `bearer_methods_supported`, ...) is carried
#: verbatim in `ProtectedResource.extra`: a host never reads them, and a partial
#: model that looked complete would be worse than one that says what it covers.
_MODELLED: Final = frozenset(
    {"resource", "resource_name", "authorization_servers", "scopes_supported", "required"}
)


@dataclass(frozen=True)
class ProtectedResource:
    """One `ProtectedResourceMetadata` entry, as published on `AgentInfo`.

    RFC 9728 shaped, so the field names are `snake_case` on the wire while the
    rest of AHP is `camelCase`. That is upstream's choice, not a slip: the point
    of reusing the RFC's shape is that a client can feed it to the same OAuth
    code path it already uses for MCP.

    `authorization_servers` and `scopes_supported` are ``None`` when the key is
    absent and ``()`` when the peer sent an empty array; the two are distinct on
    the wire and stay distinct here (ADR 0001).
    """

    resource: str
    resource_name: str | None = None
    authorization_servers: Sequence[str] | None = None
    scopes_supported: Sequence[str] | None = None
    #: `required=False` means the agent works unauthenticated but does more with
    #: a token. Absent means required -- see :meth:`from_wire`.
    required: bool = True
    #: RFC 9728 fields this module does not model, preserved verbatim.
    extra: Mapping[str, Any] = field(default_factory=dict)

    def to_wire(self) -> dict[str, Any]:
        # `extra` first, so a pass-through key can never shadow a modelled one.
        wire: dict[str, Any] = dict(self.extra)
        wire["resource"] = self.resource
        if self.resource_name is not None:
            wire["resource_name"] = self.resource_name
        if self.authorization_servers is not None:
            wire["authorization_servers"] = list(self.authorization_servers)
        if self.scopes_supported is not None:
            wire["scopes_supported"] = list(self.scopes_supported)
        # Emitted even when true, though the spec lets it default. `required` is
        # what decides whether a client blocks the user with an auth prompt
        # before the agent can be used at all, and we know the answer -- leaving
        # it to someone else's defaulting is a needless bet.
        wire["required"] = self.required
        return wire

    @classmethod
    def from_wire(cls, wire: Mapping[str, Any]) -> ProtectedResource:
        """Read metadata back off the wire. `resource` is the one REQUIRED field."""
        resource = wire.get("resource")
        if not isinstance(resource, str):
            raise ValueError("ProtectedResourceMetadata.resource is required and must be a string")
        servers = wire.get("authorization_servers")
        scopes = wire.get("scopes_supported")
        return cls(
            resource=resource,
            resource_name=wire.get("resource_name"),
            authorization_servers=None if servers is None else list(servers),
            scopes_supported=None if scopes is None else list(scopes),
            # Only a literal `false` turns the requirement off. Absent means
            # required per the spec, and an explicit `null` -- or anything else
            # a peer put there -- fails closed the same way.
            required=wire.get("required") is not False,
            extra={k: v for k, v in wire.items() if k not in _MODELLED},
        )


class BearerToken:
    """An opaque credential a client pushed for some upstream service.

    The type exists to make a token hard to leak *by accident*. It is not
    encryption and not a secure enclave: the process holds the string, and
    anything that can call :meth:`reveal` can print it. What it removes is the
    accidental path -- the log line, the traceback, the ``repr`` of a container
    that happens to hold one.
    """

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        """The raw bearer string. Every call site is a disclosure; grep for it."""
        return self._value

    def __repr__(self) -> str:
        return REDACTED_TOKEN


@dataclass(frozen=True)
class TokenGrant:
    """One stored `authenticate` push.

    Safe to `repr` -- the token redacts itself, so the dataclass `repr` a
    traceback or a debug log produces carries the resource and not the secret.
    """

    resource: str
    token: BearerToken
    #: Scopes the client says the token grants. ``None`` means the client sent
    #: no `scopes` key at all, which is **not** the same as ``()`` -- see
    #: :func:`scopes_satisfied`. Stored in the order given, undeduplicated: this
    #: is the peer's claim, and normalising it loses information we did not need
    #: to spend.
    scopes: tuple[str, ...] | None = None
    #: The `clientId` of the connection that pushed it. Recorded for audit and
    #: never used to partition the store -- see the module docstring.
    client_id: str | None = None


def scopes_satisfied(
    granted: Sequence[str] | None,
    required: Iterable[str],
    *,
    unscoped_satisfies_any: bool = True,
) -> bool:
    """Whether a token granting *granted* covers *required*.

    **Advisory.** It answers "would asking this client for another token be
    pointless?", not "is this access permitted?" -- the upstream resource server
    is the only authority on the second question, and it is not consulted here.

    ``granted is None`` -- the client sent no `scopes` -- falls back to
    *unscoped_satisfies_any*. ``granted == ()`` does not: a client that
    explicitly reports an empty grant is telling us the token covers nothing,
    and taking it at its word costs nothing a legacy client relies on.
    """
    needed = set(required)
    if not needed:
        return True
    if granted is None:
        return unscoped_satisfies_any
    return needed <= set(granted)


class TokenStore:
    """Bearer tokens pushed via `authenticate`, keyed by resource identifier.

    Host-global and deliberately not partitioned by connection; the pushing
    `clientId` is recorded, not keyed on. See the module docstring for why, and
    for what that costs a multi-tenant host.
    """

    def __init__(self, *, unscoped_satisfies_any: bool = True) -> None:
        self._grants: dict[str, TokenGrant] = {}
        #: Whether a grant with no declared scopes satisfies a scope request.
        self.unscoped_satisfies_any = unscoped_satisfies_any

    def push(
        self,
        resource: str,
        token: str,
        *,
        scopes: Iterable[str] | None = None,
        client_id: str | None = None,
    ) -> TokenGrant:
        """Record the token a client pushed for *resource*, replacing any prior one.

        Named for what the client is doing -- the spec's own verb -- rather than
        `authenticate`, because nothing is authenticated by storing it.
        """
        grant = TokenGrant(
            resource=resource,
            token=BearerToken(token),
            scopes=None if scopes is None else tuple(scopes),
            client_id=client_id,
        )
        self._grants[resource] = grant
        return grant

    def get(self, resource: str) -> TokenGrant | None:
        """The current grant for *resource*, or ``None`` if nobody pushed one."""
        return self._grants.get(resource)

    def revoke(self, resource: str) -> bool:
        """Forget the grant for *resource*. ``True`` if there was one.

        The pairing for an `auth/required` notification with `reason="expired"`:
        drop the dead token first, so nothing can hand it to a provider between
        noticing and telling the client.
        """
        return self._grants.pop(resource, None) is not None

    def clear(self) -> None:
        self._grants.clear()

    def resources(self) -> tuple[str, ...]:
        """Every resource currently holding a grant. Never the tokens."""
        return tuple(self._grants)

    def satisfies(self, resource: str, required_scopes: Iterable[str] = ()) -> bool:
        """Whether a stored grant for *resource* covers *required_scopes*."""
        grant = self._grants.get(resource)
        if grant is None:
            return False
        return scopes_satisfied(
            grant.scopes,
            required_scopes,
            unscoped_satisfies_any=self.unscoped_satisfies_any,
        )

    def unsatisfied(self, resources: Iterable[ProtectedResource]) -> list[ProtectedResource]:
        """Which of *resources* still need a token -- the input to :func:`auth_required`.

        A resource with ``required=False`` is never listed: the agent works
        without it, so failing a command over it would be inventing a
        requirement the host itself advertised as optional.
        """
        return [r for r in resources if r.required and r.resource not in self._grants]


def auth_required(resources: Iterable[ProtectedResource], message: str | None = None) -> AhpError:
    """The `-32007 AuthRequired` error, with its **mandatory** `data`.

    `AuthRequiredErrorData` is a MUST, not a SHOULD: it is the only thing that
    tells the client *which* resource to go get a token for and *which*
    authorization server to ask. A bare `-32007` leaves a client that wants to
    recover programmatically with nothing to act on. This helper exists so the
    field cannot be forgotten -- it is built from the resources, never optional.

    May be raised from **any** command, not just `authenticate`.
    """
    listed = list(resources)
    names = [r.resource if r.resource_name is None else r.resource_name for r in listed]
    detail = "Authentication required"
    if names:
        detail = f"{detail} for {', '.join(names)}"
    return AhpError(
        AHP_ERROR_CODES["AuthRequired"],
        detail if message is None else message,
        {"resources": [r.to_wire() for r in listed]},
    )


def auth_required_params(
    resource: str,
    *,
    channel: str = ROOT_CHANNEL,
    reason: AuthRequiredReason = "required",
) -> dict[str, Any]:
    """Params for the `auth/required` notification.

    `channel` defaults to the root URI because that is where `protectedResources`
    is advertised, but the notification MAY name any channel -- a per-session
    resource belongs to the session's.
    """
    return {"channel": channel, "resource": resource, "reason": reason}
