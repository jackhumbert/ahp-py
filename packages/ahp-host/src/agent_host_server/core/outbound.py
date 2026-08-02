"""Requests the host sends *to* a client, and the responses that come back.

Every `resource*` method "is symmetrical and MAY be sent in either direction"
(`commands.schema.json` in the vendored spec, on all nine of them), and the
host genuinely needs the reverse direction: a client publishes its plugin
content under its own `virtual://my-client/...` URIs, and nobody but that client
can read them. VS Code serves the whole family.

That turns `Host.serve`'s read loop from request-in/response-out into full
duplex, which is where this module's two hard problems come from.

## Invariant 10 -- one queue, one writer task

:meth:`OutboundRequests.call` never touches a transport. It takes a
**synchronous** ``send`` callable and hands it the framed message; the intended
argument is :meth:`Connection.enqueue
<agent_host_server.core.connection.Connection.enqueue>`, a non-blocking append
to that connection's single outbound queue. A signature that accepted a
coroutine would make it possible to await a write from inside a host method,
interleaved with the writer task's own writes -- the exact defect the queue
exists to prevent. The type makes it unavailable.

## Invariant 17 -- nothing escapes the handler of an inbound frame

A response is an inbound frame with an `id` and no `method`, and like a
notification it has no reply that could carry an error. :meth:`resolve
<OutboundRequests.resolve>` therefore raises nothing at all: it returns ``True``
when the frame was ours and ``False`` when it was not. Every malformed shape a
peer can construct -- no `id`, an `id` of ``{}`` (legal JSON, unhashable in
Python), an unknown `id`, both `result` and `error`, neither -- ends as a
``False`` or as a failed future, never as an exception in the read loop. One bad
frame from an untrusted peer must not cost a connection.

## Lifetime

The failure this module is really about is a request whose *connection dies*
while it is in flight. Nothing will ever answer it, so the waiter has to be woken
by the teardown path instead: `Host.serve`'s ``finally`` calls
:meth:`fail_connection <OutboundRequests.fail_connection>`, which ends every
request opened against that connection with an error rather than leaving it
parked forever. It is idempotent, so overlapping teardown paths are safe. Until
it runs the registry holds a strong reference to the connection key -- the other
reason it belongs in a ``finally`` and not in a callback.

The three other endings -- answered, timed out, caller cancelled -- all funnel
through :meth:`call <OutboundRequests.call>`'s ``finally``, so the registry is
empty on every path.

## Ids

Ids are strings: ``"ahs-1"``, ``"ahs-2"``, ... from one counter per registry.

Both peers put an `id` on the requests they send over the same socket, and the
published client mints its own with ``const id = this.nextRequestId++``
(`@microsoft/agent-host-protocol`, `client/client.ts`) -- always a JSON
*number*. A JSON string is never a JSON number, so no id it mints can equal one
of ours, under either language's equality (``1 === "ahs-1"`` and
``1 == "ahs-1"`` are both false). :meth:`resolve <OutboundRequests.resolve>`
gates on ``isinstance(raw_id, str)`` before it looks anything up, so a numeric id
is rejected as "not ours" without ever reaching the map. JSON-RPC 2.0 allows a
string id, and the protocol's own `id` type is `string | number`.

The counter is per *registry*, not per connection, so two connections' requests
cannot share an id either. That is what makes the per-connection index exact,
and it lets `resolve` refuse a response arriving on a connection other than the
one the request went out on -- sequential ids are guessable, and answering
another client's outstanding request should not be something a peer can try.

## Timeout

ADR 0005 has no timeout because a human is on the other end of all five of its
requests. Here a *machine* is: a `resourceRead` of a `virtual://` URI is a client
answering out of its own memory or file service. A peer that has not answered in
:data:`DEFAULT_TIMEOUT` seconds is not going to, and the cost of waiting forever
is a provider turn pinned open for the life of the connection.

Thirty seconds is not arbitrary: it is the reference client's own
``DEFAULT_REQUEST_TIMEOUT_MS`` (30_000, `client/client.ts`) for requests in the
other direction, so the two peers give up on each other at the same scale. It is
per-call overridable for the one case that is not machine-paced --
`resourceRequest` MAY prompt a human for permission -- where the caller should
pass a larger value, or ``None`` to wait as long as the connection lives.

There is no AHP error code for "the peer did not answer", and inventing one is
forbidden (invariant 14), so a timeout and a dropped connection both surface as
`InternalError` (-32603) with a message that says which happened.

## Wiring it into the read loop

The classification order is not free-form. `Host.serve` must ask, in order:

1. ``"method" in message`` -- **first**. A request carries both `method` and
   `id`; only `method` distinguishes it from a response, so testing `id` first
   routes every request into the response path.
2. within that, ``"id" in message`` picks request (dispatched as a task) over
   notification (handled inline, exceptions logged).
3. otherwise ``outbound.resolve(message, connection)`` -- inline, because it only
   completes a future; the host method that was waiting resumes on its own task,
   so nothing slow happens in the read loop.
4. ``False`` from `resolve` means the frame was none of those: log it and drop
   it. There is no response that could report it, and it must not end the loop.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from agent_host_protocol.errors import AhpError, internal_error
from agent_host_protocol.types import JSON_RPC_ERROR_CODES

__all__ = ["DEFAULT_TIMEOUT", "OutboundRequests"]

_log = logging.getLogger(__name__)

#: Seconds a host-initiated request waits for its answer. Matches the reference
#: client's default for the other direction; see "Timeout" above.
DEFAULT_TIMEOUT: Final = 30.0

#: Prefixes every id this host mints. See "Ids" above -- the string-ness is the
#: whole anti-collision argument, not decoration.
_ID_PREFIX: Final = "ahs-"

#: `resolve`'s default: do not check which connection the response arrived on.
#: A distinct sentinel rather than `None`, because `None` is a legal key.
_ANY_CONNECTION: Final = object()

#: What a peer's unusable error code falls back to. Invariant 14: no invented codes.
_INTERNAL: Final = JSON_RPC_ERROR_CODES["InternalError"]


@dataclass
class _Outbound:
    """One request that has gone out and is waiting for its response."""

    id: str
    connection_key: Any
    future: asyncio.Future[Any] = field(repr=False)


class OutboundRequests:
    """The registry of in-flight host-initiated requests. One per host.

    *connection_key* is whatever the caller uses to name a connection -- the
    :class:`~agent_host_server.core.connection.Connection` itself is the
    intended value, and it hashes and compares by identity. This module never
    imports it: a registry that knew what a connection was would be a registry
    that could send, and sending is the writer task's job alone.
    """

    def __init__(self) -> None:
        self._by_id: dict[str, _Outbound] = {}
        self._by_connection: dict[Any, set[str]] = {}
        self._counter = 0

    def __len__(self) -> int:
        return len(self._by_id)

    # ─── minting ─────────────────────────────────────────────────────────

    def next_id(self) -> str | int:
        """Mint the next request id. Consumes a number; never reuses one.

        Declared as `str | int` because that is the protocol's `id` type, but
        this host only ever mints strings -- see "Ids" above for why that is
        load-bearing rather than a style choice.
        """
        self._counter += 1
        return f"{_ID_PREFIX}{self._counter}"

    def open(self, connection_key: object) -> tuple[str | int, asyncio.Future[Any]]:
        """Register a request against *connection_key* and return `(id, future)`.

        The caller frames and sends the message itself. Prefer :meth:`call`,
        which cannot forget the teardown; this exists for a caller that needs
        the id before the message exists.
        """
        request = self._open(connection_key)
        return request.id, request.future

    def _open(self, connection_key: object) -> _Outbound:
        request_id = str(self.next_id())
        request = _Outbound(
            id=request_id,
            connection_key=connection_key,
            future=asyncio.get_running_loop().create_future(),
        )
        self._by_id[request_id] = request
        self._by_connection.setdefault(connection_key, set()).add(request_id)
        return request

    # ─── calling ─────────────────────────────────────────────────────────

    async def call(
        self,
        connection_key: object,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        send: Callable[[Mapping[str, Any]], None],
        timeout: float | None = DEFAULT_TIMEOUT,
    ) -> Any:
        """Send *method* to one client and await its result.

        *send* is called once, synchronously, with the framed JSON-RPC request;
        pass ``Connection.enqueue``. It must not block and must not write to the
        transport itself -- ordering is the connection's single writer task's
        property to keep (invariant 10).

        Raises :class:`~agent_host_protocol.errors.AhpError`: the peer's own
        code and message for an error response, `InternalError` for a timeout, a
        dropped connection, or a response too malformed to interpret. The caller
        is a host method whose own failure path already turns an `AhpError` into
        a JSON-RPC error response, so this propagates rather than being returned.

        *timeout* defaults to :data:`DEFAULT_TIMEOUT`. Pass ``None`` only when a
        human is expected in the loop -- `resourceRequest` may prompt for
        permission -- and accept that the request then lives until the
        connection does not.
        """
        request = self._open(connection_key)
        message: dict[str, Any] = {"jsonrpc": "2.0", "id": request.id, "method": method}
        if params is not None:
            message["params"] = dict(params)
        try:
            send(message)
            return await asyncio.wait_for(request.future, timeout)
        except TimeoutError as exc:
            raise internal_error(f"No response to {method} within {timeout}s") from exc
        finally:
            # Every ending lands here: answered, failed, timed out, cancelled,
            # or `send` itself raising. Cancelling a future that is still
            # pending keeps an abandoned entry from being resolvable later, and
            # keeps asyncio from reporting an exception nobody retrieved.
            stale = self._discard(request.id)
            if stale is not None and not stale.future.done():
                stale.future.cancel()

    # ─── inbound ─────────────────────────────────────────────────────────

    def resolve(self, message: Mapping[str, Any], connection_key: object = _ANY_CONNECTION) -> bool:
        """Route one inbound response. ``True`` if it answered a request of ours.

        Never raises -- the read loop calls this and has nothing to report an
        exception with (invariant 17). ``False`` means "not ours": the frame is
        the caller's to log and drop.

        ``True`` means the entry is gone and its waiter has been woken, *even
        when the frame was malformed*. A response carrying both `result` and
        `error`, or neither, is still an answer to a request we can identify,
        and leaving that waiter parked would be the worse failure.

        Pass *connection_key* to require that the response arrived on the
        connection the request went out on. Ids are sequential and therefore
        guessable; without this, a second peer can answer a request the host
        addressed to somebody else.
        """
        raw_id = message.get("id")
        # The type gate comes before the lookup, and both matter. Only a string
        # can be one of ours, and an `id` of `{}` or `[]` is legal JSON but
        # unhashable -- indexing the map with it raises TypeError, inside the
        # read loop, which is invariant 17's failure precisely.
        if not isinstance(raw_id, str):
            return False
        request = self._by_id.get(raw_id)
        if request is None:
            return False
        if connection_key is not _ANY_CONNECTION and request.connection_key != connection_key:
            _log.warning("outbound %s answered on a different connection; ignored", raw_id)
            return False

        self._discard(raw_id)
        has_result = "result" in message
        has_error = "error" in message
        # `in`, not truthiness: `"result": null` is a legitimate result for a
        # method that returns nothing, and `[]` is a legitimate one for a list
        # (invariants 5 and 18).
        if has_result and has_error:
            _fail(request.future, internal_error(f"Response {raw_id} has both result and error"))
        elif has_error:
            _fail(request.future, _peer_error(raw_id, message["error"]))
        elif has_result:
            if not request.future.done():
                request.future.set_result(message["result"])
        else:
            _fail(request.future, internal_error(f"Response {raw_id} has neither result nor error"))
        return True

    # ─── teardown ────────────────────────────────────────────────────────

    def fail_connection(self, connection_key: object, reason: str) -> int:
        """End every request in flight on *connection_key*. Returns how many.

        Call it from `Host.serve`'s ``finally``, unconditionally. A response can
        only ever arrive on the connection its request went out on, so once that
        connection is gone every waiter on it is waiting on nothing -- and a
        waiter is a provider holding a turn open.

        Idempotent: the entries are removed as they are woken, so a second call
        (or one for a connection that never had a request) is a no-op returning
        ``0``.
        """
        ended = 0
        for request_id in list(self._by_connection.get(connection_key, ())):
            request = self._discard(request_id)
            if request is None:  # pragma: no cover - the two indexes cannot diverge
                continue
            _fail(request.future, internal_error(f"Connection closed before a response: {reason}"))
            ended += 1
        if ended:
            _log.debug("failed %d outbound request(s): %s", ended, reason)
        return ended

    def _discard(self, request_id: str) -> _Outbound | None:
        request = self._by_id.pop(request_id, None)
        if request is None:
            return None
        open_ids = self._by_connection.get(request.connection_key)
        if open_ids is not None:
            open_ids.discard(request_id)
            if not open_ids:
                del self._by_connection[request.connection_key]
        return request


def _fail(future: asyncio.Future[Any], error: AhpError) -> None:
    """Wake *future* with *error*, and mark the exception as retrieved.

    A waiter can be cancelled in the same tick its connection is torn down --
    `Host.serve`'s ``finally`` cancels the in-flight request tasks *and* fails
    the registry, and `Task.cancel` only schedules the cancellation. The
    exception then reaches nobody and asyncio logs "Future exception was never
    retrieved" at collection time, once per dropped connection, which trains
    people to ignore the message. Reading it here changes nothing a live waiter
    sees: ``await`` on an already-failed future still raises.
    """
    if future.done():
        return
    future.set_exception(error)
    future.exception()


def _peer_error(request_id: str, raw: Any) -> AhpError:
    """The peer's error object, believed exactly as far as it is well-formed.

    The code is propagated verbatim when it is one, because callers act on it:
    `PermissionDenied` (-32009) from a reverse `resourceRequest` is a decision,
    not a fault. Anything else becomes `InternalError` rather than a code we
    made up.
    """
    if not isinstance(raw, Mapping):
        return internal_error(f"Response {request_id} has a malformed error object")
    code = raw.get("code")
    message = raw.get("message")
    return AhpError(
        # `isinstance(True, int)` is true in Python, and `"code": true` would
        # then be re-serialised as JSON `true` -- not a code at all.
        code if isinstance(code, int) and not isinstance(code, bool) else _INTERNAL,
        message if isinstance(message, str) else f"Peer error on {request_id}",
        raw.get("data"),
    )
