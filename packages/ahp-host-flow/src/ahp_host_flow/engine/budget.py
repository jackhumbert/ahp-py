"""Output-token budget for one run."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any, Final

# The AgentResult.usage key the budget sums (plan §3 budget row, §4.2).
OUTPUT_TOKENS: Final = "output_tokens"


class Budget:
    """Running total of output tokens against an optional limit."""

    def __init__(self, total: int | None = None) -> None:
        if total is not None and (
            isinstance(total, bool) or not isinstance(total, int) or total < 0
        ):
            raise ValueError("budget total must be an integer >= 0")
        self._total = total
        self._spent = 0

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

    def add(self, usage: Mapping[str, Any]) -> int:
        """Add the usage's output tokens; returns the amount added (0 if unusable)."""
        n = usage.get(OUTPUT_TOKENS, 0)
        if isinstance(n, int) and not isinstance(n, bool) and n > 0:
            self._spent += n
            return n
        return 0
