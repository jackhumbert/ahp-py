"""One turn report read into the counters a budget keeps (plan M16)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ahp_protocol.ext.usage import is_request_scoped

from ahp_host_flow.engine.numbers import is_number
from ahp_host_flow.engine.numbers import whole as _count


@dataclass(frozen=True)
class UsageReading:
    """What a usage report counts: input (cache reads apart), output, cost if known."""

    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cost_usd: float | None
    request_scoped: bool


def _money(value: Any) -> float | None:
    """A reported cost: a finite number >= 0 (0 is a real figure here), else None."""
    return float(value) if is_number(value) and value >= 0 else None


def _acp_cost(acp: Any) -> float | None:
    cost = acp.get("cost") if isinstance(acp, Mapping) else None
    if isinstance(cost, Mapping) and cost.get("currency") == "USD":
        return _money(cost.get("amount"))
    return None


def _acp_gauge(acp: Any) -> bool:
    """ahp-host-acp's mark that `input_tokens` is the context size standing in (a gauge)."""
    return isinstance(acp, Mapping) and acp.get("inputGauge") is True


def has_cost_usd(meta: Any) -> bool:
    """True when *meta* carries a usable `costUsd` (a per-turn figure to sum)."""
    return isinstance(meta, Mapping) and _money(meta.get("costUsd")) is not None


def read_usage(
    *,
    input_tokens: int | None,
    output_tokens: int | None,
    cache_read_tokens: int | None,
    meta: Any,
) -> UsageReading:
    """Read a `TurnSink.usage` report (or a recorded turn's usage) properly.

    A Claude report carries the turn's summed figures in `turnTotals` (its `inputTokens` is the
    uncached part, so cache creation is added back and cache reads are kept apart). An ACP
    report (ahp-host-acp) carries the turn's sums, one entry per model, in `turnTokenTotals`
    (`[{model, inputTokens, cachedTokens, outputTokens}]`); its `inputTokens` is only the latest
    model call, so the entries are summed instead. Any other report passes its three counts
    through, except an input that ACP marks as its context gauge (`acpUsage.inputGauge`), which
    counts 0: it is how full the context is, not what the turn used. Cost comes from `costUsd`,
    else from ACP's `acpUsage.cost` when that is in USD (the ACP session's running total, not the
    turn's; `TurnTee` takes the delta).
    """
    mapping = meta if isinstance(meta, Mapping) else {}
    totals = mapping.get("turnTotals")
    acp_totals = mapping.get("turnTokenTotals")
    if isinstance(totals, Mapping):
        in_tok = _count(totals.get("inputTokens")) + _count(totals.get("cacheCreationTokens"))
        cache = _count(totals.get("cacheReadTokens"))
        raw_out = totals.get("outputTokens")
        is_int = isinstance(raw_out, int) and not isinstance(raw_out, bool)
        out = _count(raw_out) if is_int else _count(output_tokens)
    elif isinstance(acp_totals, list):
        entries = [e for e in acp_totals if isinstance(e, Mapping)]
        in_tok = sum(_count(e.get("inputTokens")) for e in entries)
        cache = sum(_count(e.get("cachedTokens")) for e in entries)
        out = sum(_count(e.get("outputTokens")) for e in entries)
    else:
        in_tok, cache, out = _count(input_tokens), _count(cache_read_tokens), _count(output_tokens)
        if _acp_gauge(mapping.get("acpUsage")):
            in_tok = 0
    cost = _money(mapping.get("costUsd"))
    if cost is None:
        cost = _acp_cost(mapping.get("acpUsage"))
    return UsageReading(in_tok, out, cache, cost, is_request_scoped(meta))
