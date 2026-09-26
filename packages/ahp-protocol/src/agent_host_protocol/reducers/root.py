"""Root channel reducer.

Ported from ``types/channels-root/reducer.ts``. Four actions, full-replacement
semantics, and a hard no-op on ``root/configChanged`` when no config exists.

The action is read with :func:`agent_host_protocol.reducers.js.get` / ``.get``,
never indexed: a missing property is ``undefined`` upstream, not a throw --
``agents: action.agents`` with no ``agents`` writes ``undefined``, which
``JSON.stringify`` drops, so the key goes away rather than raising. And
``root/configChanged`` is client-dispatchable, so ``action["config"]`` would
hand any untrusted peer a ``KeyError`` out of the reducer.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from agent_host_protocol.reducers.js import assign, get
from agent_host_protocol.types.wire import coalesce

__all__ = ["root_reducer"]


def _obj(value: Any) -> Mapping[str, Any]:
    """A wire object read the way JavaScript spreads one.

    ``{...x}`` on ``undefined`` or ``null`` is ``{}`` rather than a
    ``TypeError``, so a payload-less ``root/configChanged`` is an empty replace
    or a no-op merge, exactly as upstream.
    """
    return value if isinstance(value, Mapping) else {}


def root_reducer(state: Any, action: Mapping[str, Any]) -> Any:
    action_type = action.get("type")

    # `agents: action.agents` etc. -- an omitted property is written as
    # `undefined` upstream and dropped at serialization, so it CLEARS an
    # existing key rather than raising or leaving it in place.
    if action_type == "root/agentsChanged":
        return assign({**state}, "agents", get(action, "agents"))

    if action_type == "root/activeSessionsChanged":
        return assign({**state}, "activeSessions", get(action, "activeSessions"))

    if action_type == "root/terminalsChanged":
        return assign({**state}, "terminals", get(action, "terminals"))

    if action_type == "root/configChanged":
        config = state.get("config")
        # Upstream: `if (!state.config) return state;` -- a config change against
        # a host that publishes no config schema is dropped, not created.
        if config is None:
            return state
        # `{...action.config}` -- spreading an absent config is `{}` upstream,
        # not a throw. `coalesce` guards `values: null`, which `{...null}`
        # likewise spreads to `{}`. Mirrors session/configChanged.
        patch = _obj(get(action, "config"))
        values = (
            {**patch} if action.get("replace") else {**coalesce(config.get("values"), {}), **patch}
        )
        return {**state, "config": {**config, "values": values}}

    # Unknown action: return the state unchanged. Never raise -- a client or host
    # speaking an older version must degrade gracefully (softAssertNever).
    return state
