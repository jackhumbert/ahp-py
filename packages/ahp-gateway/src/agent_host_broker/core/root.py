"""The fleet's root channel, merged from every node's.

`ahp-root://` is the one channel every node has, and the one the surface must
see exactly once. The broker keeps each node's root state (reduced with the
protocol's own root reducer) and derives the surface's from them:

* `agents` - the union. A provider id two nodes both offer (the same agent
  running on two machines) appears once: the first node's entry (in inventory
  order), with the models every node offers - the first node's in its order,
  then any the others add. A provider id is how `createSession` picks an
  agent, so it cannot appear twice; which node a session lands on is decided
  by its working directory (see `agent_host_broker.core.uris`).
* `activeSessions` - the sum.
* `terminals` - the concatenation.
* `config` - never advertised at the root. It is a per-host settings schema,
  and merging two hosts' schemas into one that a `root/configChanged` could be
  dispatched against is a capability this broker does not have (invariant 4).
  Each node's own `config` rides verbatim in its entry of the node list
  instead (`node_details`), as information about that machine, not as a
  capability of the fleet.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

__all__ = ["merge_root", "node_details", "root_actions"]


def node_details(handshake: Mapping[str, Any], root: Mapping[str, Any]) -> dict[str, Any]:
    """What one node says about itself, verbatim, for its entry in the node list.

    The merge keeps only what the fleet can honour as one host, so everything
    host-specific - `serverInfo`, the root's `_meta`, its `config` - would
    otherwise vanish behind the broker. A client that needs one machine's
    view (a host's advertised sealing keys, its display name, its settings)
    reads it here, and the broker implements none of it: carried, not
    interpreted, so a node's extensions never become the broker's (invariant 3).
    """
    details: dict[str, Any] = {}
    info = handshake.get("serverInfo")
    if isinstance(info, Mapping):
        details["serverInfo"] = dict(info)
    meta = root.get("_meta")
    if isinstance(meta, Mapping) and meta:
        details["meta"] = dict(meta)
    config = root.get("config")
    if isinstance(config, Mapping):
        details["config"] = dict(config)
    return details


def merge_root(states: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    agents: list[Any] = []
    by_provider: dict[str, dict[str, Any]] = {}
    active = 0
    terminals: list[Any] = []
    for state in states:
        for agent in state.get("agents") or []:
            provider = agent.get("provider") if isinstance(agent, Mapping) else None
            if not isinstance(provider, str):
                continue
            existing = by_provider.get(provider)
            if existing is None:
                # A copy: the node's own state must not change under it.
                merged_agent = dict(agent)
                by_provider[provider] = merged_agent
                agents.append(merged_agent)
                continue
            _add_models(existing, agent.get("models"))
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


def _add_models(agent: dict[str, Any], extra: Any) -> None:
    """Append to `agent["models"]` the entries of `extra` whose id it lacks."""
    if not isinstance(extra, list):
        return
    models = list(agent.get("models") or [])
    known = {model.get("id") for model in models if isinstance(model, Mapping)}
    for model in extra:
        if isinstance(model, Mapping) and model.get("id") not in known:
            known.add(model.get("id"))
            models.append(model)
    agent["models"] = models


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
