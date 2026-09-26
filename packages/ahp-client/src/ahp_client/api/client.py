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
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from ssl import SSLContext
from types import TracebackType
from typing import Any, Self

from ahp_protocol.channels import ROOT_URI
from ahp_protocol.transport import Transport
from ahp_protocol.types import JsonObject

from ahp_client.api.approvals import (
    ApprovalPolicy,
    ManualPolicy,
    UnansweredToolCallError,
    resolve_policy,
)
from ahp_client.api.changesets import (
    Changeset,
    ChangesetInfo,
    changeset_catalogue,
)
from ahp_client.api.events import (
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
from ahp_client.api.terminals import (
    ClientClaim,
    Terminal,
    TerminalClaim,
    TerminalInfo,
    new_terminal_uri,
    split_terminal_command,
    terminal_dimension,
)
from ahp_client.client import actions
from ahp_client.client.client import AhpClient
from ahp_client.client.errors import AhpClientError, RpcError
from ahp_client.client.events import ActionEvent, ClientEvent, SessionRemoved
from ahp_client.client.mirror import StateMirror
from ahp_client.hosts.runtime import HostConfig, HostRuntime, TransportFactory
from ahp_client.serve.inputs import ClientToolHost, InputResponder, pending_inputs
from ahp_client.serve.router import ResourceServer

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
        # `dispose_on_exit=False` really is honoured: a caller opting out is
        # keeping the runtime alive past the `with`, and closing it anyway
        # shuts the connection down under whatever they kept it for.
        if self._dispose and self._client is not None:
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
    from ahp_client.hosts.policy import disabled_policy

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
            from ahp_client.ws.transport import WebSocketClientTransport

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

    def terminal_command(self, text: str) -> str | None:
        """The command the host will run instead of the agent, or ``None``.

        The ``!`` shorthand, resolved against the *negotiated* prefix. This is a
        **synchronous query on the connection** rather than something a
        :class:`TurnStream` reports, because the decision it informs is made
        while the user is still typing -- an input box deciding whether to show a
        "runs as a command" badge, a script deciding whether it is about to talk
        to a model or to a shell. By the time a turn exists the answer has
        already been acted on, and a host that supports no prefix must produce
        no badge at all rather than one that lies.

        The turn itself is an ordinary one: send it with
        :meth:`Session.prompt` as usual. The sibling host runs the command and
        reports it back as a tool call named ``terminal``, so the events arrive
        through the chat surface that already exists -- there is nothing extra to
        subscribe to, which is why this is a query and not a second code path.
        """
        return split_terminal_command(text, self.terminal_command_prefix)

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
        :class:`~ahp_client.serve.ClientToolHost` also **runs** them: the
        session starts a pump that executes every call the host hands us and
        reports the result. Passing bare ``ToolDefinition`` mappings advertises
        tools with no executor behind them, so each call is *denied* as soon as
        it arrives -- which is the honest answer, and is the reason this argument
        may not simply be a list. Advertising without either is worse than not
        advertising at all: the agent asks for a tool, nothing ever answers, and
        the host parks the turn on a future that cannot be resolved.
        """
        from ahp_client.serve.resources import file_uri

        session_uri = uri or f"{provider}:/{uuid.uuid4()}"
        directories = (
            list(working_directories)
            if working_directories is not None
            else ([file_uri(cwd)] if cwd is not None else None)
        )
        # The same in-advance MUST NOT as `multipleChats`: "When absent, clients
        # ... MUST NOT set more than one entry in
        # `CreateSessionParams.workingDirectories`." Presence-flag, so
        # `is not None` -- `{}` advertises support.
        if (
            directories is not None
            and len(directories) > 1
            and self._capabilities_for(provider).get("multipleWorkingDirectories") is None
        ):
            raise AhpClientError(
                f"agent {provider!r} does not advertise "
                "capabilities.multipleWorkingDirectories; clients MUST NOT set more "
                "than one workingDirectories entry"
            )
        # `progressToken` is what makes root/progress fire at all. A client that
        # never sends one has a progress surface that can never receive anything.
        token = str(uuid.uuid4()) if progress else None
        config = await self._resolved_config(provider, directories, config)
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

    async def _resolved_config(
        self,
        provider: str,
        directories: Sequence[str] | None,
        config: Mapping[str, Any] | None,
    ) -> Mapping[str, Any] | None:
        """Plan §7.1 step 1: `resolveSessionConfig`, best-effort.

        The result's ``values`` are "server-resolved defaults to pass to
        `createSession`", so they are folded **under** the caller's own
        *config* -- the caller wins on any key both name. ``-32601``/``-32603``
        means unimplemented: continue with what the caller supplied, which is
        why every fake-host test passes without registering the method.
        """
        try:
            resolved = await self.protocol.resolve_session_config(
                provider=provider,
                workingDirectory=directories[0] if directories else None,
                config=dict(config) if config is not None else None,
            )
        except RpcError as error:
            if error.code in {-32601, -32603}:
                return config
            raise
        values = resolved.get("values")
        if not isinstance(values, dict) or not values:
            return config
        return {**values, **(config or {})}

    def _capabilities_for(self, provider: str) -> JsonObject:
        """``AgentCapabilities`` for *provider*, from ``RootState.agents``.

        The same lookup :attr:`Session.capabilities` performs, needed before
        the session exists -- `createSession` itself is gated on one of them.
        """
        for agent in self.agents():
            if agent.get("provider") == provider:
                raw = agent.get("capabilities")
                return raw if isinstance(raw, dict) else {}
        return {}

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

    # ── terminals ────────────────────────────────────────────────────────────

    def terminals(self) -> Sequence[TerminalInfo]:
        """``RootState.terminals`` -- one :class:`TerminalInfo` per live terminal.

        The catalogue, not the terminals: ``resource``, ``title``, ``claim`` and
        ``exitCode`` without the scrollback, so a tab strip renders without
        subscribing to every one of them. ``claim`` is here because it is what a
        client reads to decide whether to offer an input box at all -- which is
        why these are typed views and not raw dicts. Making that decision off a
        dict means hand-writing ``info["claim"]["clientId"] == client_id`` over
        peer-authored JSON, which is the comparison
        :func:`~ahp_client.api.terminals.claim_from_wire` exists to guard.
        """
        raw = self.root.get("terminals")
        if not isinstance(raw, list):
            return []
        return [TerminalInfo(t, self.client_id) for t in raw if isinstance(t, dict)]

    async def create_terminal(
        self,
        *,
        name: str | None = None,
        cwd: str | None = None,
        cols: int | float | None = None,
        rows: int | float | None = None,
        claim: TerminalClaim | None = None,
        uri: str | None = None,
    ) -> Terminal:
        """Open a terminal and subscribe to it.

        The claim defaults to **this client**, which is the only claim that lets
        the caller type: `CreateTerminalParams.claim` is required, and a session
        claim is held by no client at all. Pass a
        :class:`~ahp_client.api.terminals.SessionClaim` to create a
        terminal that belongs to an agent's turn rather than to a person.

        *cwd* is a **URI**, as `CreateTerminalParams.cwd` and `TerminalState.cwd`
        both are -- the terminal channel is URIs throughout, and a bare path is
        the one refusal a user sees rendered verbatim.

        A host with no terminal backend refuses with ``PermissionDenied``
        (-32009) carrying a human-readable reason, **not** ``MethodNotFound``:
        the method exists, and this request was declined. Let the
        :class:`~ahp_client.client.errors.RpcError` out -- its message is
        written to be shown.

        Subscribed after creation rather than before, because there is nothing
        to subscribe to first. Nothing is lost by the ordering: a shell that has
        already drawn its prompt has that output in the subscribe snapshot's
        ``content``, the same way a host folds a turn's opening characters into
        a chat snapshot.

        A failed subscribe **disposes what was just created**. The URI is minted
        in here, so a caller who did not pass one cannot even name the shell it
        started; leaving it behind would run it until the host stopped, which is
        the immortal-terminal failure `dispose` is deliberately ungated to avoid.
        """
        terminal_uri = uri or new_terminal_uri()
        held = claim or ClientClaim(self.client_id)
        await self.protocol.create_terminal(
            terminal_uri,
            claim=held.to_wire(),
            name=name,
            cwd=cwd,
            cols=None if cols is None else terminal_dimension("cols", cols),
            rows=None if rows is None else terminal_dimension("rows", rows),
        )
        try:
            # Bound by name, from the kind we already know. Never inferred from
            # the scheme: VS Code mints three `agenthost-terminal:` forms and the
            # spec's examples a fourth, and a scheme lookup binds no reducer at
            # all -- after which state freezes silently while output arrives.
            await self._runtime.subscribe(terminal_uri, "terminal")
        except BaseException:
            with contextlib.suppress(Exception):
                await self.protocol.dispose_terminal(terminal_uri)
            raise
        return Terminal(self, terminal_uri, owned=True)

    async def open_terminal(self, uri: str) -> Terminal:
        """Attach to a terminal someone else created.

        Never disposed on ``__aexit__``, and never claimed on entry: attaching to
        a terminal is not the same as taking it, and silently taking one would
        cut off whoever was typing. Call
        :meth:`~ahp_client.api.terminals.Terminal.take` to ask for it,
        which the host may refuse.
        """
        await self._runtime.subscribe(uri, "terminal")
        return Terminal(self, uri, owned=False)

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
        #: Executor tasks in flight -- each call runs off the pump's loop so a
        #: slow tool never blocks the reader. See :meth:`_spawn_executor`.
        self._tool_tasks: set[asyncio.Task[None]] = set()
        #: Bounds concurrent executors. 32 is arbitrary but finite: the point
        #: is that a flood of calls cannot spawn tasks without limit, not that
        #: the number is tuned.
        self._tool_gate = asyncio.Semaphore(32)

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
        if working_directories is not None and (
            capabilities.get("multipleWorkingDirectories") is None
        ):
            # "A client MUST NOT supply this field unless the agent advertises
            # `AgentCapabilities.multipleWorkingDirectories`" -- the same
            # told-in-advance rule as `multipleChats`, gated the same way.
            raise AhpClientError(
                f"agent {self.provider or self.state.get('provider', '')!r} does not advertise "
                "capabilities.multipleWorkingDirectories; clients MUST NOT supply "
                "workingDirectories on createChat"
            )
        chat_uri = uri or f"ahp-chat:/{uuid.uuid4()}"
        # The convenience form carries the required `origin` too: `Message`
        # requires `[text, origin]`, and "a client is only allowed to send
        # `MessageKind.User` messages" -- an origin-less Message is republished
        # verbatim inside the host's `chat/turnStarted` and frozen into every
        # peer's transcript, where `message.origin.kind` readers break on it.
        message = (
            {"text": initial_message, "origin": {"kind": "user"}}
            if isinstance(initial_message, str)
            else initial_message
        )
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

    # ── what the agent changed ───────────────────────────────────────────────

    def changesets(self) -> Sequence[ChangesetInfo]:
        """``SessionState.changesets`` -- the catalogue, not the diffs.

        "Just enough to render a chip or list row without subscribing", which
        is the point: a session list shows what changesets exist and what they
        are called without opening one. Empty until the agent publishes
        something, and full-replacement thereafter --
        ``session/changesetsChanged`` replaces the list entirely.
        """
        return list(changeset_catalogue(self.state))

    async def open_changeset(
        self,
        changeset: ChangesetInfo | str,
        *,
        turn_id: str | None = None,
        original_turn_id: str | None = None,
        modified_turn_id: str | None = None,
    ) -> Changeset:
        """Subscribe to one changeset and read its files, statuses and operations.

        *changeset* is an entry from :meth:`changesets` or a URI. An entry's
        ``uriTemplate`` is **expanded here** -- variable-free for the
        session-wide case, ``{turnId}`` for a per-turn slice, or the
        ``{originalTurnId}``/``{modifiedTurnId}`` pair for a comparison -- and
        that expanded URI is what the host registered, so it is what is
        subscribed. Passing a URI directly skips the expansion and looks the
        entry up by exact match, which is what a variable-free template gives.

        The reducer is bound here, from the kind we already know. Nothing
        anywhere infers it from the URI: hosts mint changeset channels under
        schemes of their own choosing and a scheme test would bind no reducer at
        all, freezing the state silently while actions kept arriving.
        """
        if isinstance(changeset, str):
            template, uri = changeset, changeset
        else:
            template = changeset.uri_template
            uri = changeset.expand(
                turn_id=turn_id,
                original_turn_id=original_turn_id,
                modified_turn_id=modified_turn_id,
            )
        await self._client._runtime.subscribe(uri, "changeset")
        return Changeset(self._client, self, uri, template=template)

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
        `TurnStream._drain` cannot answer one anyway: a client-provided call is
        *typically* auto-confirmed (`confirmed: "not-needed"`), so there is no
        approval to give, only a result to produce. Typically, not always: a
        host may gate a client tool, and that call goes through the standard
        confirmation flow first -- the pump only runs calls the mirror shows
        handed over (`status == "running"`).
        """
        if tools is None:
            return
        self._tools = tools
        self._tool_pump = asyncio.get_running_loop().create_task(
            self._run_client_tools(tools), name=f"ahp-client-tools-{self.uri}"
        )

    def _chat_uris(self) -> set[str]:
        """The chat channels that belong to **this** session, live from the mirror.

        `defaultChat` plus every `SessionState.chats[].resource`. Read per use
        rather than cached, so a chat created mid-session is covered. This is
        what keeps two sessions' pumps on one connection from double-running
        one call: `owns()` matches on `clientId`, which is identical for every
        session this `Client` created.
        """
        uris = {str(self.state.get("defaultChat") or "")}
        for chat in self.chats():
            uris.add(str(chat.get("resource") or ""))
        uris.discard("")
        return uris

    async def _run_client_tools(self, tools: ClientToolHost) -> None:
        """The pump. **The envelope is the clock and the mirror is the source.**

        Every wake -- an event on this session's channels, a reconnect, the
        initial attach -- level-scans confirmed state for owned, handed-over,
        unexecuted calls rather than acting on the woken event itself. That one
        shape covers four failure modes an edge-triggered pump has:

        * a **gated** call: a `chat/toolCallReady` without `confirmed` leaves
          the call `pending-confirmation` -- not handed over, and running it
          would execute a tool nobody approved. The scan only sees `running`.
        * the **approval** that later hands it over arrives as
          `chat/toolCallConfirmed` (there is no second ready), which carries
          neither `toolName` nor `toolInput` -- the mirror has both.
        * a call whose ready was **evicted** from the bounded fan-in tap, or
          dispatched while we were disconnected: the mirror (or the reconnect
          snapshot's `SessionState.inputNeeded`) still shows it running, and
          the next wake finds it. `state_changes()` supplies the wake a
          snapshot-arm resume otherwise would not.
        """
        events = self._client._runtime.events()
        states = self._client._runtime.state_changes()
        #: `(channel, toolCallId)` already dispatched to an executor. A second
        #: `chat/toolCallReady` is the one action that reaches an already-running
        #: call -- hosts republish it to revise the invocation message -- and
        #: without this the tool runs again and the second result overwrites the
        #: first. It also keeps the level scan from re-running a call our own
        #: (still optimistic) `chat/toolCallComplete` has not yet retired from
        #: confirmed state.
        started: set[tuple[str, str]] = set()
        watcher = asyncio.get_running_loop().create_task(
            self._rescan_on_reconnect(states, tools, started),
            name=f"ahp-client-tools-rescan-{self.uri}",
        )
        try:
            # The attach-time scan: a call handed over before the pump existed
            # -- a queued `initialMessage`'s turn, or an `open_session` onto a
            # session mid-call -- produces no further event to wake on.
            self._scan_client_tools(tools, started)
            async for tagged in events:
                event = tagged.event
                if not isinstance(event, ActionEvent) or event.rejection_reason is not None:
                    continue
                # Scoped to this session's channels: the fan-in tap carries
                # every session on the connection, and an unscoped pump is the
                # other half of the double-run defect `_chat_uris` describes.
                if tagged.channel != self.uri and tagged.channel not in self._chat_uris():
                    continue
                self._scan_client_tools(tools, started)
        except Exception:
            # Ends the pump, quietly. Reporting the result means dispatching, so
            # anything reaching here is the connection going away underneath us
            # -- and an exception left on a task nobody awaits is reported by
            # asyncio at an unrelated moment with a stack that points nowhere
            # useful, which is exactly the shape this file avoids elsewhere.
            return
        finally:
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await watcher
            await events.aclose()
            await states.aclose()

    async def _rescan_on_reconnect(
        self, states: Any, tools: ClientToolHost, started: set[tuple[str, str]]
    ) -> None:
        """Wake the level scan when a connection comes (back) up.

        A snapshot-arm resume replays **no actions**, so a call handed over
        during the gap never reaches the event loop above -- but the fresh
        snapshots do carry it, as a running call and as a
        `SessionState.inputNeeded` entry. `state_changes()` broadcasting
        ``connected`` is the one observable signal that those snapshots just
        landed.
        """
        async for state in states:
            if getattr(state, "status", "") == "connected":
                with contextlib.suppress(Exception):
                    self._scan_client_tools(tools, started)

    def _scan_client_tools(self, tools: ClientToolHost, started: set[tuple[str, str]]) -> None:
        """One level scan: start every owned, handed-over, unexecuted call."""
        for chat_uri, turn_id, call in self._handed_over_calls(tools):
            key = (chat_uri, str(call.get("toolCallId", "")))
            if key in started:
                continue
            started.add(key)
            # Action-shaped, from state: `chat/toolCallConfirmed` carries no
            # input and a scan has no action at all, so the mirror's `toolInput`
            # -- which the reducer keeps current through edits -- is what the
            # executor gets.
            action: JsonObject = {"turnId": turn_id, "toolCallId": key[1]}
            if "toolInput" in call:
                action["toolInput"] = call.get("toolInput")
            self._spawn_executor(tools, chat_uri, action, str(call.get("toolName", "")))

    def _handed_over_calls(
        self, tools: ClientToolHost
    ) -> Iterator[tuple[str, str, Mapping[str, Any]]]:
        """Every ``(chat, turnId, call)`` this client is expected to run, now.

        Two sources, deliberately both. The chat states cover every chat this
        client is subscribed to. ``SessionState.inputNeeded`` -- whose
        `toolClientExecution` entries exist precisely "so a client that
        provides the tool can pick up the work without subscribing to the
        owning chat" -- covers the ones it is not, and is all a reconnect
        snapshot needs to carry for recovery to work.

        **Confirmed** state on both arms: `status == "running"` is the
        handover marker (the reducer only enters `running` via a `confirmed`
        ready or an approval), and our own optimistic actions must not feed
        the scan that decides whether to run a tool.
        """
        for chat_uri in self._chat_uris():
            state = self._client.mirror.confirmed(chat_uri)
            if not isinstance(state, Mapping):
                continue
            turns = state.get("turns")
            candidates: list[Any] = [state.get("activeTurn")]
            candidates.extend(turns if isinstance(turns, list) else ())
            for turn in candidates:
                if not isinstance(turn, Mapping):
                    continue
                turn_id = str(turn.get("id", ""))
                parts = turn.get("responseParts")
                for part in parts if isinstance(parts, list) else ():
                    call = part.get("toolCall") if isinstance(part, Mapping) else None
                    if not isinstance(call, Mapping) or call.get("status") != "running":
                        continue
                    if tools.owns(call):
                        yield chat_uri, turn_id, call
        session_state = self._client.mirror.confirmed(self.uri)
        entries = session_state.get("inputNeeded") if isinstance(session_state, Mapping) else None
        for entry in entries if isinstance(entries, list) else ():
            if not isinstance(entry, Mapping) or entry.get("kind") != "toolClientExecution":
                continue
            call = entry.get("toolCall")
            if isinstance(call, Mapping) and tools.owns(call):
                yield str(entry.get("chat", "")), str(entry.get("turnId", "")), call

    def _spawn_executor(
        self, tools: ClientToolHost, chat: str, action: JsonObject, tool_name: str
    ) -> None:
        """Run one call on its own task, so the pump never blocks on a tool.

        Awaiting the executor inline is how one slow tool stalled the reader:
        the fan-in tap kept filling behind it (drop-oldest, 4096), later calls
        -- including the auto-denials `ClientToolHost` guarantees -- waited on
        an unrelated tool, and past the bound the pump was fast-forwarded over
        events it never saw. Tracked in ``_tool_tasks`` and cancelled with the
        pump; bounded by ``_tool_gate`` so a flood of calls cannot spawn
        without limit.
        """

        async def run() -> None:
            try:
                async with self._tool_gate:
                    await tools.execute(chat, action, tool_name=tool_name)
            except Exception:
                # Reporting the result means dispatching; anything reaching
                # here is the connection going away underneath us.
                return

        task = asyncio.get_running_loop().create_task(
            run(), name=f"ahp-client-tool-{chat}-{action.get('toolCallId', '')}"
        )
        self._tool_tasks.add(task)
        task.add_done_callback(self._tool_tasks.discard)

    async def _stop_tools(self) -> None:
        if self._tool_pump is not None:
            pump, self._tool_pump = self._tool_pump, None
            pump.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await pump
        # The executors are ours too: a tool still running against a session
        # the caller has finished with holds this object -- and its connection
        # -- alive from a task nobody can reach.
        for task in list(self._tool_tasks):
            task.cancel()
        for task in list(self._tool_tasks):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tool_tasks.clear()

    async def dispose(self) -> None:
        await self._stop_tools()
        await self._client.protocol.dispose_session(self.uri)

    async def _await_ready(self, timeout: float) -> None:
        """Wait for readiness -- but only if the host did not answer provisionally.

        A host that reports ``lifecycle == "creating"`` is telling us the session
        exists and is still warming up. Waiting unconditionally deadlocks for the
        full timeout against exactly that host.

        The failure value is ``"creationFailed"`` -- `SessionLifecycle` is
        exactly ``creating | ready | creationFailed``, and testing ``"failed"``
        (the unrelated connection-level `HostStatus` value) made the raise
        unreachable. And a deadline that expires **raises**: returning silently
        hands the caller a dead `Session` reported as success, and makes
        `ready_timeout` a parameter that can never do anything.
        """
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            lifecycle = str(self.state.get("lifecycle", ""))
            if lifecycle in {"ready", "creating"}:
                return
            if lifecycle == "creationFailed":
                # `creationError` is the reducer-recorded `ErrorInfo` for
                # exactly this lifecycle; without it the caller gets "failed"
                # with the reason left on the host.
                raise AhpClientError(
                    f"session {self.uri} failed to start: "
                    f"{self.state.get('creationError') or 'no creationError published'}"
                )
            if asyncio.get_running_loop().time() >= deadline:
                raise AhpClientError(
                    f"session {self.uri} published no lifecycle within {timeout}s; "
                    "the host never reported ready, creating or creationFailed"
                )
            await asyncio.sleep(0.01)

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
            reason = tagged.event.rejection_reason
            if reason is not None:
                # A rejected envelope describes an action that was NOT applied
                # (invariant 5: the host fans a refused action to every
                # subscriber, and no peer may apply it). Decoded as its action
                # type it lies -- a refused `chat/turnStarted` reads as a turn
                # beginning, and two clients racing to start turns is exactly
                # this class's scenario. That one surfaces as the failure it
                # is; every other refusal is the originator's optimistic
                # effect being reverted, which is not this watcher's business.
                action = envelope.get("action")
                if isinstance(action, Mapping) and action.get("type") == "chat/turnStarted":
                    return TurnFailed(dict(envelope), reason)
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
        approval_timeout: float = 300.0,
        model: str | Mapping[str, Any] | None = None,
    ) -> None:
        self._client = client
        self._chat = chat
        self._text = text
        self._policy = resolve_policy(approvals)
        self._turn_id = turn_id or str(uuid.uuid4())
        self._idle_timeout = idle_timeout
        #: How long a drained stream under the manual default waits for an
        #: *external* answer to a surfaced tool call before raising
        #: `UnansweredToolCallError` (plan §7).
        self._approval_timeout = approval_timeout
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
        try:
            last: Any = None
            async for event in self:
                if isinstance(event, ToolCallReady):
                    if isinstance(self._policy, ManualPolicy):
                        # The manual default: the event has surfaced (on the
                        # mirror, on `Session.inputs()`, on every other
                        # subscriber), so wait for someone to answer it rather
                        # than answering either way ourselves.
                        await self._await_external_answer(event)
                        continue
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
        finally:
            # `_drain` can leave the iteration early -- `UnansweredToolCallError`
            # is the designed exit -- and an abandoned iteration is exactly the
            # cursor leak `aclose` exists for.
            await self.aclose()

    async def _await_external_answer(self, event: ToolCallReady) -> None:
        """Wait out the manual default: somebody else answers, or we raise.

        The answer is observed on the **mirror**, not on this stream's events:
        a `chat/toolCallConfirmed` -- ours or another client's -- moves the call
        off `pending-confirmation`, and the reducer is the one place that
        outcome is already computed. Waiting on the raw events would mean
        re-deriving first-answer-wins arbitration here.

        After `approval_timeout` this raises `UnansweredToolCallError` -- the
        plan §7 default. Silently approving runs a tool the user never saw;
        silently denying ends the turn without the user ever learning why; a
        loud, specific error is better than both.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._approval_timeout
        while loop.time() < deadline:
            call = _tool_call_in(self._client.mirror.confirmed(self._chat.uri), event.tool_call_id)
            status = str(call.get("status", "")) if call is not None else ""
            if status and status != "pending-confirmation":
                return
            await asyncio.sleep(0.05)
        raise UnansweredToolCallError(event.tool_call_id, event.tool_name)

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
            await self._finish()
            return terminal
        await self._start()
        loop = asyncio.get_running_loop()
        deadline = None if self._idle_timeout is None else loop.time() + self._idle_timeout
        while True:
            timeout = None if deadline is None else max(0.0, deadline - loop.time())
            try:
                tagged = await asyncio.wait_for(self._next_tagged(), timeout)
            except TimeoutError as exc:
                await self._finish()
                raise TimeoutError(
                    f"turn {self._turn_id} produced nothing for {self._idle_timeout}s"
                ) from exc
            if tagged is None:
                # The connection ended under us. The spec says an in-progress
                # turn SHOULD be considered failed after an unexpected
                # termination, so say so rather than ending silently.
                await self._finish()
                raise StopAsyncIteration
            disposed = self._disposal(tagged)
            if disposed is not None:
                await self._finish()
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
                await self._finish()
                return refused
            if tagged.event.rejection_reason is not None:
                # Any OTHER rejected envelope describes an action that was NOT
                # applied, so decoding it by type lies: a rejected
                # `chat/turnCancelled` would read as this turn genuinely
                # cancelled -- terminal -- and a rejected `chat/toolCallComplete`
                # as real progress. A rejected `chat/toolCallConfirmed` is
                # another client having answered first ("not an error"); the
                # mirror reverts the optimistic effect and the turn carries on.
                continue

            if not is_modelled(envelope):
                # Everything rejected was handled above, so what remains here
                # really is noise -- mostly this caller's own accepted writes
                # echoing back.
                continue

            event = event_for(envelope, self._dispatch, self._tools)
            if isinstance(event, TurnCompleted):
                await self._finish()
                return self._finalise(event)
            if isinstance(event, TurnFailed | TurnCancelled):
                await self._finish()
                return event
            return event

    async def _next_tagged(self) -> Any:
        return await _next_or_none(self._reader)

    async def _finish(self) -> None:
        """End the stream and detach its cursor, in that order, exactly once.

        The detach is the half that was missing: a `BroadcastQueue` trims only
        past its **lowest** cursor, so a reader nobody closes pins the
        connection-wide tap forever -- one dead cursor per completed `prompt()`,
        an events buffer parked at its 4096-entry bound, and a permanent
        `DroppedEvents` diagnostic stream on every long-lived connection.
        `Session.inputs`, `ChatWatch` and the tool pump all already detach;
        this is the same convention on the one surface that leaked.
        """
        self._finished = True
        reader, self._reader = self._reader, None
        if reader is not None:
            await reader.aclose()

    async def aclose(self) -> None:
        """Release the stream's cursor without waiting for a terminal event.

        For the caller that abandons iteration -- `async for` never calls this
        on `break`, and a `TurnStream` held past its usefulness would otherwise
        keep its cursor attached for the life of the connection.
        """
        await self._finish()

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

    async def aclose(self) -> None:
        """Delegate the cursor release; a stream never resolved holds none."""
        if self._inner is not None:
            await self._inner.aclose()
