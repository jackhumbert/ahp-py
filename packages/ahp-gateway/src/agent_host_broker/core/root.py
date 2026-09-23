"""The fleet's root channel, merged from every node's.

`ahp-root://` is the one channel every node has, and the one the surface must
see exactly once. The broker keeps each node's root state (reduced with the
protocol's own root reducer) and derives the surface's from them:

* `agents` - the union, first node (in inventory order) winning a provider id
  two nodes both offer. A provider id is how `createSession` picks an agent, so
  it cannot appear twice; which node a session lands on is decided by its
  working directory (see `agent_host_broker.core.uris`).
* `activeSessions` - the sum.
* `terminals` - the concatenation.
* `config` - never advertised. It is a per-host settings schema, and merging
  two hosts' schemas into one that a `root/configChanged` could be dispatched
  against is a capability this broker does not have (invariant 4).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

__all__ = ["merge_root", "root_actions"]


def merge_root(states: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    agents: list[Any] = []
    providers: set[str] = set()
    active = 0
    terminals: list[Any] = []
    for state in states:
        for agent in state.get("agents") or []:
            provider = agent.get("provider") if isinstance(agent, Mapping) else None
            if not isinstance(provider, str) or provider in providers:
                continue
            providers.add(provider)
            agents.append(agent)
        count = state.get("activeSessions")
        if isinstance(count, int) and not isinstance(count, bool):
            active += count
        listed = state.get("terminals")
        if isinstance(listed, list):
            terminals.extend(listed)
    merged: dict[str, Any] = {"agents": agents, "activeSessions": active}
    if terminals:
        merged["terminals"] = terminals
    return merged


def root_actions(before: Mapping[str, Any], after: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The root actions that take a surface's merged state from `before` to `after`.

    Full-replacement actions, one per field that moved, so the surface's own
    root reducer arrives at exactly `after`.
    """
    actions: list[dict[str, Any]] = []
    if before.get("agents") != after.get("agents"):
        actions.append({"type": "root/agentsChanged", "agents": after.get("agents", [])})
    if before.get("activeSessions") != after.get("activeSessions"):
        actions.append(
            {"type": "root/activeSessionsChanged", "activeSessions": after.get("activeSessions", 0)}
        )
    if before.get("terminals") != after.get("terminals"):
        actions.append({"type": "root/terminalsChanged", "terminals": after.get("terminals", [])})
    return actions
