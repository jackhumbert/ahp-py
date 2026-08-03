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
    ToolInfo,
    TurnCancelled,
    TurnCompleted,
    TurnEvent,
    TurnFailed,
    TurnInProgress,
    event_for,
    is_modelled,
)
from agent_host_client.client import actions
from agent_host_client.client.client import AhpClient
from agent_host_client.client.errors import AhpClientError
from agent_host_client.client.events import ActionEvent, ClientEvent, SessionRemoved
from agent_host_client.client.mirror import StateMirror
from agent_host_client.hosts.runtime import HostConfig, HostRuntime, TransportFactory
from agent_host_client.serve.inputs import ClientToolHost, InputResponder, pending_inputs
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
            try:
                await self._runtime.start()
            except BaseException:
                # `start` raises on a permanent refusal, and `__aexit__` does not
                # run when `__aenter__` raises -- so the only chance to close the
                # runtime's queues is here, while we still hold it.
                await self._runtime.shutdown()
                raise
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

    # ── what the handshake advertised ────────────────────────────────────────
    #
    # Affordances only a client can implement, from an `InitializeResult` no
    # `connect()` caller can otherwise reach: the runtime absorbed two of these
    # and dropped the third on the floor, and re-exported none. An advertisement
    # the front door cannot read is one the host made to nobody.

    @property
    def terminal_command_prefix(self) -> str | None:
        """The `!` shorthand, or ``None`` when the host supports none.

        A ``Message.text`` starting with this is executed as a terminal command
        rather than sent to the agent. ``"!"`` is the standardised convention,
        but the *host* decides -- so a client that hardcodes it offers the
        affordance to hosts that never claimed it, and withholds it from hosts
        that spell it differently.
        """
        return self._runtime.terminal_command_prefix

    @property
    def completion_trigger_characters(self) -> Sequence[str]:
        """Characters that SHOULD make an input issue a `completions` request."""
        return self._runtime.completion_trigger_characters

    @property
    def default_directory(self) -> str | None:
        """Where the host suggests a remote filesystem browser should open."""
        return self._runtime.default_directory

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
        tools: ClientToolHost | Sequence[Mapping[str, Any]] | None = None,
        progress: bool = False,
        ready_timeout: float = 30.0,
    ) -> Session:
        """Create a session and subscribe to it.

        The URI is minted as ``<provider>:/<uuid>``, matching VS Code -- **not**
        ``ahp-session:``. Nothing anywhere routes on the scheme, so the form is
        a convention rather than a contract, but matching the one real client is
        free.

        *tools* publishes this client's own tools on ``activeClient``, and a
        :class:`~agent_host_client.serve.ClientToolHost` also **runs** them: the
        session starts a pump that executes every call the host hands us and
        reports the result. Passing bare ``ToolDefinition`` mappings advertises
        tools with no executor behind them, so each call is *denied* as soon as
        it arrives -- which is the honest answer, and is the reason this argument
        may not simply be a list. Advertising without either is worse than not
        advertising at all: the agent asks for a tool, nothing ever answers, and
        the host parks the turn on a future that cannot be resolved.
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
        tool_host = self._tool_host(tools)
        active_client: JsonObject | None = None
        if tool_host is not None:
            active_client = {"clientId": self.client_id, "tools": tool_host.definitions()}

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
        # Started before readiness is awaited: a host that queues an
        # `initialMessage` can have a turn -- and a tool call -- in flight
        # already, and a pump attached afterwards would never see it.
        session._serve_tools(tool_host)
        await session._await_ready(ready_timeout)
        return session

    def _tool_host(
        self, tools: ClientToolHost | Sequence[Mapping[str, Any]] | None
    ) -> ClientToolHost | None:
        """Normalise *tools* to the thing that can actually answer a call.

        A bare definition list becomes a host with no executors registered,
        which denies every call by name. That is deliberate: the alternative --
        what this did before there was a pump at all -- is publishing tools
        nothing in the library can run, and `chat/toolCallStart` for a client
        contributor has no other answerer.
        """
        if tools is None or isinstance(tools, ClientToolHost):
            return tools
        host = ClientToolHost(self.protocol, client_id=self.client_id)
        host.advertise(tools)
        return host

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
        self._tools: ClientToolHost | None = None
        self._tool_pump: asyncio.Task[None] | None = None

    @property
    def state(self) -> JsonObject:
        state = self._client.mirror.state(self.uri)
        return state if isinstance(state, dict) else {}

    @property
    def capabilities(self) -> JsonObject:
        """``AgentCapabilities`` for the agent behind this session.

        Read from ``RootState.agents`` -- the only place they are published --
        matched on ``AgentInfo.provider``. **Every field is a presence flag and
        ``{}`` is falsy in Python**, so a caller testing one must write
        ``is not None``: ``multipleChats: {}`` advertises multi-chat, and
        ``if caps.get("multipleChats"):`` reads it as unsupported.
        """
        provider = self.provider or str(self.state.get("provider", ""))
        for agent in self._client.agents():
            if agent.get("provider") == provider:
                raw = agent.get("capabilities")
                return raw if isinstance(raw, dict) else {}
        return {}

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

    # ── more than one chat ───────────────────────────────────────────────────

    def chats(self) -> Sequence[JsonObject]:
        """``SessionState.chats`` -- one ``ChatSummary`` per chat.

        The catalogue, not the conversations: `resource`, `title`, `status`,
        `origin` and `interactivity` without the transcript. A tab strip is
        exactly this list, and rendering it does not require subscribing to
        every chat.
        """
        raw = self.state.get("chats")
        return [c for c in raw if isinstance(c, dict)] if isinstance(raw, list) else []

    async def create_chat(
        self,
        *,
        uri: str | None = None,
        initial_message: Mapping[str, Any] | str | None = None,
        source: Mapping[str, Any] | None = None,
        working_directories: Sequence[str] | None = None,
    ) -> Chat:
        """Open a second chat in this session, and subscribe to it.

        Gated on the agent's own advertisement, which the spec states as a MUST
        NOT rather than a SHOULD: without ``capabilities.multipleChats`` a
        client must not call `createChat` for anything beyond the chat the
        session starts with, and `fork` / `sideChat` are separate opt-ins on top
        of it. Refusing here costs a round trip; sending it anyway asks a host
        to enforce a rule we were told about in advance.

        The chat URI is **ours to mint** (`CreateChatParams.chat` is documented
        client-chosen and VS Code sends one), which is what makes the subscribe
        below possible without waiting for `session/chatAdded` to name it.
        """
        capabilities = self.capabilities
        # `is not None`, never truthiness: `multipleChats: {}` is the ordinary
        # advertisement and `{}` is falsy in Python, so a truthiness test reads
        # every plain multi-chat agent as not supporting multi-chat.
        multiple = capabilities.get("multipleChats")
        if multiple is None:
            raise AhpClientError(
                f"agent {self.provider or self.state.get('provider', '')!r} does not advertise "
                "capabilities.multipleChats; the spec says clients MUST NOT call createChat"
            )
        if source is not None:
            self._check_source(source, multiple if isinstance(multiple, Mapping) else {})
        chat_uri = uri or f"ahp-chat:/{uuid.uuid4()}"
        message = {"text": initial_message} if isinstance(initial_message, str) else initial_message
        await self._client.protocol.create_chat(
            self.uri,
            chat_uri,
            initialMessage=dict(message) if message is not None else None,
            source=dict(source) if source is not None else None,
            workingDirectories=list(working_directories)
            if working_directories is not None
            else None,
        )
        return await self.open_chat(chat_uri)

    @staticmethod
    def _check_source(source: Mapping[str, Any], multiple: Mapping[str, Any]) -> None:
        """`fork` and `sideChat` are each their own opt-in.

        Both are plain booleans here rather than presence objects -- the one
        place in `AgentCapabilities` where truthiness is the correct test, and
        the reason this is not folded into the check above.
        """
        kind = str(source.get("kind", ""))
        if kind in {"fork", "sideChat"} and not multiple.get(kind):
            raise AhpClientError(
                f"agent does not advertise capabilities.multipleChats.{kind}; "
                f"clients MUST NOT pass a ChatSource with kind={kind!r}"
            )

    async def open_chat(self, uri: str) -> Chat:
        """Subscribe to a chat of this session that already exists.

        The way to reach a chat somebody else created -- a fork, a side chat, or
        the second tab another client opened -- which `chats()` lists and
        :meth:`chat` cannot return because it only ever resolves `defaultChat`.
        """
        await self._client._runtime.subscribe(uri, "chat")
        return Chat(self._client, self, uri)

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

    async def inputs(self, *, poll: float = 5.0) -> AsyncIterator[list[JsonObject]]:
        """Yield the pending set whenever it changes.

        The set is read from ``SessionState.inputNeeded``, so the **reducer
        stays the source of truth**: ``inputNeeded`` moves for several reasons
        -- a request opening, another client answering one, a turn ending -- and
        rebuilding it from ``session/inputNeededSet`` and
        ``session/inputNeededRemoved`` means re-deriving what the session
        reducer already computed.

        That does not make an interval the mechanism, which is the false binary
        this used to sit on. **The envelope is the clock and the mirror is the
        source**: any event scoped to this session wakes a re-read, and nothing
        is reconstructed from actions. Every other edge in this library is
        edge-triggered, and an approval prompt is precisely the thing a human is
        waiting on.

        *poll* is therefore a **ceiling on staleness rather than the
        mechanism** -- a backstop for anything that can move ``inputNeeded``
        without an event scoped here, a resubscribe snapshot after a reconnect
        being the case that matters. Lowering it does not make a prompt arrive
        sooner; it only shortens the worst case when the clock is missed.

        The reader is attached **before** the first read, so a request opening
        while the caller is still setting up is buffered rather than missed.
        """
        reader = self._client._runtime.events()
        try:
            previous: list[JsonObject] | None = None
            while True:
                current = self.pending_inputs()
                if current != previous:
                    previous = current
                    yield current
                if not await self._wait_for_input_change(reader, poll):
                    return
        finally:
            await reader.aclose()

    async def _wait_for_input_change(self, reader: Any, poll: float) -> bool:
        """Block until something might have moved ``inputNeeded``.

        Returns on the first event scoped to this session, on the *poll*
        ceiling, or on end of stream -- ``False`` only for the last, which ends
        the iteration. It never reports *what* moved: that is the mirror's
        answer, and asking here would be the reconstruction this avoids.

        Events on other channels do not wake a re-read, but they do consume the
        remaining budget rather than restarting it, so a busy chat cannot
        postpone the ceiling indefinitely.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + poll
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return True
            try:
                tagged = await asyncio.wait_for(_next_or_none(reader), remaining)
            except TimeoutError:
                return True
            if tagged is None:
                return False
            if tagged.channel == self.uri:
                return True

    # ── running the tools we published ───────────────────────────────────────

    def _serve_tools(self, tools: ClientToolHost | None) -> None:
        """Start answering the client-contributed calls this session will get.

        Driven off ``client.events()`` rather than off a `TurnStream`, because
        the turn carrying the call need not be one we started -- and because
        `TurnStream._drain` cannot answer one anyway: a client-provided call
        arrives already `running`, so there is no approval to give, only a
        result to produce.
        """
        if tools is None:
            return
        self._tools = tools
        self._tool_pump = asyncio.get_running_loop().create_task(
            self._run_client_tools(tools), name=f"ahp-client-tools-{self.uri}"
        )

    async def _run_client_tools(self, tools: ClientToolHost) -> None:
        reader = self._client._runtime.events()
        #: `(channel, toolCallId)` already dispatched to an executor. A second
        #: `chat/toolCallReady` is the one action that reaches an already-running
        #: call -- hosts republish it to revise the invocation message -- and
        #: without this the tool runs again and the second result overwrites the
        #: first.
        started: set[tuple[str, str]] = set()
        try:
            async for tagged in reader:
                event = tagged.event
                if not isinstance(event, ActionEvent) or event.rejection_reason is not None:
                    continue
                action = event.action
                if action.get("type") != "chat/toolCallReady":
                    continue
                call = _tool_call_in(
                    self._client.mirror.state(tagged.channel), str(action.get("toolCallId", ""))
                )
                # Ownership is read from the call's *state*: `contributor` is
                # required on `chat/toolCallStart` and only repeated on the ready
                # by hosts that choose to, so deciding from the action alone
                # silently declines to run anything against a host that does not.
                if not tools.owns(call or action):
                    continue
                key = (tagged.channel, str(action.get("toolCallId", "")))
                if key in started:
                    continue
                started.add(key)
                await tools.execute(
                    tagged.channel, action, tool_name=str((call or {}).get("toolName", ""))
                )
        except Exception:
            # Ends the pump, quietly. Reporting the result means dispatching, so
            # anything reaching here is the connection going away underneath us
            # -- and an exception left on a task nobody awaits is reported by
            # asyncio at an unrelated moment with a stack that points nowhere
            # useful, which is exactly the shape this file avoids elsewhere.
            return
        finally:
            await reader.aclose()

    async def _stop_tools(self) -> None:
        if self._tool_pump is None:
            return
        pump, self._tool_pump = self._tool_pump, None
        pump.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await pump

    async def dispose(self) -> None:
        await self._stop_tools()
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
        # Stopped whether or not we own the session: the pump is ours, and
        # leaving it attached to a `Session` the caller has finished with keeps a
        # reader on the event queue and a task holding this object alive.
        await self._stop_tools()
        if self._owned:
            with contextlib.suppress(Exception):
                await self.dispose()


class Chat:
    def __init__(self, client: Client, session: Session, uri: str) -> None:
        self._client = client
        self._session = session
        self.uri = uri
        #: ``(turn id, loop time at dispatch)`` for a turn this client started.
        #: The only own-clock measurement available for
        #: ``chat/turnCancelled.duration``, which a client "MUST NOT derive by
        #: subtracting timestamps".
        self._own_turn: tuple[str, float] | None = None

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

    async def cancel(self, *, duration_ms: float | None = None) -> None:
        """Stop whatever turn is running on this chat.

        The turn id comes from the mirror because the action requires it and
        `_end_turn` matches on it: a cancel that omits it is a no-op on *every*
        peer while the host still tears the provider down, so the transcript
        keeps a live `activeTurn` nobody can finish and the chat refuses every
        later `chat/turnStarted` as "a turn is already active".

        With no active turn there is nothing to name, and a bare cancel is
        precisely the defect above -- so this sends nothing.
        """
        active = self.state.get("activeTurn")
        turn_id = str(active.get("id", "")) if isinstance(active, Mapping) else ""
        if not turn_id:
            return
        self._client.protocol.dispatch(
            self.uri,
            actions.turn_cancelled(turn_id, duration_ms=self._elapsed_ms(turn_id, duration_ms)),
        )

    async def dispose(self) -> None:
        """Close this chat.

        `chat-channel.md` claims the protocol exposes no such command; it is in
        `CommandMap` with a `DisposeChatParams` and the reference host implements
        it. The types win. Disposing the session's *default* chat is not
        something the protocol forbids and not something a host has to survive,
        so this is for the extra chats :meth:`Session.create_chat` opened.
        """
        await self._client.protocol.dispose_chat(self.uri)

    def _note_turn_started(self, turn_id: str, at: float) -> None:
        """Remember when *we* started a turn, on our own clock."""
        self._own_turn = (turn_id, at)

    def _elapsed_ms(self, turn_id: str, override: float | None) -> float:
        """How long the turn ran, by the only clock we are allowed to use.

        Zero for a turn somebody else started: we have no own-clock measurement
        of it, and `ActiveTurn.startedAt` is the peer timestamp the spec forbids
        subtracting. The consumer "MUST treat it as opaque, producer-supplied
        data", so an honest zero beats a cross-clock difference; a caller that
        did time the turn passes *duration_ms*.
        """
        if override is not None:
            return override
        if self._own_turn is not None and self._own_turn[0] == turn_id:
            return max(0.0, (asyncio.get_running_loop().time() - self._own_turn[1]) * 1000)
        return 0.0


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
            if not is_modelled(envelope):
                # Deliberately not surfaced -- mostly this caller's own writes
                # coming back. Skipped rather than delivered as `UnknownEvent`,
                # which is reserved for an action a newer host sent that this
                # build has never heard of.
                continue
            return event_for(envelope, self._dispatch, self._tools)

    def _dispatch(self, channel: str, action: Mapping[str, Any]) -> None:
        """Adapt `dispatch` -- which returns a handle -- to the fire-and-forget
        shape an event's `.approve()` needs."""
        self._client.protocol.dispatch(channel, action)

    def _tools(self, channel: str, tool_call_id: str) -> ToolInfo:
        return _tool_info(self._client.mirror, channel, self._chat._session.uri, tool_call_id)

    async def aclose(self) -> None:
        if self._reader is not None:
            await self._reader.aclose()


async def _next_or_none(reader: Any) -> Any:
    """Read one event, turning end-of-stream into a value.

    `StopAsyncIteration` raised inside `asyncio.wait_for` finishes a task nobody
    retrieves, which asyncio then reports at an unrelated moment with a stack
    that points nowhere useful. Every timed read here goes through this.

    Cancelling the wait is safe: a reader's cursor only advances once a value
    has been taken, so a read abandoned mid-wait loses nothing.
    """
    try:
        return await reader.__anext__()
    except StopAsyncIteration:
        return None


def _tool_info(mirror: StateMirror, chat_uri: str, session_uri: str, tool_call_id: str) -> ToolInfo:
    """What the tool call *is*, read from state because no action says.

    `toolName` is published once, on `chat/toolCallStart`, and is required on
    the resulting `ToolCallState`; `annotations` is a property of
    `ToolDefinition` alone -- `SessionState.serverTools` for the host's tools,
    `activeClients[].tools` for a client's. An `ApprovalPolicy` switching on
    either has nowhere else to look.

    Reading it at event time is safe because the client applies an envelope to
    the mirror *before* publishing it (`AhpClient._on_notification`), so the
    call is already in state when its `chat/toolCallReady` reaches the caller.

    From **confirmed** state, for the same reason `TurnStream.text()` is: our
    own `chat/turnStarted` sits in the pending queue until the host echoes it,
    and replaying it over confirmed puts a second, empty `activeTurn` in the
    optimistic view -- one with no response parts, and so no tool call to name.
    """
    name = _tool_name_in(mirror.confirmed(chat_uri), tool_call_id)
    if not name:
        return ToolInfo()
    return ToolInfo(name, _annotations_in(mirror.confirmed(session_uri), name))


def _tool_name_in(state: Any, tool_call_id: str) -> str:
    call = _tool_call_in(state, tool_call_id)
    return str(call.get("toolName", "")) if call is not None else ""


def _tool_call_in(state: Any, tool_call_id: str) -> Mapping[str, Any] | None:
    """The `ToolCallState`, from the live turn *or* an archived one.

    The mirror is applied in the read loop, ahead of whatever the caller is
    still working through -- a host that finishes a turn in one burst has
    already moved `activeTurn` into `turns` by the time the tool call's
    `chat/toolCallReady` is handed over. Looking only at `activeTurn` finds the
    call for a slow host and not for a fast one, which is the worst shape a bug
    can have.

    State rather than the action because the two fields a caller needs here --
    `toolName` and `contributor` -- are published once, on
    `chat/toolCallStart`, and the reducer is what carries them forward.
    """
    if not isinstance(state, Mapping):
        return None
    turns = state.get("turns")
    candidates: list[Any] = [state.get("activeTurn")]
    candidates.extend(reversed(turns) if isinstance(turns, list) else ())
    for turn in candidates:
        parts = turn.get("responseParts") if isinstance(turn, Mapping) else None
        for part in parts if isinstance(parts, list) else ():
            call = part.get("toolCall") if isinstance(part, Mapping) else None
            if isinstance(call, Mapping) and call.get("toolCallId") == tool_call_id:
                return call
    return None


def _annotations_in(state: Any, name: str) -> JsonObject | None:
    """`ToolDefinition.annotations` for *name*, from either publisher.

    Both catalogues are searched because a client-contributed tool is announced
    on `activeClients[].tools` and never appears in `serverTools`, and a policy
    should not behave differently depending on who contributed the tool.
    """
    if not isinstance(state, Mapping):
        return None
    catalogues: list[Any] = [state.get("serverTools")]
    clients = state.get("activeClients")
    for client in clients if isinstance(clients, list) else ():
        if isinstance(client, Mapping):
            catalogues.append(client.get("tools"))
    for catalogue in catalogues:
        for tool in catalogue if isinstance(catalogue, list) else ():
            if isinstance(tool, Mapping) and tool.get("name") == name:
                annotations = tool.get("annotations")
                return annotations if isinstance(annotations, dict) else None
    return None


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
        started = asyncio.get_running_loop().time()
        self._client.protocol.dispatch(
            self._chat.uri,
            actions.turn_started(self._turn_id, text=self._text, model=self._model),
        )
        # Recorded on our own clock so a later `Chat.cancel()` has a duration it
        # is allowed to report for this turn.
        self._chat._note_turn_started(self._turn_id, started)
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
            disposed = self._disposal(tagged)
            if disposed is not None:
                self._finished = True
                return disposed
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
            refused = self._refusal(tagged.event, action)
            if refused is not None:
                self._finished = True
                return refused

            if not is_modelled(envelope):
                # AFTER the refusal check: a rejected echo of our own write is
                # exactly what the caller needs to hear about, even though the
                # accepted one is noise.
                continue

            event = event_for(envelope, self._dispatch, self._tools)
            if isinstance(event, TurnCompleted):
                self._finished = True
                return self._finalise(event)
            if isinstance(event, TurnFailed | TurnCancelled):
                self._finished = True
                return event
            return event

    async def _next_tagged(self) -> Any:
        return await _next_or_none(self._reader)

    def _refusal(self, event: ActionEvent, action: Mapping[str, Any]) -> TurnFailed | None:
        """The host refused the `chat/turnStarted` that opened this stream.

        A rejected envelope describes an action that was NOT applied, so the
        turn will never run and nothing further will arrive for it. Delivered as
        an ordinary event it decodes to a plain `TurnStarted` -- the caller is
        told the turn began, and then waits out `idle_timeout` for it.

        Scoped to `chat/turnStarted` deliberately. A rejected
        `chat/toolCallConfirmed` is another client having answered the approval
        first, which `ToolCallReady` documents as "not an error": the mirror
        reverts our optimistic effect and the turn carries on.
        """
        reason = event.rejection_reason
        if reason is None or action.get("type") != "chat/turnStarted":
            return None
        return TurnFailed(dict(event.envelope), reason)

    def _disposal(self, tagged: ClientEvent) -> TurnFailed | None:
        """The turn's chat was torn down underneath it, if this says so.

        A disposed chat's channel is dropped, so the `chat/turnComplete` this
        stream is waiting for can never arrive on it -- not even from a host
        that publishes a closing action, because there is nowhere left to
        publish it. `root/sessionRemoved` and `session/chatRemoved` are the only
        notice the protocol gives, and both arrive somewhere this stream would
        otherwise filter out: one is a notification rather than an envelope, the
        other is an action on the *session* channel.

        Without this the caller blocks for the whole `idle_timeout`, or forever
        where it is disabled, for a turn that ended before it started.
        """
        event = tagged.event
        gone = ""
        if isinstance(event, SessionRemoved):
            if str(event.params.get("session", "")) == self._session.uri:
                gone = f"session {self._session.uri}"
        elif isinstance(event, ActionEvent):
            action = event.action
            if action.get("type") == "session/chatRemoved" and (
                str(action.get("chat", "")) == self._chat.uri
            ):
                gone = f"chat {self._chat.uri}"
        if not gone:
            return None
        # Failed rather than cancelled: the spec says an in-progress turn SHOULD
        # be considered failed after an unexpected termination, and the disposal
        # need not have been this caller's doing.
        return TurnFailed(
            {"channel": self._chat.uri, "action": {"turnId": self._turn_id}},
            f"{gone} was disposed while turn {self._turn_id} was running",
        )

    def _dispatch(self, channel: str, action: Mapping[str, Any]) -> None:
        """Adapt `AhpClient.dispatch` -- which returns a handle -- to the
        fire-and-forget shape an event's `.approve()` needs."""
        self._client.protocol.dispatch(channel, action)

    def _tools(self, channel: str, tool_call_id: str) -> ToolInfo:
        return _tool_info(self._client.mirror, channel, self._session.uri, tool_call_id)

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
