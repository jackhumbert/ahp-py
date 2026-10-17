"""Which configured provider runs an `agent()` call (plan §5)."""

from __future__ import annotations

import fnmatch

from ahp_host_flow.config import Routing, RoutingRule
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


def route(routing: Routing, call: AgentCall) -> str:
    """The provider for `call`: the first rule whose every set field matches, else the default."""
    for rule in routing.rules:
        if _matches(rule, call):
            return rule.provider
    return routing.default
