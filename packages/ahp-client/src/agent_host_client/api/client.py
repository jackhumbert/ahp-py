"""The front door: ``connect``, ``Client``, ``Session``, ``Chat``, ``TurnStream``.

Ten lines should get you a streamed answer:

```python
async with connect("ws://localhost:4321") as client:
    async with await client.create_session(provider="echo", cwd=".") as session:
        async for event in session.prompt("Summarise README.md"):
            match event:
                case Delta(text=t):           print(t, end="", flush=True)
                case ToolCallReady() as call: call.approve()
                case TurnCompleted():         print()
```

Everything below this module exists to make that work without hiding anything:
``client.protocol`` is the raw client, ``client.mirror`` is the state, and
``client.protocol.request(...)`` must always work or the library becomes a
ceiling.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from ssl import SSLContext
from types import TracebackType
from typing import Any, Self

from agent_host_protocol.channels import ROOT_URI
from agent_host_protocol.transport import Transport
from agent_host_protocol.types import JsonObject

from agent_host_client.api.approvals import ApprovalPolicy, resolve_policy
from agent_host_client.api.events import (
    ToolCallReady,
    ToolCallResultReview,
    TurnCancelled,
    TurnCompleted,
    TurnEvent,
    TurnFailed,
    TurnInProgress,
    event_for,
)
from agent_host_client.client.client import AhpClient
from agent_host_client.client.errors import AhpClientError
from agent_host_client.client.events import ActionEvent, ClientEvent
from agent_host_client.client.mirror import StateMirror
from agent_host_client.hosts.runtime import HostConfig, HostRuntime, TransportFactory
from agent_host_client.serve.inputs import InputResponder, pending_inputs
from agent_host_client.serve.router import ResourceServer

__all__ = ["Chat", "ChatWatch", "Client", "ClientContext", "Session", "TurnStream", "connect"]


class ClientContext:
    """Awaitable *and* an async context manager, so both spellings work."""

    def __init__(self, runtime: HostRuntime, dispose_on_exit: bool = True) -> None:
        self._runtime = runtime
        self._dispose = dispose_on_exit
        self._client: Client | None = None

    def __await__(self) -> Any:
        return self._open().__await__()

    async def _open(self) -> Client:
        if self._client is None:
            await self._runtime.start()
            self._client = Client(self._runtime)
        return self._client

    async def __aenter__(self) -> Client:
        return await self._open()

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: Any
    ) -> None:
        if self._client is not None:
            await self._client.aclose()


def connect(
    url: str | None = None,
    *,
    transport: Transport | None = None,
    transport_factory: TransportFactory | None = None,
    token: str | None = None,
    headers: Mapping[str, str] | None = None,
    ssl: SSLContext | None = None,
    label: str | None = None,
    client_id: str | None = None,
    reconnect: bool = True,
    resources: ResourceServer | None = None,
    **host_options: Any,
) -> ClientContext:
    """Open a supervised connection.

    **``reconnect=True`` is the default, and that is a deliberate divergence**
    from every reference SDK. They make the client single-shot and push
    supervision into a separate layer; that split is right and is kept -- this
    composes it -- but the default composition is the supervised one, because a
    five-line script should not have to learn a supervisor to survive a laptop
    sleep. See ADR 0003.

    *ssl* reaches the WebSocket handshake, which is what a private CA or a
    client certificate needs. Without it the only way out is a hand-written
    ``transport_factory``, which means reimplementing the token handling this
    closure does -- and that is the security-sensitive part.

    Exactly one of *url*, *transport* or *transport_factory* is required.
    Passing a bare *transport* implies ``reconnect=False``: a transport can only
    be connected once, so there is nothing to reconnect *to*.
    """
    from agent_host_client.hosts.policy import disabled_policy

    if sum(x is not None for x in (url, transport, transport_factory)) != 1:
        raise ValueError("pass exactly one of url=, transport= or transport_factory=")

    factory: TransportFactory
    if transport is not None:
        used = False

        async def _once() -> Transport:
            nonlocal used
            if used:
                raise AhpClientError(
                    "this Client was given a single transport, which cannot be reopened; "
                    "pass transport_factory= to allow reconnection"
                )
            used = True
            return transport

        factory = _once
        reconnect = False
    elif transport_factory is not None:
        factory = transport_factory
    else:

        async def _dial() -> Transport:
            from agent_host_client.ws.transport import WebSocketClientTransport

            return await WebSocketClientTransport.connect(
                str(url), token=token, headers=headers, ssl=ssl
            )

        factory = _dial

    if not reconnect:
        host_options.setdefault("reconnect_policy", disabled_policy())

    handler = None
    if resources is not None:
        served = resources

        async def handler(method: str, params: Mapping[str, Any]) -> Any:
            return await served.handle(method, params)

    config = HostConfig(
        factory,
        label=label or (url or "host"),
        client_id=client_id,
        server_request_handler=handler,
        **host_options,
    )
    return ClientContext(HostRuntime(config))


class Client:
    """A connected host."""

    def __init__(self, runtime: HostRuntime) -> None:
        self._runtime = runtime

    # ── escape hatches, documented as such ───────────────────────────────────

    @property
    def protocol(self) -> AhpClient:
        """The raw client. ``client.protocol.request("someFutureMethod", …)``
        must always work, or this library becomes a ceiling."""
        return self._runtime.client()

    @property
    def mirror(self) -> StateMirror:
        return self._runtime.mirror

    @property
    def client_id(self) -> str:
        return self._runtime.client_id

    @property
    def protocol_version(self) -> str | None:
        return self._runtime.protocol_version

    @property
    def root(self) -> JsonObject:
        state = self._runtime.mirror.state(ROOT_URI)
        return state if isinstance(state, dict) else {}

    def agents(self) -> Sequence[JsonObject]:
        raw = self.root.get("agents")
        return [a for a in raw if isinstance(a, dict)] if isinstance(raw, list) else []

    def events(self) -> AsyncIterator[ClientEvent]:
        return self._runtime.events()

    def diagnostics(self) -> AsyncIterator[Any]:
        return self._runtime.diagnostics()

    # ── sessions ─────────────────────────────────────────────────────────────

    async def sessions(self, *, limit: int | None = None, cursor: str | None = None) -> JsonObject:
        """One page. **Do not persist the cursor** -- the spec says clients MUST
        NOT parse, modify or persist them across connections."""
        return await self.protocol.list_sessions(limit=limit, cursor=cursor)

    async def create_session(
        self,
        *,
        provider: str,
        cwd: str | os.PathLike[str] | None = None,
        working_directories: Sequence[str] | None = None,
        config: Mapping[str, Any] | None = None,
        uri: str | None = None,
        tools: Sequence[Mapping[str, Any]] | None = None,
        progress: bool = False,
        ready_timeout: float = 30.0,
    ) -> Session:
        """Create a session and subscribe to it.

        The URI is minted as ``<provider>:/<uuid>``, matching VS Code -- **not**
        ``ahp-session:``. Nothing anywhere routes on the scheme, so the form is
        a convention rather than a contract, but matching the one real client is
        free.
        """
        from agent_host_client.serve.resources import file_uri

        session_uri = uri or f"{provider}:/{uuid.uuid4()}"
        directories = (
            list(working_directories)
            if working_directories is not None
            else ([file_uri(cwd)] if cwd is not None else None)
        )
        # `progressToken` is what makes root/progress fire at all. A client that
        # never sends one has a progress surface that can never receive anything.
        token = str(uuid.uuid4()) if progress else None
        active_client: JsonObject | None = None
        if tools is not None:
            active_client = {"clientId": self.client_id, "tools": list(tools)}

        await self.protocol.create_session(
            session_uri,
            provider=provider,
            working_directories=directories,
            config=config,
            active_client=active_client,
            progress_token=token,
        )
        await self._runtime.subscribe(session_uri, "session")
        session = Session(self, session_uri, provider, owned=True)
        await session._await_ready(ready_timeout)
        return session

    async def open_session(self, uri: str) -> Session:
        """Attach to a session someone else created.

        Never disposed on ``__aexit__``: disposing another client's session on a
        ``with`` exit is the kind of surprise that loses trust.
        """
        await self._runtime.subscribe(uri, "session")
        return Session(self, uri, "", owned=False)

    async def aclose(self) -> None:
        await self._runtime.shutdown()


class Session:
    def __init__(self, client: Client, uri: str, provider: str, *, owned: bool) -> None:
        self._client = client
        self.uri = uri
        self.provider = provider
        self._owned = owned
        self._chat: Chat | None = None
        self._responder: InputResponder | None = None

    @property
    def state(self) -> JsonObject:
        state = self._client.mirror.state(self.uri)
        return state if isinstance(state, dict) else {}

    @property
    def interactivity(self) -> str:
        """``full`` | ``read-only`` | ``hidden``.

        Undocumented in every guide and spec page, and it gates whether a client
        may send a message at all. A client that ignores it happily dispatches
        into a read-only chat.
        """
        return str(self.state.get("interactivity", "full"))

    async def chat(self) -> Chat:
        """The default chat, subscribed on first use."""
        if self._chat is None:
            uri = str(self.state.get("defaultChat") or "")
            if not uri:
                raise AhpClientError(f"session {self.uri} has published no defaultChat")
            await self._client._runtime.subscribe(uri, "chat")
            self._chat = Chat(self._client, self, uri)
        return self._chat

    def prompt(self, text: str, **kwargs: Any) -> TurnStream:
        return _LazyTurnStream(self, text, kwargs)  # type: ignore[return-value]

    async def watch(self, *, from_start: bool = True) -> ChatWatch:
        """:meth:`Chat.watch` on the default chat."""
        chat = await self.chat()
        return chat.watch(from_start=from_start)

    def pending_inputs(self) -> list[JsonObject]:
        """Everything on this session waiting for a human.

        Reads ``SessionState.inputNeeded``, the aggregate whose stated purpose
        is that a client can answer **without subscribing to the chat** -- so a
        session list can resolve a prompt without opening the conversation.
        """
        return pending_inputs(self._client.mirror, self.uri)

    @property
    def responder(self) -> InputResponder:
        """Answers a request on this session, whoever started the turn.

        A pending tool call is answerable by any subscriber; the host arbitrates
        and the first answer wins. The front door teaches approvals through
        `ToolCallReady.approve()` on a turn you started, which is why this is
        stated here: answering somebody else's is not a lower-level operation,
        it is the same one.
        """
        if self._responder is None:
            self._responder = InputResponder(self._client.protocol, self._client.mirror)
        return self._responder

    async def inputs(self, *, poll: float = 0.05) -> AsyncIterator[list[JsonObject]]:
        """Yield the pending set whenever it changes.

        Derived from mirror state rather than from envelopes: ``inputNeeded``
        moves for several reasons -- a request opening, another client answering
        one, a turn ending -- and reconstructing the set from actions means
        re-deriving what the session reducer already computed.
        """
        previous: list[JsonObject] | None = None
        while True:
            current = self.pending_inputs()
            if current != previous:
                previous = current
                yield current
            await asyncio.sleep(poll)

    async def dispose(self) -> None:
        await self._client.protocol.dispose_session(self.uri)

    async def _await_ready(self, timeout: float) -> None:
        """Wait for readiness -- but only if the host did not answer provisionally.

        A host that reports ``lifecycle == "creating"`` is telling us the session
        exists and is still warming up. Waiting unconditionally deadlocks for the
        full timeout against exactly that host.
        """
        if str(self.state.get("lifecycle", "")) in {"ready", "creating"}:
            return
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.01)
            lifecycle = str(self.state.get("lifecycle", ""))
            if lifecycle in {"ready", "creating"}:
                return
            if lifecycle == "failed":
                raise AhpClientError(f"session {self.uri} failed to start")

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._owned:
            with contextlib.suppress(Exception):
                await self.dispose()


class Chat:
    def __init__(self, client: Client, session: Session, uri: str) -> None:
        self._client = client
        self._session = session
        self.uri = uri

    @property
    def state(self) -> JsonObject:
        state = self._client.mirror.state(self.uri)
        return state if isinstance(state, dict) else {}

    def turns(self) -> Sequence[JsonObject]:
        raw = self.state.get("turns")
        return [t for t in raw if isinstance(t, dict)] if isinstance(raw, list) else []

    def prompt(self, text: str, **kwargs: Any) -> TurnStream:
        return TurnStream(self._client, self, text, **kwargs)

    def watch(self, *, from_start: bool = True) -> ChatWatch:
        """Render whatever this chat is doing, starting now.

        *from_start* emits a synthetic :class:`TurnInProgress` first when a turn
        is already running, so attaching mid-turn has an entry point rather than
        a silence until the next delta.
        """
        return ChatWatch(self._client, self, from_start=from_start)

    async def cancel(self) -> None:
        self._client.protocol.dispatch(self.uri, {"type": "chat/turnCancelled"})


class ChatWatch:
    """Every event on a chat, including turns this client did not start.

    A sibling to :class:`TurnStream`, not a rework of it, with exactly the two
    originator-only assumptions lifted: it dispatches nothing on entry, and it
    filters no turn id. `TurnStarted` for somebody else's turn is an ordinary
    event here rather than the one that never arrives.

    Two clients rendering one live turn is the thing the protocol exists for.
    Building it on `client.events()` means re-deriving the dispatch this already
    performs -- that a terminal event must be *yielded* rather than raised past,
    that authoritative text comes from confirmed state rather than accumulated
    deltas, that a dropped connection means the turn *failed* -- each of which is
    a comment in this repository explaining a mistake somebody already made.
    """

    def __init__(self, client: Client, chat: Chat, *, from_start: bool = True) -> None:
        self._client = client
        self._chat = chat
        self._reader: Any = None
        self._pending: list[TurnEvent] = []
        self._from_start = from_start

    def __aiter__(self) -> ChatWatch:
        return self

    def _open(self) -> None:
        if self._reader is not None:
            return
        # Attached before the first read, so nothing arriving while the caller
        # is still setting up is missed.
        self._reader = self._client._runtime.events()
        if not self._from_start:
            return
        active = self._active_turn()
        if active is not None:
            turn_id = str(active.get("id", ""))
            # Synthetic, and typed as such: a UI needs something to open a
            # bubble on, and a forged `TurnStarted` would be indistinguishable
            # from a turn that really did begin now.
            self._pending.append(
                TurnInProgress(
                    {"channel": self._chat.uri, "action": {"turnId": turn_id}},
                    _markdown_text(active),
                )
            )

    def _active_turn(self) -> Mapping[str, Any] | None:
        state = self._client.mirror.state(self._chat.uri)
        if not isinstance(state, Mapping):
            return None
        active = state.get("activeTurn")
        return active if isinstance(active, Mapping) else None

    async def __anext__(self) -> TurnEvent:
        self._open()
        if self._pending:
            return self._pending.pop(0)
        while True:
            try:
                tagged = await self._reader.__anext__()
            except StopAsyncIteration:
                raise StopAsyncIteration from None
            if not isinstance(tagged.event, ActionEvent):
                continue
            envelope = tagged.event.envelope
            if envelope.get("channel") != self._chat.uri:
                continue
            return event_for(envelope, self._dispatch)

    def _dispatch(self, channel: str, action: Mapping[str, Any]) -> None:
        """Adapt `dispatch` -- which returns a handle -- to the fire-and-forget
        shape an event's `.approve()` needs."""
        self._client.protocol.dispatch(channel, action)

    async def aclose(self) -> None:
        if self._reader is not None:
            await self._reader.aclose()


def _markdown_text(turn: Mapping[str, Any]) -> str:
    parts = turn.get("responseParts")
    if not isinstance(parts, list):
        return ""
    return "".join(
        str(part.get("content", ""))
        for part in parts
        if isinstance(part, Mapping) and part.get("kind") == "markdown"
    )


class TurnStream:
    """Both awaitable and async-iterable. One object, two idioms, one code path.

    ``await`` drains the stream applying the approval policy and returns the
    final text; ``async for`` hands over every event. ``__await__`` delegates to
    the same drain that iteration uses, so the two cannot diverge.
    """

    def __init__(
        self,
        client: Client,
        chat: Chat,
        text: str,
        *,
        approvals: ApprovalPolicy | str | None = None,
        turn_id: str | None = None,
        idle_timeout: float | None = 300.0,
        model: str | None = None,
    ) -> None:
        self._client = client
        self._chat = chat
        self._text = text
        self._policy = resolve_policy(approvals)
        self._turn_id = turn_id or str(uuid.uuid4())
        self._idle_timeout = idle_timeout
        self._model = model
        self._started = False
        self._reader: Any = None
        #: Held so the terminal event is *yielded* before iteration stops. A
        #: `StopAsyncIteration` carrying a payload is invisible to `async for`,
        #: so the caller would never see the turn end.
        self._terminal: TurnEvent | None = None
        self._finished = False

    def __await__(self) -> Any:
        return self._drain().__await__()

    async def _drain(self) -> TurnCompleted | TurnFailed | TurnCancelled:
        last: Any = None
        async for event in self:
            if isinstance(event, ToolCallReady):
                decision = await self._policy(event)
                if decision:
                    event.approve()
                else:
                    event.deny()
            elif isinstance(event, ToolCallResultReview):
                event.confirm()
            last = event
        if isinstance(last, TurnCompleted | TurnFailed | TurnCancelled):
            return last
        return TurnFailed({}, "stream ended without a terminal event")

    def __aiter__(self) -> TurnStream:
        return self

    async def _start(self) -> None:
        if self._started:
            return
        if self._session_is_read_only():
            raise AhpClientError(
                f"chat {self._chat.uri} is {self._session.interactivity}; "
                "the host will not accept a message"
            )
        self._reader = self._client._runtime.events()
        action: JsonObject = {
            "type": "chat/turnStarted",
            "turnId": self._turn_id,
            "message": {"text": self._text},
        }
        if self._model is not None:
            action["message"]["model"] = self._model
        self._client.protocol.dispatch(self._chat.uri, action)
        self._started = True

    @property
    def _session(self) -> Session:
        return self._chat._session

    def _session_is_read_only(self) -> bool:
        return self._session.interactivity in {"read-only", "hidden"}

    async def __anext__(self) -> TurnEvent:
        if self._finished:
            raise StopAsyncIteration
        if self._terminal is not None:
            terminal, self._terminal = self._terminal, None
            self._finished = True
            return terminal
        await self._start()
        loop = asyncio.get_running_loop()
        deadline = None if self._idle_timeout is None else loop.time() + self._idle_timeout
        while True:
            timeout = None if deadline is None else max(0.0, deadline - loop.time())
            try:
                tagged = await asyncio.wait_for(self._next_tagged(), timeout)
            except TimeoutError as exc:
                raise TimeoutError(
                    f"turn {self._turn_id} produced nothing for {self._idle_timeout}s"
                ) from exc
            if tagged is None:
                # The connection ended under us. The spec says an in-progress
                # turn SHOULD be considered failed after an unexpected
                # termination, so say so rather than ending silently.
                self._finished = True
                raise StopAsyncIteration
            if not isinstance(tagged.event, ActionEvent):
                continue
            envelope = tagged.event.envelope
            if envelope.get("channel") != self._chat.uri:
                continue
            action = envelope.get("action")
            if not isinstance(action, Mapping):
                continue
            turn_id = action.get("turnId")
            if turn_id is not None and turn_id != self._turn_id:
                continue

            event = event_for(envelope, self._dispatch)
            if isinstance(event, TurnCompleted):
                self._finished = True
                return self._finalise(event)
            if isinstance(event, TurnFailed | TurnCancelled):
                self._finished = True
                return event
            return event

    async def _next_tagged(self) -> Any:
        """Read one event, turning end-of-stream into a value.

        `StopAsyncIteration` raised inside `asyncio.wait_for` finishes a task
        nobody retrieves, which asyncio then reports at an unrelated moment with
        a stack that points nowhere useful.
        """
        try:
            return await self._reader.__anext__()
        except StopAsyncIteration:
            return None

    def _dispatch(self, channel: str, action: Mapping[str, Any]) -> None:
        """Adapt `AhpClient.dispatch` -- which returns a handle -- to the
        fire-and-forget shape an event's `.approve()` needs."""
        self._client.protocol.dispatch(channel, action)

    def _finalise(self, event: TurnCompleted) -> TurnCompleted:
        """Re-read the authoritative text from the mirror.

        The accumulated deltas are not authoritative: hosts fold a turn's
        opening characters into the subscribe snapshot instead of emitting them.
        """
        return TurnCompleted(event.envelope, self.text())

    def text(self) -> str:
        """The turn's text, concatenated from its markdown response parts.

        Read from **confirmed** state, and from ``turns`` before ``activeTurn``.
        Both halves of that matter. Our own optimistic ``chat/turnStarted`` sits
        in the pending queue until the host echoes it, so optimistic state
        carries a *second*, empty ``activeTurn`` with the same id -- preferring
        it would return "" for a turn that completed. And the chat reducer
        stamps ``modifiedAt`` from the clock, so replaying a pending action
        produces a different stamp than the host's echo will.
        """
        state = self._client.mirror.confirmed(self._chat.uri)
        if not isinstance(state, Mapping):
            return ""
        turns = state.get("turns")
        candidates: list[Any] = list(reversed(turns)) if isinstance(turns, list) else []
        active = state.get("activeTurn")
        if isinstance(active, Mapping):
            candidates.append(active)
        for turn in candidates:
            if not isinstance(turn, Mapping) or turn.get("id") != self._turn_id:
                continue
            parts = turn.get("responseParts")
            if not isinstance(parts, list):
                return ""
            return "".join(
                str(part.get("content", ""))
                for part in parts
                if isinstance(part, Mapping) and part.get("kind") == "markdown"
            )
        return ""


class _LazyTurnStream:
    """``Session.prompt`` needs the chat, which needs an await. This defers it."""

    def __init__(self, session: Session, text: str, kwargs: Mapping[str, Any]) -> None:
        self._session = session
        self._text = text
        self._kwargs = dict(kwargs)
        self._inner: TurnStream | None = None

    async def _resolve(self) -> TurnStream:
        if self._inner is None:
            chat = await self._session.chat()
            self._inner = chat.prompt(self._text, **self._kwargs)
        return self._inner

    def __await__(self) -> Any:
        async def run() -> Any:
            stream = await self._resolve()
            return await stream

        return run().__await__()

    def __aiter__(self) -> _LazyTurnStream:
        return self

    async def __anext__(self) -> TurnEvent:
        stream = await self._resolve()
        return await stream.__anext__()
