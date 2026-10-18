"""Routing rules: first match wins, every set field must match."""

from __future__ import annotations

from typing import Any

from ahp_host_flow.config import Routing, RoutingRule
from ahp_host_flow.engine import AgentCall
from ahp_host_flow.routing import route


def _call(
    opts: dict[str, Any] | None = None, phase: str | None = None, label: str | None = None
) -> AgentCall:
    return AgentCall(ordinal=0, prompt="p", opts=opts or {}, phase=phase, label=label)


def test_model_rule() -> None:
    routing = Routing("a", (RoutingRule("b", model="fast"),))
    assert route(routing, _call({"model": "fast"})) == "b"
    assert route(routing, _call({"model": "slow"})) == "a"
    assert route(routing, _call({"model": 3})) == "a"
    assert route(routing, _call()) == "a"


def test_phase_rule() -> None:
    routing = Routing("a", (RoutingRule("b", phase="Review"),))
    assert route(routing, _call(phase="Review")) == "b"
    assert route(routing, _call(phase="Plan")) == "a"
    assert route(routing, _call()) == "a"


def test_label_glob() -> None:
    routing = Routing("a", (RoutingRule("b", label="rev*"),))
    assert route(routing, _call(label="review-1")) == "b"
    assert route(routing, _call(label="Review")) == "a"


def test_label_none_never_matches() -> None:
    routing = Routing("a", (RoutingRule("b", label="*"),))
    assert route(routing, _call()) == "a"


def test_multi_field_rule_needs_all() -> None:
    routing = Routing("a", (RoutingRule("b", model="fast", phase="Review"),))
    assert route(routing, _call({"model": "fast"}, phase="Review")) == "b"
    assert route(routing, _call({"model": "fast"}, phase="Plan")) == "a"
    assert route(routing, _call({"model": "slow"}, phase="Review")) == "a"


def test_first_match_wins() -> None:
    routing = Routing("a", (RoutingRule("b", label="x*"), RoutingRule("c", label="xy*")))
    assert route(routing, _call(label="xyz")) == "b"


def test_default_without_rules() -> None:
    assert route(Routing("a"), _call(label="x")) == "a"


def test_flowext_provider_route_pin_wins() -> None:
    routing = Routing("a", (RoutingRule("b", model="glm"),))
    assert route(routing, _call({"model": "glm", "x": {"provider": "a"}})) == "a"
    assert route(routing, _call({"model": "glm"})) == "b"


def test_flowext_provider_route_ignores_bad_x() -> None:
    routing = Routing("a", (RoutingRule("b", model="glm"),))
    for x in ("b", {"provider": 5}, {"provider": ""}):
        assert route(routing, _call({"model": "glm", "x": x})) == "b"
        assert route(routing, _call({"x": x})) == "a"
