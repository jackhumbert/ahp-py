"""`ClientDispatcher`: each `agent()` call as a session on another host, dialed with ahp-client.

Limits of a remote call, which only a gateway's fleet dispatch lifts (README, plan §2):

* The worker chat is a mirror of the remote turn's **text, at completion**: the
  remote session holds the transcript, tool calls and approvals.
* An approval or a question the remote agent raises is relayed to the worker
  chat when there is one (`RemoteOut.confirm` and `ask`), and the answer goes
  back to the remote session. While it is open the idle timer is paused: a
  person may take longer than `RemoteSpec.idle_timeout` to answer, and the
  answer still lands. `RemoteSpec.approval_timeout`, when set, bounds that wait
  (the call fails and the remote turn is stopped); otherwise the run's abort is
  what ends a wait nobody answers. A bare
  dispatcher (no worker chat) cannot answer: someone has to answer it in a
  client of that host, and if nothing happens for `idle_timeout` seconds the
  call is stopped and fails, rather than hanging the run.
* `opts.x.cwd` must be an absolute path or a `file:` URI on the remote host; it is sent as
  the session's working directory and not checked here. `isolation` is not applied.
* A remote call's worker chat runs the run's client tools for the remote agent: they are
  advertised on the remote session (`OffersClientTools`), and its calls of them run on the
  worker chat through the run's own clients.
* A call a restart cut off is not reattached: the remote turn it left is
  stopped, and the call is dispatched again.
* A provider with `launch` has its host started by the launcher: once, on first use, and torn
  down by `aclose`; with `per_call`, a host for every call, given the call's folder and the
  client tools and torn down after it on every path (done, failed, skipped, aborted). The
  host a restart cut off is gone, so a reattach is run again.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any, Final, Protocol, TypeVar, runtime_checkable

from ahp_client import (
    Client,
    InputRequested,
    ToolCallReady,
    ToolCallResultReview,
    TurnCancelled,
    TurnCompleted,
    TurnFailed,
    UnansweredToolCallError,
    connect,
)
from ahp_client.serve import ClientToolHost
from ahp_protocol.transport import Transport

from ahp_host_flow.engine.dispatcher import (
    AgentCall,
    AgentResult,
    DispatchError,
    DispatchHandle,
    ErrorKind,
    reattach_handle,
    report_dispatch,
)
from ahp_host_flow.engine.errorkinds import REMOTE_IDLE, REMOTE_LOST, classify
from ahp_host_flow.engine.preamble import with_preamble
from ahp_host_flow.engine.usage import read_usage

_log = logging.getLogger(__name__)

_T = TypeVar("_T")

TITLE_MAX: Final = 60
#: Seconds a remote turn may go without an event before the call is stopped and fails: a
#: question or an approval there that nobody answers would otherwise hang the run.
IDLE_TIMEOUT: Final = 600.0


class RemoteAgentError(DispatchError):
    """A remote `agent()` call failed."""

    def __init__(
        self, message: str, *, kind: ErrorKind = "failed", transient: bool = False
    ) -> None:
        super().__init__(kind, transient, message)


@dataclass(frozen=True)
class LaunchRequest:
    """What a launcher is asked to start a host for."""

    #: The provider's name in flow.toml.
    provider: str
    agent: str
    #: The call's `opts.x.cwd` as `folder_uri` gives it; None for a host shared by the run, or a
    #: call without cwd.
    folder: str | None
    #: The definitions the remote session will be offered.
    client_tools: tuple[Mapping[str, Any], ...] = ()


# Returns the transport to the launched host and the coroutine function that tears it down.
Launcher = Callable[[LaunchRequest], Awaitable[tuple[Transport, Callable[[], Awaitable[None]]]]]


@dataclass(frozen=True)
class RemoteSpec:
    """Where a remote provider lives: the host to dial and the agent to start there."""

    name: str
    agent: str
    url: str | None = None
    token: str | None = None
    #: Dials the host without a socket (tests, other transports); wins over *url*.
    transport_factory: Callable[[], Awaitable[Transport]] | None = None
    models: Mapping[str, str] = field(default_factory=dict)
    #: The run's own model (the main chat's selection), for calls that name none. Used when
    #: *models* names it or is empty, as for fleet calls.
    run_model: str | None = None
    idle_timeout: float = IDLE_TIMEOUT
    #: Seconds a relayed approval or question waits for an answer on the worker chat before
    #: the call fails (flow.toml `approval_timeout`); None waits until the run is aborted.
    approval_timeout: float | None = None
    #: Text put before the prompt the remote agent is sent (flow.toml `preamble`).
    preamble: str | None = None
    #: Starts the host (flow.toml `launch`); wins over *url* and *transport_factory*.
    launch: Launcher | None = None
    #: Start a host for every call and tear it down after it (flow.toml `per_call`).
    per_call: bool = False


@dataclass(frozen=True)
class RemoteConfirmation:
    """A tool call the remote agent wants approved."""

    tool_call_id: str
    tool_name: str
    invocation_message: str
    tool_input: Any = None
    confirmation_title: str | None = None
    read_only: bool | None = None


@dataclass(frozen=True)
class RemoteDecision:
    """The answer to a `RemoteConfirmation`."""

    approved: bool
    reason_message: str | None = None
    #: The wire `Message` of what to do instead, for a denial.
    user_suggestion: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class RemoteQuestion:
    """An input request the remote agent raised."""

    message: str | None
    url: str | None
    questions: tuple[Mapping[str, Any], ...] = ()


@dataclass(frozen=True)
class RemoteAnswer:
    """The answer to a `RemoteQuestion`: `accept`, `decline` or `cancel`, and the answers."""

    response: str
    answers: Mapping[str, Any]


class RemoteOut(Protocol):
    """Where a remote call's progress goes."""

    async def text(self, delta: str) -> None: ...

    async def linked(self, session_uri: str) -> None: ...

    async def confirm(self, request: RemoteConfirmation) -> RemoteDecision | None:
        """Ask for the approval; None: no one can answer here."""
        ...

    async def ask(self, request: RemoteQuestion) -> RemoteAnswer | None:
        """Ask the question; None: no one can answer here."""
        ...


class RemoteToolError(Exception):
    """A client tool the run could not run; its message becomes the tool's failure result."""


@runtime_checkable
class OffersClientTools(Protocol):
    """A `RemoteOut` that also runs the run's client tools for the remote agent."""

    def tools(self) -> Sequence[Mapping[str, Any]]:
        """The tool definitions to advertise on the remote session."""
        ...

    async def run_tool(self, name: str, tool_input: Any, tool_call_id: str) -> Mapping[str, Any]:
        """Run it on the worker chat and return a `ToolCallResult`; `RemoteToolError` fails it."""
        ...


class RemoteWorkers(Protocol):
    """Gives a remote call a place to show itself (a worker chat) and runs *body* in it."""

    async def run(
        self,
        call: AgentCall,
        provider: str,
        body: Callable[[RemoteOut, str], Awaitable[AgentResult]],
    ) -> AgentResult: ...


def folder_uri(opts: Mapping[str, Any]) -> str | None:
    """`opts.x.cwd` as the file URI a remote host is asked to work in; None when absent."""
    x = opts.get("x")
    if not isinstance(x, Mapping) or "cwd" not in x:
        return None
    value = x["cwd"]
    if not isinstance(value, str) or not value:
        raise RemoteAgentError("opts.x.cwd must be a non-empty string")
    if value.startswith("file:"):
        return value
    # The remote host's folders are not known here, so nothing is resolved or checked.
    if PurePosixPath(value).is_absolute():
        return PurePosixPath(value).as_uri()
    if PureWindowsPath(value).is_absolute():
        return PureWindowsPath(value).as_uri()
    raise RemoteAgentError(
        f"opts.x.cwd: {value} must be an absolute path or a file: URI for a remote provider"
    )


async def _finish(aw: Awaitable[Any]) -> None:
    """Run *aw* to the end even if this task is cancelled meanwhile; log, not raise, a failure."""
    running = asyncio.ensure_future(aw)
    cancelled = False
    while not running.done():
        try:
            await asyncio.shield(running)
        except asyncio.CancelledError:
            if running.cancelled():
                break
            cancelled = True
        except Exception:
            break
    if not running.cancelled():
        failure = running.exception()
        if failure is not None:
            _log.warning("cleaning up a launched host failed", exc_info=failure)
    if cancelled:
        raise asyncio.CancelledError


def _title(call: AgentCall) -> str:
    if isinstance(call.label, str) and call.label.strip():
        title = call.label.strip()
    else:
        lines = call.prompt.strip().splitlines()
        title = lines[0].strip() if lines else ""
    if not title:
        return "agent()"
    return title[: TITLE_MAX - 1] + "…" if len(title) > TITLE_MAX else title


def _confirmation(event: ToolCallReady) -> RemoteConfirmation:
    message = event.action.get("invocationMessage")
    if isinstance(message, Mapping):
        message = message.get("markdown")
    title = event.action.get("confirmationTitle")
    read_only = (event.annotations or {}).get("readOnlyHint")
    return RemoteConfirmation(
        tool_call_id=event.tool_call_id,
        tool_name=event.tool_name,
        invocation_message=message if isinstance(message, str) else "",
        tool_input=event.tool_input,
        confirmation_title=title if isinstance(title, str) else None,
        read_only=read_only if isinstance(read_only, bool) else None,
    )


def _question(event: InputRequested) -> RemoteQuestion:
    message, url = event.request.get("message"), event.request.get("url")
    return RemoteQuestion(
        message=message if isinstance(message, str) else None,
        url=url if isinstance(url, str) else None,
        questions=tuple(q for q in event.questions if isinstance(q, Mapping)),
    )


def _tool_executor(
    out: OffersClientTools, name: str
) -> Callable[..., Awaitable[Mapping[str, Any]]]:
    async def execute(action: Mapping[str, Any]) -> Mapping[str, Any]:
        return await out.run_tool(name, action.get("toolInput"), str(action.get("toolCallId", "")))

    return execute


class _BareOut:
    """No worker chat: only the dispatch handle is recorded."""

    def __init__(self, provider: str) -> None:
        self.provider = provider

    async def text(self, delta: str) -> None:
        return None

    async def linked(self, session_uri: str) -> None:
        report_dispatch(DispatchHandle(provider=self.provider, session=session_uri))

    async def confirm(self, request: RemoteConfirmation) -> RemoteDecision | None:
        return None

    async def ask(self, request: RemoteQuestion) -> RemoteAnswer | None:
        return None


class ClientDispatcher:
    """Runs each `agent()` call as a session on another host over one shared connection."""

    reports_dispatch: bool = True

    def __init__(
        self,
        spec: RemoteSpec,
        *,
        workers: RemoteWorkers | None = None,
        client_id: str = "ahp-host-flow",
        on_log: Callable[[str], None] | None = None,
    ) -> None:
        self.spec = spec
        self.workers = workers
        self.client_id = client_id
        self.on_log = on_log
        self._client: Client | None = None
        self._launched_close: Callable[[], Awaitable[None]] | None = None
        self._lock = asyncio.Lock()

    def _note(self, line: str) -> None:
        if self.on_log is not None:
            self.on_log(line)
        else:
            _log.info("%s", line)

    async def _connected(self) -> Client:
        async with self._lock:
            if self._client is None:
                spec = self.spec
                if spec.launch is not None:
                    transport, close = await spec.launch(
                        LaunchRequest(spec.name, spec.agent, None, ())
                    )
                    try:
                        self._client = await connect(transport=transport, client_id=self.client_id)
                    except BaseException:
                        await _finish(close())
                        raise
                    self._launched_close = close
                elif spec.transport_factory is not None:
                    self._client = await connect(
                        transport_factory=spec.transport_factory, client_id=self.client_id
                    )
                else:
                    self._client = await connect(
                        spec.url, token=spec.token, client_id=self.client_id
                    )
            return self._client

    @contextlib.asynccontextmanager
    async def _launched(self, request: LaunchRequest) -> AsyncIterator[Client]:
        """A host started for one call and torn down after it, whatever way the call ends."""
        spec = self.spec
        assert spec.launch is not None
        try:
            transport, close = await spec.launch(request)
        except Exception as exc:
            raise RemoteAgentError(
                f"provider {spec.name!r}: cannot launch a host: {exc}",
                kind="unavailable",
                transient=True,
            ) from exc
        try:
            client = await connect(transport=transport, client_id=self.client_id)
        except BaseException as exc:
            # A skip or abort can land while the client connects: the host is torn down then too.
            await _finish(close())
            if not isinstance(exc, Exception):
                raise
            raise RemoteAgentError(
                f"provider {spec.name!r}: cannot reach the launched host: {exc}",
                kind="unavailable",
                transient=True,
            ) from exc

        async def teardown() -> None:
            try:
                await client.aclose()
            except Exception:
                _log.warning("closing the client of a launched host failed", exc_info=True)
            finally:
                await close()

        try:
            yield client
        finally:
            # One shielded teardown: a cancellation arriving while the client closes (an abort
            # or skip lands twice) must not skip stopping the host.
            await _finish(teardown())

    async def aclose(self) -> None:
        async with self._lock:
            client, self._client = self._client, None
            close, self._launched_close = self._launched_close, None
        if client is not None:
            with contextlib.suppress(Exception):
                await client.aclose()
        if close is not None:
            await _finish(close())

    async def run_agent(self, call: AgentCall) -> AgentResult:
        iso = call.opts.get("isolation")
        if iso == "worktree":
            self._note(
                f"agent() #{call.ordinal}: isolation 'worktree' is not applied on remote "
                f"provider {self.spec.name!r}; ignored"
            )
        elif iso is not None:
            self._note(f"agent() #{call.ordinal}: isolation {iso!r} is not supported; ignored")
        # Read here, in the dispatch task: the worker callback runs in a task of its own.
        before = reattach_handle()
        if before is not None and before.session and before.provider == self.spec.name:
            if self.spec.per_call:
                self._note(
                    f"agent() #{call.ordinal}: provider {self.spec.name!r} launches a host per "
                    "call; the one the restart cut off is gone, so the call runs again"
                )
            else:
                await self._stop_cut_off(call, before.session)
        if self.workers is not None:
            return await self.workers.run(
                call, self.spec.name, lambda out, title: self._body(call, out, title)
            )
        return await self._body(call, _BareOut(self.spec.name), _title(call))

    async def _stop_cut_off(self, call: AgentCall, uri: str) -> None:
        """Stop the remote turn a restart cut this call off from; the call runs again.

        Not reattached: the turn's text would come back, but the remote session
        may have stopped meanwhile for want of an answer only this run could have
        given, and a mirror that started late cannot tell. Left running, it would
        be an orphan working on the same prompt as its replacement.
        """
        try:
            client = await self._connected()
            session = await client.open_session(uri)
            chat = await session.chat()
            running = chat.state.get("activeTurn") is not None
            if running:
                await chat.cancel()
        except Exception as exc:
            self._note(
                f"agent() #{call.ordinal}: could not stop remote session {uri} that the "
                f"restart cut off ({exc}); dispatching it again"
            )
            return
        self._note(
            f"agent() #{call.ordinal}: "
            + (
                f"stopped remote session {uri} that the restart cut off"
                if running
                else f"remote session {uri} that the restart cut off is not running"
            )
            + "; dispatching it again"
        )

    def _model(self, call: AgentCall) -> str | None:
        spec = self.spec
        alias = call.opts.get("model")
        if isinstance(alias, str):
            return spec.models.get(alias, alias)
        run_model = spec.run_model
        if run_model is None:
            return None
        if run_model in spec.models:
            return spec.models[run_model]
        if not spec.models or run_model in spec.models.values():
            return run_model
        return None

    async def _relayed(self, awaitable: Awaitable[_T]) -> _T:
        """Wait for the worker chat's answer, however long a person takes (plan §8 M15 item 2).

        An open relayed request pauses `idle_timeout`: no `__anext__` is pending, so the
        stream's own idle timer (measured from each `__anext__`) is not running, and no
        timer of our own caps a person's answer. `idle_timeout` stays the backstop for a
        remote turn that goes silent; an abort cancels this wait. `approval_timeout`, when
        set, bounds it: the call fails naming the provider and the setting.
        """
        limit = self.spec.approval_timeout
        if limit is None:
            return await awaitable
        try:
            async with asyncio.timeout(limit):
                return await awaitable
        except TimeoutError:
            raise RemoteAgentError(
                f"provider {self.spec.name!r}: nobody answered the remote agent's request on "
                f"the worker chat within approval_timeout ({limit:g}s); stopped the remote turn",
                kind="timeout",
            ) from None

    async def _body(self, call: AgentCall, out: RemoteOut, title: str) -> AgentResult:
        spec = self.spec
        folder = folder_uri(call.opts)
        tools = tuple(out.tools()) if isinstance(out, OffersClientTools) else ()
        if spec.per_call:
            request = LaunchRequest(spec.name, spec.agent, folder, tools)
            async with self._launched(request) as client:
                return await self._run_on(client, call, out, title, folder, tools)
        try:
            shared = await self._connected()
        except Exception as exc:
            raise RemoteAgentError(
                f"provider {spec.name!r}: cannot reach the remote host: {exc}",
                kind="unavailable",
                transient=True,
            ) from exc
        return await self._run_on(shared, call, out, title, folder, tools)

    async def _run_on(
        self,
        client: Client,
        call: AgentCall,
        out: RemoteOut,
        title: str,
        folder: str | None,
        tools: tuple[Mapping[str, Any], ...],
    ) -> AgentResult:
        spec = self.spec
        try:
            host: ClientToolHost | None = None
            if isinstance(out, OffersClientTools) and tools:
                offers: OffersClientTools = out
                host = ClientToolHost(client.protocol, client_id=self.client_id)
                for definition in tools:
                    host.register(dict(definition), _tool_executor(offers, str(definition["name"])))
            session = await client.create_session(
                provider=spec.agent,
                working_directories=[folder] if folder is not None else None,
                tools=host,
            )
        except Exception as exc:
            raise RemoteAgentError(
                f"provider {spec.name!r}: cannot reach the remote host: {exc}",
                kind="unavailable",
                transient=True,
            ) from exc
        if session.capabilities.get("multipleChats") is not None:
            chat = await session.create_chat(title=title)
        else:
            chat = await session.chat()
            with contextlib.suppress(Exception):
                client.protocol.dispatch(
                    session.uri, {"type": "session/titleChanged", "title": title}
                )
        await out.linked(session.uri)
        raw_schema = call.opts.get("schema")
        schema = raw_schema if isinstance(raw_schema, Mapping) else None
        stream = chat.prompt(
            with_preamble(call.prompt, spec.preamble),
            output_schema=schema,
            model=self._model(call),
            idle_timeout=spec.idle_timeout,
        )
        event: Any = None
        try:
            async for item in stream:
                if isinstance(item, ToolCallReady):
                    decision = await self._relayed(out.confirm(_confirmation(item)))
                    if decision is None:
                        continue
                    if decision.approved:
                        item.approve()
                    else:
                        item.deny(
                            reason_message=decision.reason_message,
                            user_suggestion=decision.user_suggestion,
                        )
                elif isinstance(item, InputRequested):
                    answer = await self._relayed(out.ask(_question(item)))
                    if answer is None:
                        continue
                    if answer.response == "accept":
                        item.answer(answer.answers)
                    elif answer.response == "decline":
                        item.decline()
                    else:
                        item.cancel()
                elif isinstance(item, ToolCallResultReview):
                    item.confirm()
                elif isinstance(item, TurnCompleted | TurnFailed | TurnCancelled):
                    event = item
                    break
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await chat.cancel()
            raise
        except RemoteAgentError:
            # `approval_timeout` passed: stop the remote turn the request was holding.
            with contextlib.suppress(Exception):
                await chat.cancel()
            raise
        except (TimeoutError, UnansweredToolCallError) as exc:
            with contextlib.suppress(Exception):
                await chat.cancel()
            kind, transient = classify(REMOTE_IDLE)
            raise RemoteAgentError(
                f"the remote turn on {session.uri} waited {spec.idle_timeout:g}s with nothing "
                "happening (an approval or question there cannot be answered from this run); "
                f"stopped it: {exc}",
                kind=kind,
                transient=transient,
            ) from exc
        finally:
            await stream.aclose()
        if isinstance(event, TurnFailed):
            # The chat/error action, or the envelope's action wrapper around it.
            inner = event.action.get("action")
            part = (inner if isinstance(inner, Mapping) else event.action).get("part")
            resumable = isinstance(part, Mapping) and part.get("resumable") is True
            if event.error_type:
                kind, transient = classify(
                    event.error_type, resumable=resumable, message=event.reason
                )
            else:
                kind, transient = classify(REMOTE_LOST)
            raise RemoteAgentError(
                event.reason or "the remote turn failed", kind=kind, transient=transient
            )
        if isinstance(event, TurnCancelled) or not isinstance(event, TurnCompleted):
            return AgentResult(None)
        # The authoritative text, not the deltas, which miss a turn's opening characters.
        await out.text(event.text)
        usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0}
        cost: float | None = None
        recorded: list[Mapping[str, Any]] = [
            u for t in chat.turns() if isinstance(u := t.get("usage"), Mapping)
        ]
        if recorded:
            last = recorded[-1]
            reading = read_usage(
                input_tokens=last.get("inputTokens"),
                output_tokens=last.get("outputTokens"),
                cache_read_tokens=last.get("cacheReadTokens"),
                meta=last.get("_meta"),
            )
            usage["input_tokens"] = reading.input_tokens
            usage["output_tokens"] = reading.output_tokens
            usage["cache_read_tokens"] = reading.cache_read_tokens
            cost = reading.cost_usd
        return AgentResult(
            event.structured if schema is not None else event.text, usage=usage, cost_usd=cost
        )
