"""The fleet's telemetry: every node's OTLP signals, as one host's.

`InitializeResult.telemetry` is the host's to give and has no client half:
the client never offers anything, it subscribes to the channel advertised for
each signal it can process, and only then is it sent `otlp/export*` batches.
That is the only agreement there is, so it is the one the gateway keeps.

It cannot pass its nodes' channels through. The field names one channel per
signal, and two nodes may name different channels for one signal (or one
channel that the gateway could not then tell apart). So the gateway advertises
a channel of its own (:data:`CHANNELS`) for each signal *any* connected node
emits - like `automations`, nothing is promised that the fleet cannot keep: a
node that emits no metrics contributes none, which is what absence means. A
subscription to it subscribes to that signal on every such node, and their
batches arrive on the gateway's channel, fanned in as an OpenTelemetry
collector would. Each payload is relayed verbatim - "AHP only adds the routing
envelope" - so its resource attributes (`service.name`) still say which
machine it came from.

**The logs template.** A host that filters logs by severity advertises a
template with a `{level}` variable; one that does not advertises a literal URI
and delivers every severity. The gateway advertises `ahp-otlp://logs{?level}`
only when every node that emits logs filters, since a template is a promise
that the gateway would otherwise half-keep. A level the surface does send is
passed to every node that takes one either way (clients filter defensively
regardless, as the spec requires).

The channels are fixed strings, the same on every connection and every
gateway instance, because a client remembers them from its first `initialize`:
telemetry is stateless and "not replayed on reconnect", and `reconnect` hands
back no new `telemetry` map, so the client re-subscribes to the URIs it
already has.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import parse_qs, quote

__all__ = [
    "CHANNELS",
    "LEVELS",
    "LOGS_TEMPLATE",
    "SCHEME",
    "Wanted",
    "advertised",
    "expand",
    "node_signals",
    "parse",
]

SCHEME: Final = "ahp-otlp:"
#: The signals this protocol version defines. A node advertising another is
#: not relayed: the gateway would be promising a signal it knows nothing of.
SIGNALS: Final = ("logs", "traces", "metrics")
#: The gateway's own channel per signal.
CHANNELS: Final[Mapping[str, str]] = {signal: f"{SCHEME}//{signal}" for signal in SIGNALS}
#: The logs channel, advertised when every node emitting logs filters them.
LOGS_TEMPLATE: Final = f"{CHANNELS['logs']}{{?level}}"
#: OTLP `SeverityNumber` short names, the values `{level}` may take.
LEVELS: Final = frozenset({"trace", "debug", "info", "warn", "error", "fatal"})

#: One RFC 6570 expression: its operator, then its variable list.
_EXPRESSION: Final = re.compile(r"\{([+#./;?&]?)([^{}]*)\}")
#: Per operator: what leads the expansion, what joins its values, whether
#: they are named (`name=value`), and whether reserved characters stay literal.
_OPERATORS: Final[Mapping[str, tuple[str, str, bool, bool]]] = {
    "": ("", ",", False, False),
    "+": ("", ",", False, True),
    "#": ("#", ",", False, True),
    ".": (".", ".", False, False),
    "/": ("/", "/", False, False),
    ";": (";", ";", True, False),
    "?": ("?", "&", True, False),
    "&": ("&", "&", True, False),
}
_RESERVED: Final = ":/?#[]@!$&'()*+,;="


@dataclass(frozen=True)
class Wanted:
    """What a surface subscribed to: a signal, and for logs the least severity."""

    signal: str
    level: str | None = None


def node_signals(handshake: Mapping[str, Any]) -> dict[str, str]:
    """The signals a node's `initialize` result says it emits, and on what URI."""
    telemetry = handshake.get("telemetry")
    if not isinstance(telemetry, Mapping):
        return {}
    return {
        signal: uri
        for signal in SIGNALS
        if isinstance(uri := telemetry.get(signal), str) and uri.startswith(SCHEME)
    }


def _variables(template: str) -> set[str]:
    return {
        spec.partition(":")[0].rstrip("*")
        for match in _EXPRESSION.finditer(template)
        for spec in match.group(2).split(",")
    }


def advertised(nodes: Sequence[Mapping[str, str]]) -> dict[str, str] | None:
    """The fleet's `TelemetryCapabilities`, from each connected node's signals."""
    fleet: dict[str, str] = {}
    for signal in SIGNALS:
        uris = [signals[signal] for signals in nodes if signal in signals]
        if not uris:
            continue
        filtering = signal == "logs" and all("level" in _variables(uri) for uri in uris)
        fleet[signal] = LOGS_TEMPLATE if filtering else CHANNELS[signal]
    return fleet or None


def parse(channel: str) -> Wanted | None:
    """Which gateway channel `channel` is, as the surface expanded it.

    None for anything else. Raises :class:`ValueError` for a gateway channel
    asked for with a level this protocol does not define.
    """
    base, separator, query = channel.partition("?")
    signal = next((name for name, uri in CHANNELS.items() if uri == base), None)
    if signal is None:
        return None
    if not separator:
        return Wanted(signal)
    if signal != "logs":
        # No template variable is defined for traces or metrics, so no
        # expansion of them carries a query.
        return None
    levels = parse_qs(query).get("level")
    if not levels:
        return Wanted(signal)
    level = levels[0].lower()
    if level not in LEVELS:
        raise ValueError(f"{levels[0]!r} is not a log level ({', '.join(sorted(LEVELS))})")
    return Wanted(signal, level)


def expand(template: str, values: Mapping[str, str]) -> str:
    """RFC 6570 expansion of a node's advertised URI.

    `level` is the one variable this protocol defines; any other "MUST be
    ignored by clients", which RFC 6570 spells as undefined - its expression
    expands to nothing. A literal URI comes back unchanged.
    """

    def one(match: re.Match[str]) -> str:
        lead, joiner, named, reserved = _OPERATORS[match.group(1)]
        parts: list[str] = []
        for spec in match.group(2).split(","):
            name, _, prefix = spec.partition(":")
            name = name.rstrip("*")
            value = values.get(name)
            if value is None:
                continue
            if prefix.isdigit():
                value = value[: int(prefix)]
            encoded = quote(value, safe=_RESERVED if reserved else "")
            parts.append(f"{name}={encoded}" if named else encoded)
        return lead + joiner.join(parts) if parts else ""

    return _EXPRESSION.sub(one, template)
