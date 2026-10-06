"""A turn's token usage, as AHP `UsageInfo`.

Claude Code reports usage three ways, and they mean different things (the
descriptions are the CLI's own result schema):

* each `AssistantMessage.usage` is one model request: ``input_tokens`` is the
  uncached part of that request's prompt, beside ``cache_read_input_tokens``
  and ``cache_creation_input_tokens``;
* `ResultMessage.usage` is the main loop only, *per turn*, and **summed over
  every request of the turn** - a turn with ten tool calls counts its prompt
  ten times;
* `ResultMessage.total_cost_usd` and `model_usage` are *cumulative across
  turns*: "read the latest result rather than summing across results".

`UsageInfo` is one report per turn, and its reader that matters renders
``inputTokens`` against the model's ``maxPromptTokens`` as a context gauge
(see `ModelInfo` in ahp-host). So ``inputTokens`` is the prompt of the turn's
last main-loop request, cached parts included - how full the context window
is - and ``cacheReadTokens`` is the cached part of that same prompt.
The summed figure would put the gauge past 100% after a few tool calls, and
the uncached part alone would keep it near zero. ``outputTokens`` is what the
turn generated, summed, which is what "output tokens generated" says.

Everything else goes in ``_meta``: the cache-creation part of that prompt (no
field of its own), the turn's summed totals, the turn's cost (the change in the
running total), the running total itself, and the model's context window and
output limit as Claude Code reported them for this request.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

__all__ = ["Report", "report"]

_PROMPT_KEYS = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _prompt(usage: Mapping[str, Any]) -> int | None:
    parts = [_int(usage.get(key)) for key in _PROMPT_KEYS]
    known = [part for part in parts if part is not None]
    return sum(known) if known else None


@dataclass(frozen=True)
class Report:
    """The arguments of one `TurnSink.usage` call."""

    input_tokens: int | None
    output_tokens: int | None
    cache_read_tokens: int | None
    model: str | None
    meta: Mapping[str, Any] | None


def _model_entry(model_usage: Any, model: str | None) -> Mapping[str, Any] | None:
    if not isinstance(model_usage, Mapping) or not model_usage:
        return None
    if model is not None and isinstance(model_usage.get(model), Mapping):
        entry: Mapping[str, Any] = model_usage[model]
        return entry
    for key, entry in model_usage.items():
        if not isinstance(entry, Mapping):
            continue
        if model is not None and (
            entry.get("canonicalModel") == model or str(key).startswith(model)
        ):
            return entry
    if len(model_usage) == 1:
        (only,) = model_usage.values()
        return only if isinstance(only, Mapping) else None
    return None


def report(
    turn_usage: Mapping[str, Any] | None,
    last_request: Mapping[str, Any] | None,
    *,
    model: str | None,
    model_usage: Any = None,
    cost: float | None = None,
    total_cost: float | None = None,
) -> Report:
    """One turn's usage.

    *turn_usage* is `ResultMessage.usage`, *last_request* the usage of the
    turn's last main-loop `AssistantMessage` (``None`` if it carried none: then
    the turn's summed figure stands in, which is exact for a one-request
    turn). *cost* is this turn's share of *total_cost*, when it is known.
    """
    turn = turn_usage or {}
    request = last_request if last_request else turn
    meta: dict[str, Any] = {}
    creation = _int(request.get("cache_creation_input_tokens"))
    if creation is not None:
        meta["cacheCreationTokens"] = creation
    totals = {
        name: value
        for name, key in (
            ("inputTokens", "input_tokens"),
            ("outputTokens", "output_tokens"),
            ("cacheReadTokens", "cache_read_input_tokens"),
            ("cacheCreationTokens", "cache_creation_input_tokens"),
        )
        if (value := _int(turn.get(key))) is not None
    }
    if totals:
        meta["turnTotals"] = totals
    if cost is not None:
        meta["costUsd"] = cost
    if total_cost is not None:
        meta["totalCostUsd"] = total_cost
    entry = _model_entry(model_usage, model)
    if entry is not None:
        for key in ("contextWindow", "maxOutputTokens"):
            if (value := _int(entry.get(key))) is not None:
                meta[key] = value
    return Report(
        input_tokens=_prompt(request),
        output_tokens=_int(turn.get("output_tokens")),
        cache_read_tokens=_int(request.get("cache_read_input_tokens")),
        model=model,
        meta=meta or None,
    )
