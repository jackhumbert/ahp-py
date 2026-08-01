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


class PendingRequests:
    """The registry. One per host."""

    def __init__(self) -> None:
        self._by_id: dict[str, PendingRequest] = {}
        self._by_scope: dict[str, set[str]] = {}
        self._counter = 0

    def __len__(self) -> int:
        return len(self._by_id)

    def open(self, scope: str, kind: str) -> PendingRequest:
        """Park a new request and return it, un-awaited.

        The id is minted **here**. A provider-chosen id lets two providers
        collide; a client-chosen one lets a peer resolve a request it was never
        offered. The caller publishes the id to clients; nobody supplies one.
        """
        self._counter += 1
        request_id = f"{kind}-{self._counter}"
        request = PendingRequest(
            id=request_id,
            scope=scope,
            kind=kind,
            future=asyncio.get_running_loop().create_future(),
        )
        self._by_id[request_id] = request
        self._by_scope.setdefault(scope, set()).add(request_id)
        return request

    def get(self, request_id: str) -> PendingRequest | None:
        return self._by_id.get(request_id)

    def is_open(self, request_id: Any) -> bool:
        """Whether this id names a live request. Takes any value: it is used to
        validate a client-supplied `requestId`, which may be any JSON."""
        return isinstance(request_id, str) and request_id in self._by_id

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

    def cancel_scope(self, scope: str, reason: str) -> int:
        """End every request opened under *scope*. Returns how many were live.

        The provider's ``await`` raises ``CancelledError``, which is already how
        turn cancellation reaches it -- so an adapter that handles cancellation
        at all handles this without knowing it exists.
        """
        cancelled = 0
        for request_id in list(self._by_scope.get(scope, ())):
            request = self._discard(request_id)
            if request is None:
                continue
            if not request.future.done():
                request.future.cancel()
                cancelled += 1
        if cancelled:
            _log.debug("cancelled %d pending request(s) in %s: %s", cancelled, scope, reason)
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
        return request
