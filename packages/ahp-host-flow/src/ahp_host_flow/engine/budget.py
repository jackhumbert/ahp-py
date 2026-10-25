"""Budget counters for one run: output tokens, input tokens, cost and wall time."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any, Final

from ahp_host_flow.engine.numbers import is_number as _is_number
from ahp_host_flow.engine.numbers import whole as _count

# The AgentResult.usage key the budget sums (plan §3 budget row, §4.2).
OUTPUT_TOKENS: Final = "output_tokens"
INPUT_TOKENS: Final = "input_tokens"
CACHE_READ_TOKENS: Final = "cache_read_tokens"
COST_USD: Final = "cost_usd"
WALL_SECONDS: Final = "wall_seconds"
# The counters a budget can limit, in the order `refusal` names the first exhausted one.
COUNTERS: Final = (OUTPUT_TOKENS, INPUT_TOKENS, COST_USD, WALL_SECONDS)


class Budget:
    """Running totals against optional limits; the output-token counter is the original one."""

    def __init__(
        self,
        total: int | None = None,
        *,
        input_total: int | None = None,
        cost_total: float | None = None,
        wall_total: float | None = None,
        count_cache: bool = True,
    ) -> None:
        for label, limit in (("budget total", total), ("input budget", input_total)):
            if limit is not None and (
                isinstance(limit, bool) or not isinstance(limit, int) or limit < 0
            ):
                raise ValueError(f"{label} must be an integer >= 0")
        if cost_total is not None and not (_is_number(cost_total) and cost_total >= 0):
            raise ValueError("cost budget must be a number >= 0")
        if wall_total is not None and not (_is_number(wall_total) and wall_total > 0):
            raise ValueError("wall budget must be a number > 0")
        self._total = total
        self._spent = 0
        self._input_total = input_total
        self._input = 0
        self._cost_total = cost_total
        self._cost = 0.0
        self._wall_total = wall_total
        self._elapsed = 0.0
        self._count_cache = count_cache

    @property
    def total(self) -> int | None:
        return self._total

    @property
    def spent(self) -> int:
        return self._spent

    @property
    def remaining(self) -> float:
        if self._total is None:
            return math.inf
        return float(max(0, self._total - self._spent))

    @property
    def exhausted(self) -> bool:
        return self._total is not None and self._spent >= self._total

    @property
    def elapsed(self) -> float:
        return self._elapsed

    def set_wall(self, seconds: float) -> None:
        """The Scheduler's reading of the run's wall time so far."""
        self._elapsed = seconds

    def add(self, usage: Mapping[str, Any]) -> int:
        """Add the usage's output tokens; returns the amount added (0 if unusable)."""
        n = _count(usage.get(OUTPUT_TOKENS))
        self._spent += n
        return n

    def add_result(self, usage: Mapping[str, Any], cost_usd: float | None) -> int:
        """Add one live result's output, input (cache reads too if counted) and cost.

        Returns the output tokens added, as `add` does.
        """
        added = self.add(usage)
        self._input += _count(usage.get(INPUT_TOKENS))
        if self._count_cache:
            self._input += _count(usage.get(CACHE_READ_TOKENS))
        if cost_usd is not None and _is_number(cost_usd) and cost_usd > 0:
            self._cost += cost_usd
        return added

    def value(self, name: str) -> float:
        """The current reading of counter *name* (one of `COUNTERS`)."""
        readings = {
            OUTPUT_TOKENS: float(self._spent),
            INPUT_TOKENS: float(self._input),
            COST_USD: self._cost,
            WALL_SECONDS: self._elapsed,
        }
        return readings[name]

    def limit(self, name: str) -> float | None:
        """The limit of counter *name*, or None when it has none."""
        limits = {
            OUTPUT_TOKENS: self._total,
            INPUT_TOKENS: self._input_total,
            COST_USD: self._cost_total,
            WALL_SECONDS: self._wall_total,
        }
        limit = limits[name]
        return None if limit is None else float(limit)

    def refusal(self) -> str | None:
        """The message for the first exhausted counter (value >= limit), else None."""
        for name in COUNTERS:
            limit = self.limit(name)
            if limit is None or self.value(name) < limit:
                continue
            if name == OUTPUT_TOKENS:
                return f"budget exhausted: {self._spent} of {self._total} output tokens spent"
            if name == INPUT_TOKENS:
                return (
                    f"input budget exhausted: {self._input} of {self._input_total} "
                    "input tokens spent"
                )
            if name == COST_USD:
                return f"cost budget exhausted: ${self._cost:.4f} of ${limit:.4f} spent"
            return f"wall budget exhausted: {self._elapsed:.1f} of {limit:g} s elapsed"
        return None
