"""The Agent Host Protocol, as a Python library.

Wire types, the nine pure state reducers, protocol-version negotiation, the
error taxonomy, the transport abstraction, and the vendored upstream
conformance corpora. No I/O beyond reading its own fixture files, no agent, no
host, no client -- those are the two peers that depend on this.

The layering is enforced, not merely intended (``lint-imports``):

    types  <-  reducers  <-  conformance
    types  <-  channels / versions / errors
    types  <-  transport

and ``types`` may not import ``asyncio``, ``socket``, ``pathlib``, ``json``,
``os`` or ``websockets`` at all.

Wire values are plain ``dict``/``list``/scalars with ``TypedDict`` views over
them; there is no parse-into-objects step. That is ADR 0001 and it is what lets
a peer stay authoritative for state it relays to peers newer than itself.
"""

from __future__ import annotations

from agent_host_protocol.channels import (
    ROOT_URI,
    ChannelKind,
    classify,
    reducer_for_state,
)
from agent_host_protocol.errors import AhpError
from agent_host_protocol.reducers import REDUCERS, Reducer
from agent_host_protocol.transport import Transport, TransportClosed, memory_pair
from agent_host_protocol.types import (
    ACTION_INTRODUCED_IN,
    ACTION_TYPES,
    AHP_ERROR_CODES,
    IS_CLIENT_DISPATCHABLE,
    JSON_RPC_ERROR_CODES,
    UPSTREAM_PROTOCOL_VERSION,
    UPSTREAM_SUPPORTED_PROTOCOL_VERSIONS,
    JsonObject,
    JsonValue,
    SessionStatus,
    coalesce,
    drop_none,
    reduced_equal,
    wire_equal,
)
from agent_host_protocol.versions import (
    DEFAULT_SUPPORTED_VERSIONS,
    is_compatible,
    negotiate,
    parse_version,
)

#: This distribution's own version. Deliberately independent of the protocol's
#: -- see :data:`UPSTREAM_PROTOCOL_VERSION` for the spec revision vendored here.
#: The single source of truth. `pyproject.toml` reads it from here through
#: hatchling's dynamic version, so there is no second place to forget.
__version__ = "0.1.0"

__all__ = [
    "ACTION_INTRODUCED_IN",
    "ACTION_TYPES",
    "AHP_ERROR_CODES",
    "DEFAULT_SUPPORTED_VERSIONS",
    "IS_CLIENT_DISPATCHABLE",
    "JSON_RPC_ERROR_CODES",
    "REDUCERS",
    "ROOT_URI",
    "UPSTREAM_PROTOCOL_VERSION",
    "UPSTREAM_SUPPORTED_PROTOCOL_VERSIONS",
    "AhpError",
    "ChannelKind",
    "JsonObject",
    "JsonValue",
    "Reducer",
    "SessionStatus",
    "Transport",
    "TransportClosed",
    "__version__",
    "classify",
    "coalesce",
    "drop_none",
    "is_compatible",
    "memory_pair",
    "negotiate",
    "parse_version",
    "reduced_equal",
    "reducer_for_state",
    "wire_equal",
]
