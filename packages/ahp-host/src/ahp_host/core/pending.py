"""Provider requests that are waiting on a client.

See [ADR 0005](../../../docs/decisions/0005-suspending-provider-requests.md).
Five features need the same thing -- elicitation, tool-call confirmation,
client-contributed tool execution, auth step-up, terminal claim hand-back -- and
this is the only place allowed to hold a future waiting on one.

The awkward part is not parking a future, it is the lifetime. A suspended
provider is holding a turn open. If a cancelled turn leaves its requests parked,
nothing will ever resolve them: the provider stays blocked, the chat's status
stays ``InputNeeded``, and the only cure is disposing the session. So the scope
is bound structurally rather than by convention -- every request is opened under
a scope, and ending the scope cancels everything in it.

There is no default timeout. A human is on the other end of all five of these,
and a host-imposed deadline would cancel a turn because someone went to lunch.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

__all__ = ["PendingRequest", "PendingRequests", "RequestOutcome"]

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RequestOutcome:
    """How a suspended request ended.

    Neutral, per ADR 0003: `"accept"` / `"decline"` / `"cancel"` are the
    protocol's own `ChatInputResponseKind` values, but `payload` is whatever the
    host's mapper decided the provider needs -- never a wire action.
    """

    response: str
    payload: Any = None


@dataclass
class PendingRequest:
    id: str
    scope: str
    kind: str
    future: asyncio.Future[RequestOutcome] = field(repr=False)
    #: A second name the resolving action uses. Tool-call actions carry a
    #: `toolCallId`, not a request id, so they need one.
    key: str | None = None
    #: The channel this request was asked on, and the ONLY one that may answer
    #: it. Stored rather than parsed back out of `scope`: a chat URI is
    #: client-chosen and opaque and may legally contain the separator.
    channel: str | None = None
    #: The client that must answer, when exactly one may. Set for a tool the
    #: host asked a specific client to RUN -- "the server SHOULD reject this
    #: action if the dispatching client does not match the contributor's
    #: `clientId`" -- and left None for a park anyone admitted may answer, such
    #: as the confirmation of a server-side tool.
    owner: str | None = None
    #: The protected-resource identifier an `"auth"` park is waiting on, and the
    #: scopes the challenge demanded. Step-up is "resolved by the client
    #: obtaining a token for `auth.resource`" -- a token for a DIFFERENT
    #: resource must not wake this park, and the reference session checks both
    #: fields before resolving (`copilotAgentSession.ts:1756`). ``None`` means
    #: the park named no resource, and any push may answer it -- the same
    #: anywhere-answerable default a channel-less park gets.
    resource: str | None = None
    required_scopes: tuple[str, ...] = ()


class PendingRequests:
    """The registry. One per host."""

    def __init__(self) -> None:
        self._by_id: dict[str, PendingRequest] = {}
        self._by_scope: dict[str, set[str]] = {}
        #: Keyed by (channel, key), NOT by the bare key. A `toolCallId` is
        #: chosen by the provider and is only unique within its own chat --
        #: `EchoProvider` hardcodes "echo-tool-1", and any adapter numbering
        #: calls per turn does the same. Host-globally, two ordinary sessions
        #: doing the same thing collided: the second park overwrote the first's
        #: entry, the first session's approval was then refused forever with
        #: "no tool call awaiting that id", and its chat stayed pinned at
        #: InputNeeded with a live `session/inputNeeded` until disposal.
        #:
        #: That is verbatim the failure `open()`'s docstring says the channel
        #: scoping prevents -- the scoping was applied to the LOOKUP and not to
        #: the storage, so it filtered a collision that had already happened.
        self._by_key: dict[tuple[str | None, str], str] = {}
        self._counter = 0

    def __len__(self) -> int:
        return len(self._by_id)

    def open(
        self,
        scope: str,
        kind: str,
        key: str | None = None,
        *,
        channel: str | None = None,
        owner: str | None = None,
        resource: str | None = None,
        required_scopes: Sequence[str] = (),
    ) -> PendingRequest:
        """Park a new request and return it, un-awaited.

        The id is minted **here**. A provider-chosen id lets two providers
        collide; a client-chosen one lets a peer resolve a request it was never
        offered. The caller publishes the id to clients; nobody supplies one.

        *key* is an optional second name -- a `toolCallId` -- because the
        actions that resolve a tool call name the call, not the request.

        *resource* and *required_scopes* are what an `"auth"` park is waiting
        for, so `authenticate` can wake only the calls its token can actually
        unblock.

        *channel* is where the request was asked, and it is what makes
        :meth:`is_open` and :meth:`id_for_key` answerable. Ids are minted
        globally, so without it a peer could answer chat A's question by
        dispatching to chat B -- which resolved the victim's future while
        reading the answers out of the wrong channel's state, dropping them and
        pinning the victim in `InputNeeded` until the session was disposed.
        """
        self._counter += 1
        request_id = f"{kind}-{self._counter}"
        request = PendingRequest(
            id=request_id,
            scope=scope,
            kind=kind,
            future=asyncio.get_running_loop().create_future(),
            key=key,
            channel=channel,
            owner=owner,
            resource=resource,
            required_scopes=tuple(required_scopes),
        )
        self._by_id[request_id] = request
        self._by_scope.setdefault(scope, set()).add(request_id)
        if key is not None:
            self._by_key[(channel, key)] = request_id
        return request

    def id_for_key(self, key: Any, *, channel: str | None = None) -> str | None:
        """The live request registered under *key*, if any.

        Takes any value: the key comes off a client-dispatched action, so it may
        be a dict, a number, or missing entirely.

        *channel*, when given, additionally requires the request to have been
        asked there -- so a tool call in one chat cannot be resolved from
        another. A request opened without a channel is answerable from anywhere,
        which is what a bare sink in a test wants.
        """
        if not isinstance(key, str):
            return None
        request_id = self._by_key.get((channel, key))
        if request_id is None and channel is not None:
            # A park opened without a channel is answerable from anywhere,
            # which is what a bare sink in a test wants.
            request_id = self._by_key.get((None, key))
        return request_id

    def ids_of_kind(self, kind: str) -> list[str]:
        """Every live request of one kind.

        Step-up auth needs this: its resolution arrives as the `authenticate`
        COMMAND rather than through `dispatchAction`, so there is no action
        carrying a request id to look up -- the host knows only the resource,
        and has to find whoever was waiting on it.
        """
        return [rid for rid, request in self._by_id.items() if request.kind == kind]

    def get(self, request_id: str) -> PendingRequest | None:
        return self._by_id.get(request_id)

    def is_open(self, request_id: Any, *, channel: str | None = None) -> bool:
        """Whether this id names a live request. Takes any value: it is used to
        validate a client-supplied `requestId`, which may be any JSON.

        *channel*, when given, additionally requires the request to have been
        asked there. "No unresolved input-request part with the matching
        requestId" is a PER-CHANNEL rule -- the reducer reads it that way, and
        the host must too.
        """
        if not isinstance(request_id, str):
            return False
        request = self._by_id.get(request_id)
        if request is None:
            return False
        return channel is None or request.channel in (None, channel)

    def resolve(self, request_id: str, outcome: RequestOutcome) -> bool:
        """Hand the outcome to the waiting provider. ``False`` if nothing waits.

        Called *after* the reducer has applied the resolving action, so the
        provider and the published state can never disagree about whether the
        request was answered.
        """
        request = self._discard(request_id)
        if request is None:
            return False
        if not request.future.done():
            request.future.set_result(outcome)
        return True

    def owned_by(self, owner: str) -> list[PendingRequest]:
        """Every live request this client alone can answer.

        The host fails these when the client leaves: a park waiting on a peer
        that is gone advertises `session/inputNeeded` forever, pins the session
        at `InputNeeded`, and outlives `disposeSession`.
        """
        return [r for r in self._by_id.values() if r.owner == owner]

    def cancel_scope(self, scope: str, reason: str) -> list[PendingRequest]:
        """End every request opened under *scope*. Returns the ones that were live.

        The provider's ``await`` raises ``CancelledError``, which is already how
        turn cancellation reaches it -- so an adapter that handles cancellation
        at all handles this without knowing it exists.

        The returned list is what the caller needs to retract the corresponding
        `session/inputNeeded` entries: a cancelled turn that leaves them behind
        pins the session in `InputNeeded` with nothing to answer.
        """
        cancelled: list[PendingRequest] = []
        for request_id in list(self._by_scope.get(scope, ())):
            request = self._discard(request_id)
            if request is None:
                continue
            # Reported whether or not the future still needed cancelling. When
            # the TASK is cancelled, the future it was awaiting is already
            # cancelled by the time this runs -- but its `session/inputNeeded`
            # entry is still published, and that is what the caller retracts.
            # Reporting only the ones cancelled here left the session pinned in
            # `InputNeeded` after every cancelled turn.
            if not request.future.done():
                request.future.cancel()
            cancelled.append(request)
        if cancelled:
            _log.debug("cancelled %d pending request(s) in %s: %s", len(cancelled), scope, reason)
        return cancelled

    def _discard(self, request_id: str) -> PendingRequest | None:
        request = self._by_id.pop(request_id, None)
        if request is None:
            return None
        scoped = self._by_scope.get(request.scope)
        if scoped is not None:
            scoped.discard(request_id)
            if not scoped:
                del self._by_scope[request.scope]
        if request.key is not None:
            entry = (request.channel, request.key)
            if self._by_key.get(entry) == request_id:
                del self._by_key[entry]
        return request
