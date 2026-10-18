"""`ClientDispatcher`: each `agent()` call as a session on another host, dialed with ahp-client.

Limits of a remote call, which only a gateway's fleet dispatch lifts (README, plan §2):

* The worker chat is a mirror of the remote turn's **text, at completion**: the
  remote session holds the transcript, tool calls and approvals.
* An approval or a question the remote agent raises cannot be answered from
  here (the dispatcher is a bare client with no person behind it). Someone has
  to answer it in a client of that host, and if nothing happens for
  `RemoteSpec.idle_timeout` seconds the call is stopped and fails, rather than
  hanging the run.
* A call a restart cut off is not reattached: the remote turn it left is
  stopped, and the call is dispatched again.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final, Protocol

from ahp_client import (
    Client,
    TurnCancelled,
    TurnCompleted,
    TurnFailed,
    UnansweredToolCallError,
    connect,
)
from ahp_protocol.transport import Transport

from ahp_host_flow.engine.dispatcher import (
    AgentCall,
    AgentResult,
    DispatchHandle,
    reattach_handle,
    report_dispatch,
)
from ahp_host_flow.engine.preamble import with_preamble

_log = logging.getLogger(__name__)

TITLE_MAX: Final = 60
#: Seconds a remote turn may go without an event before the call is stopped and fails: a
#: question or an approval there that nobody answers would otherwise hang the run.
IDLE_TIMEOUT: Final = 600.0


class RemoteAgentError(RuntimeError):
    """A remote `agent()` call failed."""


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
    #: Text put before the prompt the remote agent is sent (flow.toml `preamble`).
    preamble: str | None = None


class RemoteOut(Protocol):
    """Where a remote call's progress goes."""

    async def text(self, delta: str) -> None: ...

    async def linked(self, session_uri: str) -> None: ...


class RemoteWorkers(Protocol):
    """Gives a remote call a place to show itself (a worker chat) and runs *body* in it."""

    async def run(
        self,
        call: AgentCall,
        provider: str,
        body: Callable[[RemoteOut, str], Awaitable[AgentResult]],
    ) -> AgentResult: ...


def _title(call: AgentCall) -> str:
    if isinstance(call.label, str) and call.label.strip():
        title = call.label.strip()
    else:
        lines = call.prompt.strip().splitlines()
        title = lines[0].strip() if lines else ""
    if not title:
        return "agent()"
    return title[: TITLE_MAX - 1] + "…" if len(title) > TITLE_MAX else title


class _BareOut:
    """No worker chat: only the dispatch handle is recorded."""

    def __init__(self, provider: str) -> None:
        self.provider = provider

    async def text(self, delta: str) -> None:
        return None

    async def linked(self, session_uri: str) -> None:
        report_dispatch(DispatchHandle(provider=self.provider, session=session_uri))


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
                if spec.transport_factory is not None:
                    self._client = await connect(
                        transport_factory=spec.transport_factory, client_id=self.client_id
                    )
                else:
                    self._client = await connect(
                        spec.url, token=spec.token, client_id=self.client_id
                    )
            return self._client

    async def aclose(self) -> None:
        async with self._lock:
            client, self._client = self._client, None
        if client is not None:
            with contextlib.suppress(Exception):
                await client.aclose()

    async def run_agent(self, call: AgentCall) -> AgentResult:
        # Read here, in the dispatch task: the worker callback runs in a task of its own.
        before = reattach_handle()
        if before is not None and before.session and before.provider == self.spec.name:
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

    async def _body(self, call: AgentCall, out: RemoteOut, title: str) -> AgentResult:
        spec = self.spec
        client = await self._connected()
        session = await client.create_session(provider=spec.agent)
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
        try:
            event = await chat.prompt(
                with_preamble(call.prompt, spec.preamble),
                output_schema=schema,
                model=self._model(call),
                idle_timeout=spec.idle_timeout,
                approval_timeout=spec.idle_timeout,
            )
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await chat.cancel()
            raise
        except (TimeoutError, UnansweredToolCallError) as exc:
            with contextlib.suppress(Exception):
                await chat.cancel()
            raise RemoteAgentError(
                f"the remote turn on {session.uri} waited {spec.idle_timeout:g}s with nothing "
                "happening (an approval or question there cannot be answered from this run); "
                f"stopped it: {exc}"
            ) from exc
        if isinstance(event, TurnFailed):
            raise RemoteAgentError(event.reason or "the remote turn failed")
        if isinstance(event, TurnCancelled) or not isinstance(event, TurnCompleted):
            return AgentResult(None)
        # The authoritative text, not the deltas, which miss a turn's opening characters.
        await out.text(event.text)
        usage = {"input_tokens": 0, "output_tokens": 0}
        recorded: list[Mapping[str, Any]] = [
            u for t in chat.turns() if isinstance(u := t.get("usage"), Mapping)
        ]
        if recorded:
            last = recorded[-1]
            for key, wire in (("input_tokens", "inputTokens"), ("output_tokens", "outputTokens")):
                count = last.get(wire)
                usage[key] = count if isinstance(count, int) else 0
        return AgentResult(event.structured if schema is not None else event.text, usage=usage)
