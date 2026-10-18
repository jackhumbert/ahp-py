"""Which configured provider runs an `agent()` call (plan §5)."""

from __future__ import annotations

import fnmatch
from collections.abc import Mapping
from typing import Any

from ahp_host_flow.config import FlowConfig, Routing, RoutingRule
from ahp_host_flow.engine import AgentCall


def _matches(rule: RoutingRule, call: AgentCall) -> bool:
    if rule.model is not None:
        model = call.opts.get("model")
        if not isinstance(model, str) or model != rule.model:
            return False
    if rule.phase is not None and rule.phase != call.phase:
        return False
    return rule.label is None or (
        isinstance(call.label, str) and fnmatch.fnmatchcase(call.label, rule.label)
    )


def x_opts(opts: Mapping[str, Any]) -> Mapping[str, Any]:
    """`opts.x`, the extension options; a non-object `x` is ignored, as Claude Code ignores it."""
    x = opts.get("x")
    return x if isinstance(x, Mapping) else {}


def pinned_provider(call: AgentCall) -> str | None:
    """The provider `opts.x.provider` pins the call to, if it is a non-empty string."""
    provider = x_opts(call.opts).get("provider")
    return provider if isinstance(provider, str) and provider else None


def route(routing: Routing, call: AgentCall) -> str:
    """The provider for `call`: the one `opts.x.provider` pins, else the first rule whose every
    set field matches, else the default."""
    pinned = pinned_provider(call)
    if pinned is not None:
        return pinned
    for rule in routing.rules:
        if _matches(rule, call):
            return rule.provider
    return routing.default


def resolved_route(
    config: FlowConfig, call: AgentCall, run_model: str | None
) -> dict[str, str | None]:
    """The provider and model *call* resolves to, from the config alone.

    Never from a provider's discovered model list, so it is known before dispatch. `opts.model`
    is a configured alias (or passes through as it is); else the run's model, if the provider
    lists it; else None, the provider's default.
    """
    provider = route(config.routing, call)
    spec = config.providers.get(provider)
    option = call.opts.get("model")
    model: str | None
    if isinstance(option, str):
        model = spec.models.get(option, option) if spec is not None else option
    elif run_model is not None and spec is not None and run_model in spec.models.values():
        model = run_model
    else:
        model = None
    return {"provider": provider, "model": model}
