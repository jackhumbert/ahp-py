"""`RoutingDispatcher`: each `agent()` call to the dispatcher for its provider kind (plan §4.4)."""

from __future__ import annotations

import itertools
from collections.abc import Callable, Iterator, Mapping
from typing import Any

from ahp_host_flow.config import FlowConfig, ProviderConfig
from ahp_host_flow.dispatch import (
    _ATTEMPT,
    _FALLBACK_KINDS,
    _ROUTE_OVERRIDE,
    FlowAgentError,
    _log,
    named_chat,
)
from ahp_host_flow.engine import (
    AgentCall,
    AgentResult,
    Dispatcher,
    DispatchError,
    begin_usage_attempt,
    end_usage_attempt,
)
from ahp_host_flow.limits import ProviderSlotsRegistry
from ahp_host_flow.routing import fallback_chain, x_opts


class RoutingDispatcher:
    """Routes each `agent()` call to the dispatcher for its provider's kind."""

    reports_dispatch: bool = True

    def __init__(
        self,
        config: FlowConfig,
        *,
        inprocess: Dispatcher,
        fleet: Dispatcher | None = None,
        remote: Callable[[str, ProviderConfig], Dispatcher] | None = None,
        slots: ProviderSlotsRegistry | None = None,
    ) -> None:
        self.config = config
        #: Per-provider concurrency, shared with every other run; held inside this run's own
        #: Scheduler slot, never instead of it.
        self._slots = slots
        self._by_kind: dict[str, Dispatcher | None] = {"inprocess": inprocess, "fleet": fleet}
        #: Makes the dispatcher of a remote provider, on its first call.
        self._remote = remote
        self._remotes: dict[str, Dispatcher] = {}
        self._run_id: str | None = None

    def _inner(self) -> list[Dispatcher]:
        return [
            *(d for d in self._by_kind.values() if d is not None),
            *self._remotes.values(),
        ]

    @property
    def run_id(self) -> str | None:
        return self._run_id

    @run_id.setter
    def run_id(self, value: str | None) -> None:
        self._run_id = value
        for dispatcher in self._inner():
            if hasattr(dispatcher, "run_id"):
                dispatcher.run_id = value

    async def run_agent(self, call: AgentCall) -> AgentResult:
        chain = fallback_chain(self.config, call)
        if chain[0] not in self.config.providers:
            configured = ", ".join(sorted(self.config.providers))
            raise FlowAgentError(
                f"provider {chain[0]!r} is not configured (opts.x.provider); "
                f"configured: {configured}"
            )
        in_process_only = named_chat(call.opts) is not None or "attach" in x_opts(call.opts)
        attempts = itertools.count()
        failed = chain[0]
        error: DispatchError | None = None
        for index, name in enumerate(chain):
            spec = self.config.providers[name]
            if index:
                assert error is not None
                if in_process_only and spec.kind != "inprocess":
                    self._note(
                        f"agent() #{call.ordinal}: fallback to {name} skipped ({name} is a "
                        f"{spec.kind} provider; opts.x.chat/attach need an in-process one)"
                    )
                    continue
                self._note(
                    f"agent() #{call.ordinal}: fell back from {failed} to {name} ({error.kind})"
                )
            token = _ROUTE_OVERRIDE.set(name if index else None)
            try:
                return await self._run_on(name, spec, call, attempts)
            except DispatchError as exc:
                if not (exc.kind in _FALLBACK_KINDS or (exc.kind == "failed" and exc.transient)):
                    raise
                error, failed = exc, name
            finally:
                _ROUTE_OVERRIDE.reset(token)
        assert error is not None
        raise error

    def _note(self, line: str) -> None:
        _log.info("%s", line)

    async def _run_on(
        self,
        name: str,
        spec: ProviderConfig,
        call: AgentCall,
        attempts: Iterator[int],
    ) -> AgentResult:
        """One provider's turn at *call*, with its `retry_empty` attempts."""
        kind = spec.kind
        chat = named_chat(call.opts)
        if (chat is not None or "attach" in x_opts(call.opts)) and kind != "inprocess":
            raise FlowAgentError(
                f"opts.x.{'chat' if chat else 'attach'} needs an in-process provider; "
                f"{name!r} is a {kind} provider"
            )
        dispatcher: Dispatcher | None
        if kind == "remote" and self._remote is not None:
            dispatcher = self._remotes.get(name)
            if dispatcher is None:
                dispatcher = self._remotes[name] = self._remote(name, spec)
                if hasattr(dispatcher, "run_id"):
                    dispatcher.run_id = self._run_id
        else:
            dispatcher = self._by_kind.get(kind)
        if dispatcher is None:
            raise FlowAgentError(f"provider {name!r}: {kind} dispatch is not available here")
        if spec.retry_empty <= 0:
            return await self._attempt(dispatcher, call, name, attempts)
        return await self._run_retrying(dispatcher, call, name, spec.retry_empty, attempts)

    async def _attempt(
        self, dispatcher: Dispatcher, call: AgentCall, name: str, attempts: Iterator[int]
    ) -> AgentResult:
        """One dispatch inside the provider's concurrency slot; a busy provider halves the limit.

        Each dispatch of a call takes the next attempt number, so its tool call id is its own,
        and a usage slot of its own, so the Scheduler counts a failed attempt's usage too.
        """
        token = _ATTEMPT.set(next(attempts))
        reporter, usage_token = begin_usage_attempt()
        try:
            if self._slots is None:
                result = await dispatcher.run_agent(call)
            else:
                slots = self._slots.for_name(name)
                async with slots.hold():
                    try:
                        result = await dispatcher.run_agent(call)
                    except DispatchError as exc:
                        if exc.kind == "rate_limited" or (
                            exc.kind == "unavailable" and exc.transient
                        ):
                            slots.on_busy(self._slots.on_log, exc.kind)
                        raise
            # The attempt's result is its settled usage (it replaces what it reported live).
            settled: dict[str, Any] = dict(result.usage)
            if result.cost_usd is not None:
                settled["cost_usd"] = result.cost_usd
            reporter(settled)
            return result
        finally:
            end_usage_attempt(usage_token)
            _ATTEMPT.reset(token)

    async def _run_retrying(
        self,
        dispatcher: Dispatcher,
        call: AgentCall,
        name: str,
        retries: int,
        attempts: Iterator[int],
    ) -> AgentResult:
        """Dispatches *call* again while its reply is empty, up to *retries* more times.

        The Scheduler sees one result: the last attempt's value, with the usage and cost of every
        attempt summed (one ordinal, one key, one journal record).
        """
        schema = call.opts.get("schema")
        usage: dict[str, int] = {}
        cost: float | None = None
        attempt = 0
        while True:
            result = await self._attempt(dispatcher, call, name, attempts)
            for key, count in result.usage.items():
                usage[key] = usage.get(key, 0) + count
            if result.cost_usd is not None:
                cost = (cost or 0.0) + result.cost_usd
            empty = (
                result.value is None
                if isinstance(schema, Mapping)
                else isinstance(result.value, str) and result.value.strip() == ""
            )
            if not empty:
                return AgentResult(result.value, usage=usage, cost_usd=cost)
            if attempt >= retries:
                raise FlowAgentError(
                    f"agent() #{call.ordinal}: {name} returned an empty reply {retries + 1} times",
                    kind="empty",
                    transient=True,
                )
            attempt += 1
            _log.info(
                "agent() #%d: empty reply from %s; retrying (%d of %d)",
                call.ordinal,
                name,
                attempt,
                retries,
            )

    async def aclose(self) -> None:
        for dispatcher in self._inner():
            close = getattr(dispatcher, "aclose", None)
            if close is not None:
                await close()
